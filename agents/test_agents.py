"""Agent runtime tests: policy enforcement, toolset gating, backend loops
(mocked network/SDK) and run_turn orchestration."""

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from agents.backends.base import (
    PlanProposed,
    TextDelta,
    ToolCallDenied,
    ToolCallFinished,
    ToolCallPlanned,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
)
from agents.backends.claude_code import ClaudeCodeBackend
from agents.backends.openai_compat import OpenAICompatBackend
from agents.models import Turn
from agents.policy import AutonomyPolicy
from agents.runtime import get_backend, run_turn, start_turn
from agents.tools import TOOLS, BoundToolset, ToolDenied
from audit.models import AuditEntry
from audit.services import AuditedAdapter
from instances.adapters import SQLiteAdapter
from instances.models import Server
from workspaces.models import ChatMessage, Project

pytestmark = pytest.mark.django_db


@pytest.fixture
def project(tmp_path):
    server = Server.objects.create(name="Local", adapter_type="sqlite", dsn=str(tmp_path / "t.db"))
    return Project.objects.create(name="P", server=server)


def make_toolset(project, level="full", turn=None):
    adapter = AuditedAdapter(SQLiteAdapter(project.server.dsn), project=project, actor="test")
    return BoundToolset(
        adapter=adapter, policy=AutonomyPolicy(level), project=project, actor="test", turn=turn
    )


class TestPolicy:
    def test_invalid_level_rejected(self):
        with pytest.raises(ValueError, match="Unknown autonomy level"):
            AutonomyPolicy("yolo")

    def test_read_only_blocks_write_tools(self):
        policy = AutonomyPolicy("read_only")
        by_name = {s.name: s for s in TOOLS}
        assert policy.allows(by_name["list_tables"])
        assert policy.allows(by_name["describe_table"])
        assert not policy.allows(by_name["execute_sql"])

    def test_full_allows_everything(self):
        policy = AutonomyPolicy("full")
        assert all(policy.allows(s) for s in TOOLS)


class TestBoundToolset:
    def test_read_only_hides_write_tools_from_the_model(self, project):
        toolset = make_toolset(project, "read_only")
        assert [s.name for s in toolset.allowed_specs()] == [
            "list_tables",
            "describe_table",
            "query_sql",
            "read_context_file",
            "search_context_files",
        ]

    def test_denied_call_raises_even_if_attempted(self, project):
        toolset = make_toolset(project, "read_only")
        with pytest.raises(ToolDenied, match="read-only"):
            toolset.execute("execute_sql", {"sql": "DROP TABLE x"})

    def test_adapter_error_returned_to_model(self, project):
        toolset = make_toolset(project)
        out = toolset.execute("describe_table", {"table": "missing"})
        assert "error" in out

    def test_unknown_tool_and_missing_args(self, project):
        toolset = make_toolset(project)
        assert "error" in toolset.execute("rm_rf", {})
        assert "error" in toolset.execute("describe_table", {})

    def test_execution_is_audited(self, project):
        toolset = make_toolset(project)
        toolset.execute("execute_sql", {"sql": "CREATE TABLE t (id INTEGER)"})
        assert AuditEntry.objects.filter(action="execute_sql", outcome="success").count() == 1


def _openai_response(message):
    return {"choices": [{"message": message}]}


class TestOpenAICompatBackend:
    def test_agent_loop_with_tool_call(self, project):
        backend = OpenAICompatBackend(base_url="http://localhost:11434/v1", model="test-model")
        toolset = make_toolset(project)
        responses = [
            _openai_response(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {
                                "name": "execute_sql",
                                "arguments": json.dumps({"sql": "CREATE TABLE t (id INTEGER)"}),
                            },
                        }
                    ],
                }
            ),
            _openai_response({"role": "assistant", "content": "Done: table t created."}),
        ]
        with mock.patch.object(backend, "_request", side_effect=responses):
            events = list(
                backend.run(system_prompt="sys", history=[], user_message="create table t", toolset=toolset)
            )
        kinds = [type(e) for e in events]
        assert kinds == [ToolCallStarted, ToolCallFinished, TextDelta, TurnCompleted]
        assert events[-1].reply == "Done: table t created."
        # the tool really ran and was audited
        assert AuditEntry.objects.filter(action="execute_sql", outcome="success").exists()

    def test_policy_denial_flows_back_to_model(self, project):
        backend = OpenAICompatBackend(base_url="http://x/v1", model="m")
        toolset = make_toolset(project, "read_only")
        # read_only hides execute_sql from the advertised tools, but a model
        # may still hallucinate a call to it: the gate must hold at execution
        responses = [
            _openai_response(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "execute_sql", "arguments": '{"sql": "DROP TABLE t"}'},
                        }
                    ],
                }
            ),
            _openai_response({"role": "assistant", "content": "Understood, I cannot do that."}),
        ]
        with mock.patch.object(backend, "_request", side_effect=responses):
            events = list(backend.run(system_prompt="s", history=[], user_message="drop it", toolset=toolset))
        assert any(isinstance(e, ToolCallDenied) for e in events)
        assert isinstance(events[-1], TurnCompleted)
        # nothing was executed on the instance
        assert not AuditEntry.objects.filter(action="execute_sql", outcome="success").exists()

    def test_endpoint_error_fails_the_turn(self, project):
        backend = OpenAICompatBackend(base_url="http://x/v1", model="m")
        toolset = make_toolset(project)
        with mock.patch.object(backend, "_request", side_effect=KeyError("choices")):
            events = list(backend.run(system_prompt="s", history=[], user_message="hi", toolset=toolset))
        assert isinstance(events[-1], TurnFailed)

    def test_availability_requires_model(self):
        assert OpenAICompatBackend(model="")._request is not None  # sanity
        ok, reason = OpenAICompatBackend(model="").availability()
        assert not ok and "DIABASE_OPENAI_MODEL" in reason
        assert OpenAICompatBackend(model="llama3").availability()[0]


class TestAnthropicAPIBackend:
    def test_agent_loop_with_tool_call(self, project, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        from agents.backends.anthropic_api import AnthropicAPIBackend

        backend = AnthropicAPIBackend(model="claude-test")
        toolset = make_toolset(project)

        tool_use = SimpleNamespace(type="tool_use", name="list_tables", input={}, id="tu_1", text=None)
        first = SimpleNamespace(content=[tool_use], stop_reason="tool_use")
        final_text = SimpleNamespace(type="text", text="No tables yet.")
        second = SimpleNamespace(content=[final_text], stop_reason="end_turn")

        fake_client = mock.MagicMock()
        fake_client.messages.create.side_effect = [first, second]
        with mock.patch("anthropic.Anthropic", return_value=fake_client):
            events = list(backend.run(system_prompt="s", history=[], user_message="tables?", toolset=toolset))

        kinds = [type(e) for e in events]
        assert kinds == [ToolCallStarted, ToolCallFinished, TextDelta, TurnCompleted]
        assert events[1].output == {"tables": []}
        # advertised tools came from the registry
        sent_tools = fake_client.messages.create.call_args_list[0].kwargs["tools"]
        assert [t["name"] for t in sent_tools] == [
            "list_tables",
            "describe_table",
            "query_sql",
            "execute_sql",
            "read_context_file",
            "search_context_files",
        ]

    def test_availability(self, monkeypatch):
        from agents.backends.anthropic_api import AnthropicAPIBackend

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert not AnthropicAPIBackend().availability()[0]
        monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
        assert AnthropicAPIBackend().availability()[0]


class TestClaudeCodeBackend:
    def test_availability_reflects_cli_presence(self):
        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            assert ClaudeCodeBackend().availability()[0]
        with mock.patch("shutil.which", return_value=None):
            ok, reason = ClaudeCodeBackend().availability()
            assert not ok and "claude" in reason

    @pytest.mark.django_db(transaction=True)
    def test_events_arrive_incrementally_not_buffered_until_the_end(self, project, monkeypatch):
        """Regression test for the bug where this backend collected every
        event into a list during asyncio.run() and only yielded them once
        the ENTIRE turn had finished — so the GUI saw nothing until the
        agent was completely done, no matter how many tool calls it made.

        Proof: the SDK is faked to pause for a real 0.25s between its first
        and second message. If events are truly streamed, the first ones
        arrive well under that delay; the delayed one only arrives after
        it. Under the old buffering bug, EVERY event — including the
        first — would only appear after the full delay.
        """
        import asyncio
        import sys
        import time
        import types

        toolset = make_toolset(project)
        DELAY = 0.25

        class FakeTextBlock:
            def __init__(self, text):
                self.text = text

        class FakeAssistantMessage:
            def __init__(self, blocks):
                self.content = blocks

        class FakeResultMessage:
            is_error = False

        async def fake_query(*, prompt, options):
            yield FakeAssistantMessage([FakeTextBlock("Looking...")])
            await options.mcp_servers["db"]["list_tables"]({})
            await asyncio.sleep(DELAY)
            yield FakeAssistantMessage([FakeTextBlock("Done.")])
            yield FakeResultMessage()

        def fake_tool(name, description, schema):
            return lambda fn: fn  # the real decorator just wraps; identity is enough here

        def fake_create_sdk_mcp_server(name, version, tools):
            names = [s.name for s in toolset.allowed_specs()]
            return {n: t for n, t in zip(names, tools, strict=True)}

        fake_module = types.SimpleNamespace(
            AssistantMessage=FakeAssistantMessage,
            ClaudeAgentOptions=lambda **kw: types.SimpleNamespace(**kw),
            ResultMessage=FakeResultMessage,
            TextBlock=FakeTextBlock,
            create_sdk_mcp_server=fake_create_sdk_mcp_server,
            query=fake_query,
            tool=fake_tool,
        )
        monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_module)

        it = ClaudeCodeBackend().run(system_prompt="s", history=[], user_message="tables?", toolset=toolset)
        t0 = time.monotonic()

        first = next(it)
        t_first = time.monotonic() - t0
        assert isinstance(first, TextDelta) and first.text == "Looking..."
        assert t_first < DELAY * 0.6, "first event should not wait for the whole turn"

        assert isinstance(next(it), ToolCallStarted)
        assert isinstance(next(it), ToolCallFinished)
        t_before_delay = time.monotonic() - t0

        fourth = next(it)  # only produced after the SDK's artificial delay
        t_after_delay = time.monotonic() - t0
        assert isinstance(fourth, TextDelta) and fourth.text == "Done."
        assert t_after_delay - t_before_delay > DELAY * 0.6

        assert isinstance(next(it), TurnCompleted)
        with pytest.raises(StopIteration):
            next(it)


class FakeBackend:
    """Deterministic backend for orchestration tests."""

    name = "fake"
    model = "fake-1"

    def __init__(self, events):
        self._events = events

    def availability(self):
        return True, ""

    def run(self, **kwargs):
        yield from self._events


class TestRunTurn:
    def test_completed_turn_persists_everything(self, project):
        events = [TextDelta(text="hi"), TurnCompleted(reply="All done.")]
        with mock.patch("agents.runtime.get_backend", return_value=FakeBackend(events)):
            out = list(run_turn(project, "do something", user="francesco"))

        assert isinstance(out[-1], TurnCompleted)
        turn = Turn.objects.get()
        assert turn.status == "completed" and turn.finished_at is not None
        roles = list(ChatMessage.objects.values_list("role", flat=True))
        assert roles == ["user", "assistant"]
        actions = set(AuditEntry.objects.values_list("action", flat=True))
        assert {"chat.message", "chat.reply"} <= actions

    def test_failed_turn_recorded(self, project):
        events = [TurnFailed(error="boom")]
        with mock.patch("agents.runtime.get_backend", return_value=FakeBackend(events)):
            out = list(run_turn(project, "explode"))
        assert isinstance(out[-1], TurnFailed)
        turn = Turn.objects.get()
        assert turn.status == "failed" and turn.error == "boom"
        # user message persisted, no assistant reply
        assert list(ChatMessage.objects.values_list("role", flat=True)) == ["user"]

    def test_get_backend_env_selection(self, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "openai_compat")
        assert get_backend().name == "openai_compat"
        monkeypatch.setenv("AGENT_BACKEND", "nope")
        with pytest.raises(ValueError, match="Unknown agent backend"):
            get_backend()


class _ImmediateThread:
    """Runs the target synchronously — exercises the worker's logic without
    real concurrency or timing, so these tests stay fast and deterministic."""

    def __init__(self, target, daemon=None):
        self._target = target

    def start(self):
        self._target()


class TestStartTurn:
    def test_persists_every_event_and_finalizes(self, project):
        from agents.models import Turn, TurnEvent

        events = [
            ToolCallStarted(tool="list_tables", payload={}),
            ToolCallFinished(tool="list_tables", payload={}, output={"tables": []}),
            TextDelta(text="No tables yet."),
            TurnCompleted(reply="No tables yet."),
        ]
        with (
            mock.patch("agents.runtime.get_backend", return_value=FakeBackend(events)),
            mock.patch("agents.runtime.threading.Thread", _ImmediateThread),
        ):
            turn = start_turn(project, "what tables exist?", user="francesco")

        turn.refresh_from_db()
        assert turn.status == "completed"
        kinds = list(TurnEvent.objects.filter(turn=turn).order_by("pk").values_list("kind", flat=True))
        assert kinds == ["ToolCallStarted", "ToolCallFinished", "TextDelta", "TurnCompleted"]
        completed_row = TurnEvent.objects.get(turn=turn, kind="TurnCompleted")
        assert completed_row.data == {"reply": "No tables yet."}
        assert ChatMessage.objects.filter(
            project=project, role="assistant", content="No tables yet."
        ).exists()
        assert Turn.objects.count() == 1  # unchanged from what run_turn would create

    def test_execution_is_actually_handed_to_a_background_thread(self, project):
        # complements the test above: proves start_turn doesn't just run the
        # worker inline — Turn stays "running" because the mocked Thread
        # never actually invokes its target
        events = [TurnCompleted(reply="ok")]
        with (
            mock.patch("agents.runtime.get_backend", return_value=FakeBackend(events)),
            mock.patch("agents.runtime.threading.Thread") as thread_cls,
        ):
            thread_cls.return_value = mock.MagicMock()
            turn = start_turn(project, "hi")

        assert thread_cls.call_args.kwargs.get("daemon") is True
        thread_cls.return_value.start.assert_called_once()
        assert turn.status == "running"


class TestAgentConnections:
    def test_api_key_encrypted_at_rest_and_masked(self, project):
        from agents.models import AgentConnection

        conn = AgentConnection(name="OpenRouter", backend="openai_compat", model="gpt-4o")
        conn.api_key = "sk-or-v1-supersecretvalue123"
        conn.save()
        conn.refresh_from_db()
        assert "supersecret" not in conn.api_key_encrypted  # not plaintext in the DB
        assert conn.api_key == "sk-or-v1-supersecretvalue123"  # roundtrips
        assert "supersecret" not in conn.masked_key
        assert conn.masked_key.startswith("sk-")

    def test_project_connection_drives_the_backend(self, project, monkeypatch):
        from agents.models import AgentConnection

        monkeypatch.delenv("AGENT_BACKEND", raising=False)
        conn = AgentConnection(
            name="Ollama", backend="openai_compat", model="llama3.1", base_url="http://localhost:11434/v1"
        )
        conn.api_key = "local-key"
        conn.save()
        project.agent_connection = conn
        project.save()
        backend = get_backend(project=project)
        assert backend.name == "openai_compat"
        assert backend.model == "llama3.1"
        assert backend.base_url == "http://localhost:11434/v1"
        assert backend.api_key == "local-key"

    def test_anthropic_connection_key_reaches_the_backend(self, project, monkeypatch):
        from agents.models import AgentConnection

        monkeypatch.delenv("AGENT_BACKEND", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        conn = AgentConnection(name="Claude prod", backend="anthropic_api", model="claude-opus-4-8")
        conn.api_key = "sk-ant-test"
        conn.save()
        project.agent_connection = conn
        project.save()
        backend = get_backend(project=project)
        assert backend.name == "anthropic_api"
        assert backend.model == "claude-opus-4-8"
        assert backend.api_key == "sk-ant-test"
        assert backend.availability()[0]  # key on the connection suffices

    def test_explicit_name_beats_project_connection(self, project):
        from agents.models import AgentConnection

        conn = AgentConnection.objects.create(name="C", backend="anthropic_api")
        project.agent_connection = conn
        project.save()
        assert get_backend("claude_code", project).name == "claude_code"

    def test_env_fallback_when_no_connection(self, project, monkeypatch):
        monkeypatch.setenv("AGENT_BACKEND", "openai_compat")
        assert get_backend(project=project).name == "openai_compat"


# ---------------------------------------------------------------------------
# Phase 2 — Plan & Approve
# ---------------------------------------------------------------------------


def make_turn(project, **kw):
    from workspaces.models import Conversation

    conversation = kw.pop("conversation", None) or Conversation.objects.create(project=project)
    return Turn.objects.create(
        project=project, conversation=conversation, backend="fake", user_message="x", **kw
    )


def make_proposed_plan(project, sqls):
    from agents.models import Plan, PlanStep

    plan = Plan.objects.create(project=project, turn=make_turn(project), status="proposed")
    for i, sql in enumerate(sqls, start=1):
        PlanStep.objects.create(plan=plan, order=i, tool="execute_sql", payload={"sql": sql})
    return plan


class TestPlanPolicy:
    def test_plan_level_flags_writes_and_passes_reads(self):
        policy = AutonomyPolicy("plan")
        by_name = {s.name: s for s in TOOLS}
        decision = policy.check(by_name["execute_sql"])
        assert decision.allowed and decision.requires_plan
        assert not policy.check(by_name["list_tables"]).requires_plan

    def test_plan_level_still_advertises_write_tools(self, project):
        # the model must SEE execute_sql to queue steps with it
        toolset = make_toolset(project, "plan", turn=make_turn(project))
        assert "execute_sql" in [s.name for s in toolset.allowed_specs()]

    def test_plan_mode_prompt_injected_only_at_plan_level(self, project):
        from agents.prompts import build_system_prompt

        project.autonomy_level = "plan"
        assert "Plan & approve mode" in build_system_prompt(project)
        project.autonomy_level = "full"
        assert "Plan & approve mode" not in build_system_prompt(project)


class TestPlanGate:
    def test_write_is_queued_not_executed(self, project):
        from agents.models import Plan

        turn = make_turn(project)
        toolset = make_toolset(project, "plan", turn=turn)
        out1 = toolset.execute("execute_sql", {"sql": "CREATE TABLE t (id INTEGER)"})
        out2 = toolset.execute("execute_sql", {"sql": "CREATE TABLE u (id INTEGER)"})
        assert out1["planned"]["step"] == 1 and out2["planned"]["step"] == 2
        plan = Plan.objects.get()  # both steps landed on ONE draft plan
        assert plan.status == "draft" and plan.turn == turn
        assert list(plan.steps.values_list("tool", flat=True)) == ["execute_sql", "execute_sql"]
        # nothing touched the instance; the queueing itself is audited
        assert not AuditEntry.objects.filter(action="execute_sql").exists()
        assert AuditEntry.objects.filter(action="plan.step_queued").count() == 2

    def test_reads_execute_immediately_at_plan_level(self, project):
        toolset = make_toolset(project, "plan", turn=make_turn(project))
        assert toolset.execute("list_tables", {}) == {"tables": []}

    def test_query_sql_is_a_read_and_never_queued(self, project):
        from agents.models import Plan

        toolset = make_toolset(project, "plan", turn=make_turn(project))
        toolset.execute("list_tables", {})  # touches the db file into existence
        out = toolset.execute("query_sql", {"sql": "SELECT 1 AS x"})
        assert out["rows"] == [{"x": 1}]  # executed NOW, not planned
        assert not Plan.objects.exists()
        assert AuditEntry.objects.filter(action="query_sql", outcome="success").exists()

    def test_query_sql_write_attempt_surfaces_as_tool_error(self, project):
        toolset = make_toolset(project, "plan", turn=make_turn(project))
        toolset.execute("list_tables", {})  # touches the db file into existence
        out = toolset.execute("query_sql", {"sql": "CREATE TABLE sneaky (id INTEGER)"})
        assert "Read-only query failed" in out["error"]  # the model can read and react
        assert "sneaky" not in (toolset.execute("list_tables", {})["tables"])

    def test_completed_turn_proposes_the_draft(self, project):
        from agents.models import Plan

        class QueueingBackend(FakeBackend):
            def run(self, *, toolset, **kwargs):
                out = toolset.execute("execute_sql", {"sql": "CREATE TABLE t (id INTEGER)"})
                yield ToolCallPlanned(tool="execute_sql", payload={}, step=out["planned"]["step"])
                yield TurnCompleted(reply="I propose one step.")

        project.autonomy_level = "plan"
        project.save()
        with mock.patch("agents.runtime.get_backend", return_value=QueueingBackend([])):
            events = list(run_turn(project, "create table t"))
        kinds = [type(e) for e in events]
        assert kinds == [ToolCallPlanned, PlanProposed, TurnCompleted]
        plan = Plan.objects.get()
        assert plan.status == "proposed"
        assert events[1].plan_id == plan.pk and events[1].steps == 1

    def test_failed_turn_discards_the_draft(self, project):
        from agents.models import Plan

        class FailingQueueingBackend(FakeBackend):
            def run(self, *, toolset, **kwargs):
                toolset.execute("execute_sql", {"sql": "CREATE TABLE t (id INTEGER)"})
                yield TurnFailed(error="boom")

        project.autonomy_level = "plan"
        project.save()
        with mock.patch("agents.runtime.get_backend", return_value=FailingQueueingBackend([])):
            list(run_turn(project, "create table t"))
        assert Plan.objects.get().status == "superseded"


class TestPlanDecisions:
    def test_approve_applies_steps_and_starts_continuation(self, project):
        from agents.runtime import approve_plan

        plan = make_proposed_plan(
            project,
            ["CREATE TABLE t (id INTEGER)", "INSERT INTO t (id) VALUES (1)"],
        )
        continuation = [TurnCompleted(reply="Verified: table t has one row.")]
        with (
            mock.patch("agents.runtime.get_backend", return_value=FakeBackend(continuation)),
            mock.patch("agents.runtime.threading.Thread", _ImmediateThread),
        ):
            approve_plan(plan, user="francesco")

        plan.refresh_from_db()
        assert plan.status == "applied"
        assert plan.decided_by == "francesco" and plan.decided_at is not None
        assert list(plan.steps.values_list("status", flat=True)) == ["applied", "applied"]
        # steps ran through the audited path, attributed to the USER
        sql_entries = AuditEntry.objects.filter(action="execute_sql", outcome="success")
        assert sql_entries.count() == 2
        assert all(e.actor_type == "user" and e.actor == "francesco" for e in sql_entries)
        actions = set(AuditEntry.objects.values_list("action", flat=True))
        assert {"plan.approved", "plan.applied"} <= actions
        # the incremental-apply loop: real results went back to the agent
        plan.refresh_from_db()
        assert plan.continuation_turn is not None
        report = ChatMessage.objects.get(kind="plan_result")
        assert "Step 1" in report.content and "applied" in report.content

    def test_apply_stops_at_first_failure_and_skips_the_rest(self, project):
        from agents.runtime import approve_plan

        plan = make_proposed_plan(
            project,
            ["CREATE TABLE t (id INTEGER)", "INSERT INTO nope VALUES (1)", "DROP TABLE t"],
        )
        with (
            mock.patch("agents.runtime.get_backend", return_value=FakeBackend([TurnCompleted(reply="ok")])),
            mock.patch("agents.runtime.threading.Thread", _ImmediateThread),
        ):
            approve_plan(plan, user="francesco")

        plan.refresh_from_db()
        assert plan.status == "failed" and plan.error
        assert list(plan.steps.values_list("status", flat=True)) == ["applied", "failed", "skipped"]
        # the DROP never ran
        assert not AuditEntry.objects.filter(
            action="execute_sql", payload_in={"sql": "DROP TABLE t"}
        ).exists()
        # the failure report still goes back to the agent so it can revise
        assert "FAILED" in ChatMessage.objects.get(kind="plan_result").content

    def test_reject_executes_nothing_and_informs_the_agent(self, project):
        from agents.runtime import reject_plan

        plan = make_proposed_plan(project, ["DROP TABLE precious"])
        reject_plan(plan, user="francesco")
        plan.refresh_from_db()
        assert plan.status == "rejected"
        assert not AuditEntry.objects.filter(action="execute_sql").exists()
        assert AuditEntry.objects.filter(action="plan.rejected").exists()
        assert "rejected" in ChatMessage.objects.get(kind="plan_result").content

    def test_revise_supersedes_and_starts_a_new_turn(self, project):
        from agents.runtime import revise_plan

        plan = make_proposed_plan(project, ["CREATE TABLE t (id INTEGER)"])
        with (
            mock.patch("agents.runtime.get_backend", return_value=FakeBackend([TurnCompleted(reply="v2")])),
            mock.patch("agents.runtime.threading.Thread", _ImmediateThread),
        ):
            turn = revise_plan(plan, "use TEXT ids instead", user="francesco")
        plan.refresh_from_db()
        assert plan.status == "superseded"
        assert f"Plan #{plan.pk} was not applied" in turn.user_message
        assert "use TEXT ids instead" in turn.user_message
        # the revision turn stays in the conversation that proposed the plan
        assert turn.conversation == plan.turn.conversation
        assert AuditEntry.objects.filter(action="plan.superseded").exists()


class TestConversations:
    def test_history_is_scoped_to_the_conversation(self, project):
        from workspaces.models import Conversation

        other = Conversation.objects.create(project=project, title="Other thread")
        ChatMessage.objects.create(
            project=project, conversation=other, role="user", content="SECRET other-thread message"
        )

        captured = {}

        class CapturingBackend(FakeBackend):
            def run(self, *, history, **kwargs):
                captured["history"] = history
                yield TurnCompleted(reply="ok")

        with mock.patch("agents.runtime.get_backend", return_value=CapturingBackend([])):
            list(run_turn(project, "hello"))
        # the fresh conversation sees nothing from the other thread
        assert captured["history"] == []

    def test_first_message_titles_the_conversation(self, project):
        with mock.patch("agents.runtime.get_backend", return_value=FakeBackend([TurnCompleted(reply="hi")])):
            list(run_turn(project, "Add a reviews table\nwith ratings"))
        from workspaces.models import Conversation

        conversation = Conversation.objects.get()
        assert conversation.title == "Add a reviews table"
        assert conversation.messages.count() == 2  # user + reply, both attached

    def test_apply_continuation_stays_in_the_plans_conversation(self, project):
        from agents.runtime import approve_plan

        plan = make_proposed_plan(project, ["CREATE TABLE t (id INTEGER)"])
        with (
            mock.patch("agents.runtime.get_backend", return_value=FakeBackend([TurnCompleted(reply="ok")])),
            mock.patch("agents.runtime.threading.Thread", _ImmediateThread),
        ):
            approve_plan(plan, user="francesco")
        plan.refresh_from_db()
        assert plan.continuation_turn.conversation == plan.turn.conversation
        report = ChatMessage.objects.get(kind="plan_result")
        assert report.conversation == plan.turn.conversation


class TestFunctionTools:
    """Capability-gated tools: advertised only where the instance supports
    them, plan-gated like any write, with a review diff captured at queue
    time."""

    def test_function_tools_hidden_without_the_capability(self, project):
        toolset = make_toolset(project)  # sqlite adapter: no "functions"
        names = [s.name for s in toolset.allowed_specs()]
        assert "list_functions" not in names and "deploy_function" not in names
        out = toolset.execute("deploy_function", {"slug": "x", "body": "y"})
        assert "not supported" in out["error"]

    def _fn_toolset(self, project, level="plan", turn=None):
        """A toolset whose adapter fakes the functions capability."""

        class FakeFnAdapter:
            capabilities = frozenset({"functions"})

            def __init__(self):
                self.deployed = {"greet": "Deno.serve(() => new Response('v1'))"}

            def list_functions(self):
                return [{"slug": s, "status": "ACTIVE", "version": 1} for s in self.deployed]

            def get_function_body(self, slug):
                if slug not in self.deployed:
                    raise Exception(f"no function {slug}")
                return self.deployed[slug]

            def deploy_function(self, slug, body, *, name="", verify_jwt=True):
                updated = slug in self.deployed
                self.deployed[slug] = body
                return {"slug": slug, "version": 2, "updated": updated}

            def delete_function(self, slug):
                self.deployed.pop(slug, None)
                return {"slug": slug, "deleted": True}

        return BoundToolset(
            adapter=FakeFnAdapter(), policy=AutonomyPolicy(level), project=project, actor="test", turn=turn
        )

    def test_function_tools_advertised_with_the_capability(self, project):
        toolset = self._fn_toolset(project)
        names = [s.name for s in toolset.allowed_specs()]
        assert {"list_functions", "read_function", "deploy_function", "delete_function"} <= set(names)

    def test_reads_execute_immediately(self, project):
        toolset = self._fn_toolset(project, turn=make_turn(project))
        assert toolset.execute("list_functions", {})["functions"][0]["slug"] == "greet"
        assert "v1" in toolset.execute("read_function", {"slug": "greet"})["body"]

    def test_deploy_is_queued_with_a_diff_against_the_live_source(self, project):
        from agents.models import PlanStep

        toolset = self._fn_toolset(project, turn=make_turn(project))
        new_body = "Deno.serve(() => new Response('v2'))"
        out = toolset.execute("deploy_function", {"slug": "greet", "body": new_body})
        assert out["planned"]["step"] == 1
        step = PlanStep.objects.get()
        assert step.meta["updates_existing"] is True
        assert "-Deno.serve(() => new Response('v1'))" in step.meta["diff"]
        assert "+Deno.serve(() => new Response('v2'))" in step.meta["diff"]
        # nothing deployed yet
        assert toolset.adapter.deployed["greet"].endswith("'v1'))")

    def test_new_function_has_no_diff_but_flags_it(self, project):
        from agents.models import PlanStep

        toolset = self._fn_toolset(project, turn=make_turn(project))
        toolset.execute("deploy_function", {"slug": "fresh", "body": "code"})
        step = PlanStep.objects.get()
        assert step.meta == {"updates_existing": False, "diff": ""}

    def test_apply_dispatches_function_steps(self, project):
        """The generic apply executes any queued tool, not just SQL."""
        from agents.models import Plan, PlanStep
        from agents.runtime import _apply

        turn = make_turn(project)
        plan = Plan.objects.create(project=project, turn=turn, status="applying")
        PlanStep.objects.create(
            plan=plan,
            order=1,
            tool="deploy_function",
            payload={"slug": "greet", "body": "new code"},
        )
        fn_toolset = self._fn_toolset(project, level="full")
        with (
            mock.patch("agents.runtime.BoundToolset", return_value=fn_toolset),
            mock.patch("agents.runtime.AuditedAdapter"),
            mock.patch("agents.runtime.get_adapter"),
            mock.patch("agents.runtime.start_turn", return_value=make_turn(project)),
        ):
            _apply(plan, "francesco")
        plan.refresh_from_db()
        assert plan.status == "applied"
        step = plan.steps.get()
        assert step.status == "applied" and step.output["updated"] is True
        assert fn_toolset.adapter.deployed["greet"] == "new code"

    def test_functions_prompt_only_for_capable_adapters(self, project):
        from agents.prompts import build_system_prompt

        assert "Edge functions" not in build_system_prompt(project)  # sqlite
        project.server.adapter_type = "supabase"
        assert "Edge functions" in build_system_prompt(project)


class TestStorageTools:
    """Bucket tools: same capability gating and plan discipline as the
    function tools — structure through plans, files never."""

    def _storage_toolset(self, project, level="plan", turn=None):
        class FakeStorageAdapter:
            capabilities = frozenset({"storage"})

            def __init__(self):
                self.buckets = {"avatars": {"public": True}}

            def list_buckets(self):
                return [{"id": b, "name": b, "public": v["public"]} for b, v in self.buckets.items()]

            def create_bucket(self, name, *, public=False, file_size_limit=None, allowed_mime_types=None):
                self.buckets[name] = {"public": public}
                return {"name": name, "public": public, "created": True}

            def delete_bucket(self, name):
                self.buckets.pop(name, None)
                return {"name": name, "deleted": True}

        return BoundToolset(
            adapter=FakeStorageAdapter(),
            policy=AutonomyPolicy(level),
            project=project,
            actor="test",
            turn=turn,
        )

    def test_storage_tools_hidden_without_the_capability(self, project):
        toolset = make_toolset(project)  # sqlite adapter: no "storage"
        names = [s.name for s in toolset.allowed_specs()]
        assert "list_buckets" not in names and "create_bucket" not in names
        out = toolset.execute("create_bucket", {"name": "avatars"})
        assert "not supported" in out["error"]

    def test_storage_tools_advertised_with_the_capability(self, project):
        names = [s.name for s in self._storage_toolset(project).allowed_specs()]
        assert {"list_buckets", "create_bucket", "update_bucket", "delete_bucket"} <= set(names)

    def test_list_executes_immediately_at_plan_level(self, project):
        toolset = self._storage_toolset(project, turn=make_turn(project))
        assert toolset.execute("list_buckets", {})["buckets"][0]["id"] == "avatars"

    def test_bucket_writes_are_queued_not_executed(self, project):
        from agents.models import PlanStep

        toolset = self._storage_toolset(project, turn=make_turn(project))
        out = toolset.execute("create_bucket", {"name": "docs", "public": False})
        assert out["planned"]["step"] == 1
        assert "docs" not in toolset.adapter.buckets  # nothing touched the instance
        step = PlanStep.objects.get()
        assert step.tool == "create_bucket" and step.payload["name"] == "docs"

    def test_full_autonomy_executes_bucket_writes(self, project):
        toolset = self._storage_toolset(project, level="full")
        out = toolset.execute("create_bucket", {"name": "docs"})
        assert out == {"name": "docs", "public": False, "created": True}
        assert toolset.execute("delete_bucket", {"name": "docs"}) == {"name": "docs", "deleted": True}

    def test_storage_prompt_only_for_capable_adapters(self, project):
        from agents.prompts import build_system_prompt

        assert "# Storage" not in build_system_prompt(project)  # sqlite
        project.server.adapter_type = "supabase"
        prompt = build_system_prompt(project)
        assert "# Storage" in prompt and "storage.objects" in prompt


class TestFunctionSourceTracking:
    """Local-first sources: Diabase keeps what it deploys and reads from
    its own copy; drift with the live version is detectable, never hidden."""

    def _toolset(self, project, turn=None):
        return TestFunctionTools()._fn_toolset(project, level="full", turn=turn)

    def test_successful_deploy_tracks_the_source(self, project):
        from instances.services import get_function_source

        toolset = self._toolset(project)
        toolset.execute("deploy_function", {"slug": "greet", "body": "v2 code"})
        src = get_function_source(project.server, "greet")
        assert src.body == "v2 code" and src.deployed_version == 2
        assert src.deployed_by == "test"

    def test_read_function_prefers_the_tracked_copy(self, project):
        toolset = self._toolset(project)
        toolset.execute("deploy_function", {"slug": "greet", "body": "tracked source"})
        out = toolset.execute("read_function", {"slug": "greet"})
        assert out["tracked"] is True and out["body"] == "tracked source"
        assert out["deployed_version"] == 2

    def test_untracked_function_falls_back_to_the_api(self, project):
        toolset = self._toolset(project)
        out = toolset.execute("read_function", {"slug": "greet"})  # never deployed via Diabase
        assert out["tracked"] is False and "v1" in out["body"]

    def test_delete_untracks(self, project):
        from instances.services import get_function_source

        toolset = self._toolset(project)
        toolset.execute("deploy_function", {"slug": "greet", "body": "x"})
        toolset.execute("delete_function", {"slug": "greet"})
        assert get_function_source(project.server, "greet") is None

    def test_queue_time_diff_reads_the_tracked_copy(self, project):
        from agents.models import PlanStep
        from instances.services import save_function_source

        save_function_source(project.server, "greet", "line one\n", version=4)
        plan_toolset = TestFunctionTools()._fn_toolset(project, turn=make_turn(project))
        plan_toolset.execute("deploy_function", {"slug": "greet", "body": "line two\n"})
        step = PlanStep.objects.get()
        assert step.meta["updates_existing"] is True
        assert "-line one" in step.meta["diff"] and "+line two" in step.meta["diff"]

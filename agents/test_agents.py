"""Agent runtime tests: policy enforcement, toolset gating, backend loops
(mocked network/SDK) and run_turn orchestration."""

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from agents.backends.base import (
    TextDelta,
    ToolCallDenied,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
    TurnFailed,
)
from agents.backends.claude_code import ClaudeCodeBackend
from agents.backends.openai_compat import OpenAICompatBackend
from agents.models import Turn
from agents.policy import AutonomyPolicy
from agents.runtime import get_backend, run_turn
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


def make_toolset(project, level="full"):
    adapter = AuditedAdapter(SQLiteAdapter(project.server.dsn), project=project, actor="test")
    return BoundToolset(adapter=adapter, policy=AutonomyPolicy(level))


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
            "read_context_file",
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
            "execute_sql",
            "read_context_file",
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

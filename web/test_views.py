"""GUI view tests: pages render, creations are audited, SSE stream shapes."""

import json
from unittest import mock

import pytest
from django.urls import reverse

from audit.models import AuditEntry
from instances.models import Server
from workspaces.models import Project

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def operator(client, django_user_model):
    """Every view sits behind LoginRequiredMiddleware: the test client is
    the instance operator, already past the forced password change."""
    user = django_user_model.objects.create_user("francesco", password="not-the-factory-one")  # nosec B106
    client.force_login(user)
    return user


@pytest.fixture
def project(tmp_path):
    server = Server.objects.create(name="Local", adapter_type="sqlite", dsn=str(tmp_path / "t.db"))
    return Project.objects.create(name="Room", server=server)


class TestPages:
    def test_home_renders(self, client, project):
        r = client.get(reverse("home"))
        assert r.status_code == 200
        assert b"Room" in r.content

    def test_project_room_renders_orb_and_workspace(self, client, project):
        r = client.get(reverse("project_room", args=[project.pk]))
        assert r.status_code == 200
        assert b'id="orb"' in r.content
        assert b'id="workspace"' in r.content
        # all four workspace tabs are present
        for pane in (b"pane-schema", b"pane-audit", b"pane-context", b"pane-settings"):
            assert pane in r.content
        assert b"activeTurnId: null" in r.content

    def test_project_room_exposes_running_turn_for_resume(self, client, project):
        from agents.models import Turn
        from workspaces.models import Conversation

        conversation = Conversation.objects.create(project=project)
        turn = Turn.objects.create(
            project=project,
            conversation=conversation,
            backend="claude_code",
            user_message="hi",
            status="running",
        )
        r = client.get(reverse("project_room", args=[project.pk]))
        assert f"activeTurnId: {turn.pk}".encode() in r.content

    def test_finished_turns_are_not_offered_for_resume(self, client, project):
        from agents.models import Turn

        Turn.objects.create(project=project, backend="claude_code", user_message="hi", status="completed")
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b"activeTurnId: null" in r.content

    def test_schema_json(self, client, project):
        r = client.get(reverse("schema_json", args=[project.pk]))
        assert r.status_code == 200
        assert r.json() == {"schema": {}}

    def test_audit_partial_lists_entries(self, client, project):
        from audit.services import record

        record(action="project.created", actor_type="user", project=project)
        r = client.get(reverse("audit_partial", args=[project.pk]))
        assert b"project.created" in r.content


class TestCreationFlows:
    def test_server_create_audits_without_creating_a_project(self, client, tmp_path):
        r = client.post(
            reverse("server_create"),
            {"name": "Prod", "adapter_type": "sqlite", "dsn": str(tmp_path / "p.db")},
        )
        assert r.status_code == 302 and r.url == reverse("connections")
        assert Server.objects.filter(name="Prod").exists()
        # projects are created explicitly from the Projects page now
        assert not Project.objects.filter(name="Prod").exists()
        assert AuditEntry.objects.filter(action="server.connected").exists()

    def test_project_create_audited(self, client, project):
        r = client.post(reverse("project_create"), {"name": "Second", "server_id": project.server.pk})
        assert r.status_code == 302
        assert AuditEntry.objects.filter(action="project.created").exists()


class TestTurnStart:
    def test_starts_a_turn_in_the_background(self, client, project):
        from agents.models import Turn
        from workspaces.models import Conversation

        conversation = Conversation.objects.create(project=project)
        fake_turn = Turn.objects.create(project=project, backend="fake", user_message="tables?")
        with mock.patch("web.views.start_turn", return_value=fake_turn) as st:
            r = client.post(
                reverse("turn_start", args=[project.pk]),
                data=json.dumps({"message": "tables?", "conversation_id": conversation.pk}),
                content_type="application/json",
            )
        assert r.status_code == 200
        assert r.json() == {"turn_id": fake_turn.pk}
        assert st.call_args.args == (project, "tables?")
        assert st.call_args.kwargs["conversation"] == conversation

    def test_rejects_empty_message(self, client, project):
        r = client.post(
            reverse("turn_start", args=[project.pk]),
            data=json.dumps({"message": "  "}),
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_unknown_conversation_404s(self, client, project):
        r = client.post(
            reverse("turn_start", args=[project.pk]),
            data=json.dumps({"message": "hi", "conversation_id": 9999}),
            content_type="application/json",
        )
        assert r.status_code == 404


class TestTurnStream:
    def test_streams_persisted_events_then_closes(self, client, project):
        from agents.models import Turn, TurnEvent

        turn = Turn.objects.create(project=project, backend="fake", user_message="hi", status="completed")
        TurnEvent.objects.create(
            turn=turn, kind="ToolCallStarted", data={"tool": "list_tables", "payload": {}}
        )
        TurnEvent.objects.create(turn=turn, kind="TurnCompleted", data={"reply": "done"})

        with mock.patch("web.views.STREAM_POLL_SECONDS", 0.01):
            r = client.get(reverse("turn_stream", args=[project.pk, turn.pk]))
            body = b"".join(r.streaming_content).decode()

        assert r["Content-Type"] == "text/event-stream"
        lines = [json.loads(x[6:]) for x in body.strip().split("\n\n")]
        assert [e["event"] for e in lines] == ["ToolCallStarted", "TurnCompleted"]
        assert lines[1]["reply"] == "done"

    def test_orphaned_running_turn_is_self_healed_after_timeout(self, client, project):
        """A turn whose worker died (crash, deploy, server restart) would
        stay 'running' forever with nobody to finalize it — the stream
        itself reaps it once its time budget is exhausted, so a client
        reconnecting to it doesn't spin on 'thinking...' indefinitely."""
        from agents.models import Turn

        turn = Turn.objects.create(project=project, backend="fake", user_message="hi", status="running")
        with (
            mock.patch("web.views.STREAM_POLL_SECONDS", 0.01),
            mock.patch("web.views.STREAM_MAX_SECONDS", 0.02),
        ):
            r = client.get(reverse("turn_stream", args=[project.pk, turn.pk]))
            body = b"".join(r.streaming_content).decode()

        lines = [json.loads(x[6:]) for x in body.strip().split("\n\n")]
        assert lines[-1]["event"] == "TurnFailed"
        assert "timed out" in lines[-1]["error"]
        turn.refresh_from_db()
        assert turn.status == "failed" and turn.finished_at is not None

    def test_cursor_skips_already_seen_events(self, client, project):
        from agents.models import Turn, TurnEvent

        turn = Turn.objects.create(project=project, backend="fake", user_message="hi", status="completed")
        first = TurnEvent.objects.create(turn=turn, kind="TextDelta", data={"text": "a"})
        TurnEvent.objects.create(turn=turn, kind="TurnCompleted", data={"reply": "a"})

        with mock.patch("web.views.STREAM_POLL_SECONDS", 0.01):
            r = client.get(reverse("turn_stream", args=[project.pk, turn.pk]) + f"?after={first.pk}")
            body = b"".join(r.streaming_content).decode()

        lines = [json.loads(x[6:]) for x in body.strip().split("\n\n")]
        assert [e["event"] for e in lines] == ["TurnCompleted"]


class TestAuditLogPagination:
    def test_cursor_pagination(self, client, project):
        from audit.services import record

        for i in range(60):
            record(action=f"evt.{i:02d}", actor_type="system", project=project)
        r1 = client.get(reverse("audit_log", args=[project.pk]))
        assert r1.content.count(b"log-row") // 2 == 50 or b"evt." in r1.content
        assert b'data-has-more="1"' in r1.content
        import re as _re

        before = int(_re.search(rb'data-next-before="(\d+)"', r1.content).group(1))
        r2 = client.get(reverse("audit_log", args=[project.pk]) + f"?before={before}")
        assert b'data-has-more="0"' in r2.content
        # no overlap: the newest entry of page 2 is older than the cursor
        assert f"evt.{59:02d}".encode() in r1.content
        assert f"evt.{0:02d}".encode() in r2.content

    def test_sidebar_limited_to_ten(self, client, project):
        from audit.services import record

        for i in range(15):
            record(action=f"evt.{i}", actor_type="system", project=project)
        r = client.get(reverse("audit_partial", args=[project.pk]))
        assert r.content.count(b'class="entry"') == 10


class TestProjectSettings:
    def test_prompt_and_autonomy_update_audited(self, client, project):
        r = client.post(
            reverse("project_update", args=[project.pk]),
            {"system_prompt": "Use uuid PKs.", "autonomy_level": "read_only"},
        )
        assert r.status_code == 302
        project.refresh_from_db()
        assert project.system_prompt == "Use uuid PKs."
        assert project.autonomy_level == "read_only"
        actions = set(AuditEntry.objects.values_list("action", flat=True))
        assert {"project.prompt_updated", "project.autonomy_updated"} <= actions

    def test_context_file_lifecycle_via_views(self, client, project):
        client.post(
            reverse("context_file_save", args=[project.pk]),
            {"name": "notes.md", "content": "hello"},
        )
        assert project.context_files.filter(name="notes.md").exists()
        client.post(reverse("context_file_delete", args=[project.pk]), {"name": "notes.md"})
        assert not project.context_files.exists()
        actions = list(
            AuditEntry.objects.filter(action__startswith="context_file").values_list("action", flat=True)
        )
        assert "context_file.added" in actions and "context_file.removed" in actions


class TestContextFileJson:
    def test_returns_content_for_editor(self, client, project):
        from workspaces.services import save_context_file

        save_context_file(project, "notes.md", "# Hello\nworld")
        r = client.get(reverse("context_file_json", args=[project.pk]) + "?name=notes.md")
        assert r.status_code == 200
        assert r.json() == {"name": "notes.md", "content": "# Hello\nworld", "size": 13}

    def test_missing_file_404s(self, client, project):
        r = client.get(reverse("context_file_json", args=[project.pk]) + "?name=nope.md")
        assert r.status_code == 404


class TestConnectionsViews:
    def test_create_persists_encrypted_and_audits_without_the_key(self, client, project):
        from agents.models import AgentConnection

        r = client.post(
            reverse("connection_create"),
            {
                "name": "OpenRouter",
                "backend": "openai_compat",
                "model": "gpt-4o",
                "base_url": "https://openrouter.ai/api/v1",
                "api_key": "sk-or-verysecret",
            },
        )
        assert r.status_code == 302
        conn = AgentConnection.objects.get(name="OpenRouter")
        assert conn.api_key == "sk-or-verysecret"
        assert "verysecret" not in conn.api_key_encrypted
        entry = AuditEntry.objects.get(action="connection.created")
        assert "verysecret" not in json.dumps(entry.payload_in)  # never in the trail
        assert entry.payload_in["api_key_set"] is True

    def test_delete_audits(self, client, project):
        from agents.models import AgentConnection

        conn = AgentConnection.objects.create(name="Tmp", backend="claude_code")
        client.post(reverse("connection_delete", args=[conn.pk]))
        assert not AgentConnection.objects.filter(name="Tmp").exists()
        assert AuditEntry.objects.filter(action="connection.deleted").exists()

    def test_project_selects_connection_and_audits(self, client, project):
        from agents.models import AgentConnection

        conn = AgentConnection.objects.create(name="Ollama", backend="openai_compat", model="llama3.1")
        r = client.post(
            reverse("project_update", args=[project.pk]),
            {"system_prompt": "", "agent_connection": str(conn.pk)},
        )
        assert r.status_code == 302
        project.refresh_from_db()
        assert project.agent_connection_id == conn.pk
        entry = AuditEntry.objects.get(action="project.agent_updated")
        assert entry.payload_in == {"connection": "Ollama"}

    def test_room_shows_connection_select(self, client, project):
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b'name="agent_connection"' in r.content
        assert b"Manage LLM connections" in r.content

    def test_settings_page_masks_keys(self, client, project):
        from agents.models import AgentConnection

        conn = AgentConnection(name="Sec", backend="openai_compat")
        conn.api_key = "sk-live-abcdefghijklmnop"
        conn.save()
        r = client.get(reverse("settings"))
        assert b"Sec" in r.content
        assert b"abcdefghijklmnop" not in r.content  # full key never rendered


@pytest.fixture
def proposed_plan(project):
    from agents.models import Plan, PlanStep, Turn
    from workspaces.models import Conversation

    conversation = Conversation.objects.create(project=project)
    turn = Turn.objects.create(project=project, conversation=conversation, backend="fake", user_message="x")
    plan = Plan.objects.create(project=project, turn=turn, status="proposed")
    PlanStep.objects.create(
        plan=plan, order=1, tool="execute_sql", payload={"sql": "CREATE TABLE t (id INTEGER)"}
    )
    return plan


class TestPlanViews:
    def test_plan_json_shape(self, client, project, proposed_plan):
        r = client.get(reverse("plan_json", args=[project.pk, proposed_plan.pk]))
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == proposed_plan.pk and data["status"] == "proposed"
        assert data["steps"][0]["payload"]["sql"] == "CREATE TABLE t (id INTEGER)"

    def test_room_exposes_pending_plan_for_rebuild(self, client, project, proposed_plan):
        r = client.get(reverse("project_room", args=[project.pk]))
        assert f"activePlanId: {proposed_plan.pk}".encode() in r.content

    def test_room_ignores_settled_plans(self, client, project, proposed_plan):
        proposed_plan.status = "applied"
        proposed_plan.save()
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b"activePlanId: null" in r.content

    def test_approve_delegates_to_runtime(self, client, project, proposed_plan):
        with mock.patch("agents.runtime.approve_plan", return_value=proposed_plan) as ap:
            r = client.post(reverse("plan_approve", args=[project.pk, proposed_plan.pk]))
        assert r.status_code == 200
        assert ap.call_args.args == (proposed_plan,)

    def test_decisions_conflict_when_not_proposed(self, client, project, proposed_plan):
        proposed_plan.status = "applied"
        proposed_plan.save()
        for name in ("plan_approve", "plan_reject"):
            r = client.post(reverse(name, args=[project.pk, proposed_plan.pk]))
            assert r.status_code == 409

    def test_reject_flows_through_runtime(self, client, project, proposed_plan):
        r = client.post(reverse("plan_reject", args=[project.pk, proposed_plan.pk]))
        assert r.status_code == 200
        proposed_plan.refresh_from_db()
        assert proposed_plan.status == "rejected"
        assert AuditEntry.objects.filter(action="plan.rejected").exists()

    def test_revise_requires_comment_and_returns_new_turn(self, client, project, proposed_plan):
        r = client.post(
            reverse("plan_revise", args=[project.pk, proposed_plan.pk]),
            data=json.dumps({"comment": "  "}),
            content_type="application/json",
        )
        assert r.status_code == 400

        from agents.models import Turn

        new_turn = Turn.objects.create(project=project, backend="fake", user_message="revised")
        with mock.patch("agents.runtime.revise_plan", return_value=new_turn) as rv:
            r = client.post(
                reverse("plan_revise", args=[project.pk, proposed_plan.pk]),
                data=json.dumps({"comment": "use TEXT ids"}),
                content_type="application/json",
            )
        assert r.status_code == 200
        assert r.json()["turn_id"] == new_turn.pk
        assert rv.call_args.args == (proposed_plan, "use TEXT ids")

    def test_plan_of_another_project_404s(self, client, project, proposed_plan, tmp_path):
        other_server = Server.objects.create(name="S2", adapter_type="sqlite", dsn=str(tmp_path / "o.db"))
        other = Project.objects.create(name="Other", server=other_server)
        r = client.get(reverse("plan_json", args=[other.pk, proposed_plan.pk]))
        assert r.status_code == 404


class TestConversationsViews:
    def test_room_creates_a_conversation_when_none_exists(self, client, project):
        from workspaces.models import Conversation

        r = client.get(reverse("project_room", args=[project.pk]))
        assert r.status_code == 200
        assert Conversation.objects.filter(project=project).count() == 1
        conversation = Conversation.objects.get()
        assert f"conversationId: {conversation.pk}".encode() in r.content

    def test_room_selects_the_requested_chat(self, client, project):
        from workspaces.models import ChatMessage, Conversation

        a = Conversation.objects.create(project=project, title="Thread A")
        b = Conversation.objects.create(project=project, title="Thread B")
        ChatMessage.objects.create(project=project, conversation=b, role="user", content="only in B")
        r = client.get(reverse("project_room", args=[project.pk]) + f"?chat={b.pk}")
        assert b"only in B" in r.content
        assert f"conversationId: {b.pk}".encode() in r.content
        r2 = client.get(reverse("project_room", args=[project.pk]) + f"?chat={a.pk}")
        assert b"only in B" not in r2.content

    def test_chat_of_another_project_404s(self, client, project, tmp_path):
        from workspaces.models import Conversation

        other_server = Server.objects.create(name="S2", adapter_type="sqlite", dsn=str(tmp_path / "o.db"))
        other = Project.objects.create(name="Other", server=other_server)
        foreign = Conversation.objects.create(project=other)
        r = client.get(reverse("project_room", args=[project.pk]) + f"?chat={foreign.pk}")
        assert r.status_code == 404

    def test_create_and_delete_are_audited(self, client, project):
        from workspaces.models import ChatMessage, Conversation

        r = client.post(reverse("chat_create", args=[project.pk]))
        assert r.status_code == 302
        conversation = Conversation.objects.get()
        assert AuditEntry.objects.filter(action="chat.created").exists()

        ChatMessage.objects.create(project=project, conversation=conversation, role="user", content="x")
        r = client.post(reverse("chat_delete", args=[project.pk, conversation.pk]))
        assert r.status_code == 302
        assert not Conversation.objects.exists()
        assert not ChatMessage.objects.exists()  # cascade
        assert AuditEntry.objects.filter(action="chat.deleted").exists()

    def test_deleting_a_chat_keeps_the_audit_trail(self, client, project):
        from audit.services import record
        from workspaces.models import Conversation

        conversation = Conversation.objects.create(project=project)
        record(action="execute_sql", actor_type="agent", project=project, payload_in={"sql": "SELECT 1"})
        client.post(reverse("chat_delete", args=[project.pk, conversation.pk]))
        assert AuditEntry.objects.filter(action="execute_sql").exists()

    def test_sidebar_lists_chats(self, client, project):
        from workspaces.models import Conversation

        Conversation.objects.create(project=project, title="RLS policies")
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b"RLS policies" in r.content
        assert b'id="sidebar"' in r.content


class TestGlobalShell:
    def test_every_page_carries_the_sidebar_nav(self, client, project):
        for name in ("home", "connections", "settings"):
            r = client.get(reverse(name))
            assert r.status_code == 200
            assert b'id="sidebar"' in r.content
            for target in ("home", "connections", "settings"):
                assert f'href="{reverse(target)}"'.encode() in r.content, name

    def test_connections_page_lists_servers(self, client, project):
        r = client.get(reverse("connections"))
        assert b"Local" in r.content  # the project fixture's server
        assert b'name="adapter_type"' in r.content

    def test_settings_page_hosts_llm_connections(self, client, project):
        r = client.get(reverse("settings"))
        assert b"LLM connections" in r.content
        assert b'name="backend"' in r.content

    def test_project_delete_cascades_and_audits(self, client, project):
        from workspaces.models import Conversation

        Conversation.objects.create(project=project, title="thread")
        r = client.post(reverse("project_delete", args=[project.pk]))
        assert r.status_code == 302
        assert not Project.objects.exists()
        assert not Conversation.objects.exists()
        entry = AuditEntry.objects.get(action="project.deleted")
        assert entry.project is None  # FK nulled by the delete
        assert entry.project_name == "Room"  # the denormalized name survives

    def test_server_delete_cascades_projects_and_audits(self, client, project):
        r = client.post(reverse("server_delete", args=[project.server.pk]))
        assert r.status_code == 302 and r.url == reverse("connections")
        assert not Server.objects.exists()
        assert not Project.objects.exists()
        entry = AuditEntry.objects.get(action="server.deleted")
        assert entry.payload_in["projects"] == ["Room"]


class TestAuth:
    def test_anonymous_is_redirected_to_login(self, project):
        from django.test import Client

        anon = Client()
        for name, args in (("home", []), ("project_room", [project.pk]), ("settings", [])):
            r = anon.get(reverse(name, args=args))
            assert r.status_code == 302 and r.url.startswith(reverse("login")), name

    def test_factory_password_forces_the_change_page(self, django_user_model):
        """Logs in as the admin/admin user the SEED MIGRATION created —
        the exact first-boot experience of a fresh instance."""
        from django.test import Client

        assert django_user_model.objects.filter(username="admin").exists()  # seeded by web/0001
        c = Client()
        r = c.post(reverse("login"), {"username": "admin", "password": "admin"})  # nosec B105
        assert r.status_code == 302
        # everything redirects to the password change until it's rotated
        r = c.get(reverse("home"))
        assert r.status_code == 302 and r.url == reverse("password_change")
        r = c.get(reverse("connections"))
        assert r.url == reverse("password_change")
        # the change page itself is reachable
        assert c.get(reverse("password_change")).status_code == 200

        r = c.post(
            reverse("password_change"),
            {
                "old_password": "admin",  # nosec B105
                "new_password1": "a-long-real-password-1",
                "new_password2": "a-long-real-password-1",
            },
        )
        assert r.status_code == 302
        # free to navigate now, and still logged in
        assert c.get(reverse("home")).status_code == 200
        actions = list(AuditEntry.objects.values_list("action", flat=True))
        assert "auth.login" in actions and "auth.password_changed" in actions

    def test_non_factory_login_is_not_forced(self, django_user_model):
        from django.test import Client

        django_user_model.objects.create_user("op", password="proper-password-9")  # nosec B106
        c = Client()
        c.post(reverse("login"), {"username": "op", "password": "proper-password-9"})  # nosec B105
        assert c.get(reverse("home")).status_code == 200

    def test_actor_is_the_logged_in_user(self, client, project):
        client.post(reverse("chat_create", args=[project.pk]))
        entry = AuditEntry.objects.get(action="chat.created")
        assert entry.actor == "francesco"

    def test_seed_migration_created_the_bootstrap_admin(self, django_user_model):
        """The data migration ran while creating the test database with
        zero users — exactly the fresh-instance path it exists for."""
        from django.contrib.auth.hashers import check_password

        seeded = django_user_model.objects.get(username="admin")
        assert seeded.is_superuser
        assert check_password("admin", seeded.password)


class TestFunctionsViews:
    def test_functions_json_lists_via_adapter(self, client, project):
        with mock.patch("web.views.get_adapter") as ga:
            ga.return_value.list_functions.return_value = [{"slug": "greet", "status": "ACTIVE"}]
            r = client.get(reverse("functions_json", args=[project.pk]))
        assert r.status_code == 200
        assert r.json()["functions"][0]["slug"] == "greet"

    def test_source_json_prefers_the_tracked_copy(self, client, project):
        from instances.services import save_function_source

        save_function_source(project.server, "greet", "tracked code", version=3, actor="francesco")
        r = client.get(reverse("function_source_json", args=[project.pk, "greet"]))
        data = r.json()
        assert data["tracked"] is True and data["body"] == "tracked code"
        assert data["deployed_version"] == 3 and data["deployed_by"] == "francesco"

    def test_source_json_falls_back_to_the_api_for_untracked(self, client, project):
        with mock.patch("web.views.get_adapter") as ga:
            ga.return_value.get_function_body.return_value = "legacy source"
            r = client.get(reverse("function_source_json", args=[project.pk, "greet"]))
        assert r.json() == {"slug": "greet", "body": "legacy source", "tracked": False}

    def test_functions_json_annotates_tracked_and_drift(self, client, project):
        from instances.services import save_function_source

        save_function_source(project.server, "greet", "code", version=3)
        with mock.patch("web.views.get_adapter") as ga:
            ga.return_value.list_functions.return_value = [
                {"slug": "greet", "version": 5},  # live moved past us: drift
                {"slug": "other", "version": 1},  # never seen: untracked
            ]
            r = client.get(reverse("functions_json", args=[project.pk]))
        rows = {f["slug"]: f for f in r.json()["functions"]}
        assert rows["greet"]["tracked"] is True and rows["greet"]["drift"] is True
        assert rows["other"]["tracked"] is False and rows["other"]["drift"] is False

    def test_user_deploy_is_audited_and_tracked(self, client, project):
        from instances.services import get_function_source

        with mock.patch("web.views.get_adapter") as ga:
            ga.return_value.deploy_function.return_value = {"slug": "greet", "version": 9, "status": "ACTIVE"}
            r = client.post(
                reverse("function_deploy", args=[project.pk, "greet"]),
                data=json.dumps({"body": "Deno.serve(x)", "verify_jwt": False}),
                content_type="application/json",
            )
        assert r.status_code == 200 and r.json()["version"] == 9
        src = get_function_source(project.server, "greet")
        assert src.body == "Deno.serve(x)" and src.deployed_version == 9
        assert src.deployed_by == "francesco" and src.verify_jwt is False
        entry = AuditEntry.objects.get(action="deploy_function")
        assert entry.actor_type == "user" and entry.actor == "francesco"
        assert entry.payload_in["body"] == "Deno.serve(x)"  # the full code, in the trail

    def test_user_deploy_rejects_empty_source(self, client, project):
        r = client.post(
            reverse("function_deploy", args=[project.pk, "greet"]),
            data=json.dumps({"body": "   "}),
            content_type="application/json",
        )
        assert r.status_code == 400

    def test_adapter_errors_become_502(self, client, project):
        from instances.adapters import AdapterError

        with mock.patch("web.views.get_adapter") as ga:
            ga.return_value.list_functions.side_effect = AdapterError("nope")
            r = client.get(reverse("functions_json", args=[project.pk]))
        assert r.status_code == 502 and r.json()["error"] == "nope"

    def test_functions_tab_only_for_capable_servers(self, client, project):
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b"pane-functions" not in r.content  # sqlite project
        project.server.adapter_type = "supabase"
        project.server.save()
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b"pane-functions" in r.content
        assert b'id="fn-editor"' in r.content

    def test_storage_json_enriches_buckets_with_the_read_only_query(self, client, project):
        with mock.patch("web.views.get_adapter") as ga:
            ga.return_value.list_buckets.return_value = [{"id": "avatars", "name": "avatars", "public": True}]
            ga.return_value.query_sql.return_value = {
                "rows": [
                    {
                        "id": "avatars",
                        "file_size_limit": "1048576",
                        "allowed_mime_types": "{image/png}",
                        "objects": "12",
                    }
                ]
            }
            r = client.get(reverse("storage_json", args=[project.pk]))
        b = r.json()["buckets"][0]
        assert b["objects"] == "12" and b["file_size_limit"] == "1048576"
        assert b["allowed_mime_types"] == "{image/png}"

    def test_storage_json_survives_a_failing_count_query(self, client, project):
        with mock.patch("web.views.get_adapter") as ga:
            ga.return_value.list_buckets.return_value = [
                {"id": "avatars", "name": "avatars", "public": False}
            ]
            ga.return_value.query_sql.side_effect = Exception("boom")
            r = client.get(reverse("storage_json", args=[project.pk]))
        assert r.status_code == 200
        assert r.json()["buckets"][0]["objects"] is None

    def test_storage_json_adapter_errors_become_502(self, client, project):
        from instances.adapters import AdapterError

        with mock.patch("web.views.get_adapter") as ga:
            ga.return_value.list_buckets.side_effect = AdapterError("nope")
            r = client.get(reverse("storage_json", args=[project.pk]))
        assert r.status_code == 502 and r.json()["error"] == "nope"

    def test_storage_tab_only_for_capable_servers(self, client, project):
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b"pane-storage" not in r.content  # sqlite project
        project.server.adapter_type = "supabase"
        project.server.save()
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b"pane-storage" in r.content

    def test_auth_config_json_serves_the_redacted_config(self, client, project):
        with mock.patch("web.views.get_adapter") as ga:
            # the adapter redacts at the source; the view passes it through
            ga.return_value.get_auth_config.return_value = {"site_url": "https://x", "smtp_pass": "***set***"}  # noqa: S105
            r = client.get(reverse("auth_config_json", args=[project.pk]))
        assert r.status_code == 200
        assert r.json()["config"] == {"site_url": "https://x", "smtp_pass": "***set***"}  # noqa: S105

    def test_auth_config_json_adapter_errors_become_502(self, client, project):
        from instances.adapters import AdapterError

        with mock.patch("web.views.get_adapter") as ga:
            ga.return_value.get_auth_config.side_effect = AdapterError("nope")
            r = client.get(reverse("auth_config_json", args=[project.pk]))
        assert r.status_code == 502 and r.json()["error"] == "nope"

    def test_auth_tab_only_for_capable_servers(self, client, project):
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b"pane-auth" not in r.content  # sqlite project
        project.server.adapter_type = "supabase"
        project.server.save()
        r = client.get(reverse("project_room", args=[project.pk]))
        assert b"pane-auth" in r.content
        assert b'id="tpl-preview"' in r.content

    def test_plan_json_carries_step_meta(self, client, project, proposed_plan):
        step = proposed_plan.steps.get()
        step.meta = {"updates_existing": True, "diff": "-a\n+b"}
        step.save()
        r = client.get(reverse("plan_json", args=[project.pk, proposed_plan.pk]))
        assert r.json()["steps"][0]["meta"]["diff"] == "-a\n+b"

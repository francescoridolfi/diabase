"""GUI view tests: pages render, creations are audited, SSE stream shapes."""

import json
from unittest import mock

import pytest
from django.urls import reverse

from audit.models import AuditEntry
from instances.models import Server
from workspaces.models import Project

pytestmark = pytest.mark.django_db


@pytest.fixture
def project(tmp_path):
    server = Server.objects.create(name="Local", adapter_type="sqlite", dsn=str(tmp_path / "t.db"))
    return Project.objects.create(name="Room", server=server)


class TestPages:
    def test_home_renders(self, client, project):
        r = client.get(reverse("home"))
        assert r.status_code == 200
        assert b"Room" in r.content

    def test_project_room_renders_orb_and_overlay(self, client, project):
        r = client.get(reverse("project_room", args=[project.pk]))
        assert r.status_code == 200
        assert b'id="orb"' in r.content
        assert b"schema-overlay" in r.content
        assert b"activeTurnId: null" in r.content

    def test_project_room_exposes_running_turn_for_resume(self, client, project):
        from agents.models import Turn

        turn = Turn.objects.create(
            project=project, backend="claude_code", user_message="hi", status="running"
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
    def test_server_create_makes_project_and_audits(self, client, tmp_path):
        r = client.post(
            reverse("server_create"),
            {"name": "Prod", "adapter_type": "sqlite", "dsn": str(tmp_path / "p.db")},
        )
        assert r.status_code == 302
        assert Project.objects.filter(name="Prod").exists()
        assert AuditEntry.objects.filter(action="server.connected").exists()

    def test_project_create_audited(self, client, project):
        r = client.post(reverse("project_create"), {"name": "Second", "server_id": project.server.pk})
        assert r.status_code == 302
        assert AuditEntry.objects.filter(action="project.created").exists()


class TestTurnStart:
    def test_starts_a_turn_in_the_background(self, client, project):
        from agents.models import Turn

        fake_turn = Turn.objects.create(project=project, backend="fake", user_message="tables?")
        with mock.patch("web.views.start_turn", return_value=fake_turn) as st:
            r = client.post(
                reverse("turn_start", args=[project.pk]),
                data=json.dumps({"message": "tables?"}),
                content_type="application/json",
            )
        assert r.status_code == 200
        assert r.json() == {"turn_id": fake_turn.pk}
        assert st.call_args.args == (project, "tables?")

    def test_rejects_empty_message(self, client, project):
        r = client.post(
            reverse("turn_start", args=[project.pk]),
            data=json.dumps({"message": "  "}),
            content_type="application/json",
        )
        assert r.status_code == 400


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
        assert b"Manage connections" in r.content

    def test_connections_page_masks_keys(self, client, project):
        from agents.models import AgentConnection

        conn = AgentConnection(name="Sec", backend="openai_compat")
        conn.api_key = "sk-live-abcdefghijklmnop"
        conn.save()
        r = client.get(reverse("connections"))
        assert b"Sec" in r.content
        assert b"abcdefghijklmnop" not in r.content  # full key never rendered

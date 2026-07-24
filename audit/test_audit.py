"""Audit trail tests: immutability guarantees and automatic tool-call recording."""

import pytest

from audit.models import AuditEntry
from audit.services import AuditedAdapter, record
from instances.adapters import AdapterError, SQLiteAdapter
from instances.models import Server
from workspaces.models import Project

pytestmark = pytest.mark.django_db


@pytest.fixture
def project(tmp_path):
    server = Server.objects.create(name="Local test", adapter_type="sqlite", dsn=str(tmp_path / "t.db"))
    return Project.objects.create(name="Test project", server=server)


@pytest.fixture
def audited(project):
    return AuditedAdapter(SQLiteAdapter(project.server.dsn), project=project, actor="claude-test")


class TestAppendOnly:
    def test_existing_row_cannot_be_saved(self, project):
        entry = record(action="project.created", actor_type="user", actor="francesco", project=project)
        entry.action = "tampered"
        with pytest.raises(TypeError, match="append-only"):
            entry.save()

    def test_row_cannot_be_deleted(self, project):
        entry = record(action="x", actor_type="system")
        with pytest.raises(TypeError, match="append-only"):
            entry.delete()

    def test_queryset_update_blocked(self, project):
        record(action="x", actor_type="system")
        with pytest.raises(TypeError, match="append-only"):
            AuditEntry.objects.all().update(action="tampered")

    def test_queryset_delete_blocked(self, project):
        record(action="x", actor_type="system")
        with pytest.raises(TypeError, match="append-only"):
            AuditEntry.objects.all().delete()


class TestRecord:
    def test_denormalizes_project_context(self, project):
        entry = record(action="chat.message", actor_type="user", actor="francesco", project=project)
        assert entry.project_name == "Test project"
        assert entry.server_name == "Local test"
        assert entry.adapter_type == "sqlite"

    def test_survives_project_deletion(self, project):
        entry = record(action="project.created", actor_type="user", project=project)
        project.delete()
        entry.refresh_from_db()
        assert entry.project is None
        assert entry.project_name == "Test project"  # denormalized context remains


class TestAuditedAdapter:
    def test_success_records_full_payloads(self, audited, project):
        audited.execute_sql("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        audited.execute_sql("INSERT INTO t (id) VALUES (42)")
        audited.execute_sql("SELECT * FROM t")

        entries = list(AuditEntry.objects.filter(action="execute_sql").order_by("created_at"))
        assert len(entries) == 3
        assert all(e.outcome == "success" for e in entries)
        assert all(e.actor_type == "agent" and e.actor == "claude-test" for e in entries)
        assert entries[0].payload_in == {"sql": "CREATE TABLE t (id INTEGER PRIMARY KEY)"}
        # full output stored (GDPR policy: issue #1)
        assert entries[2].payload_out["rows"] == [{"id": 42}]

    def test_error_records_and_reraises(self, audited):
        with pytest.raises(AdapterError):
            audited.describe_table("missing")
        entry = AuditEntry.objects.get(action="describe_table")
        assert entry.outcome == "error"
        assert "does not exist" in entry.error
        assert entry.payload_in == {"table": "missing"}

    def test_get_schema_records_each_underlying_call(self, audited):
        audited.execute_sql("CREATE TABLE a (id INTEGER)")
        before = AuditEntry.objects.count()
        audited.get_schema()
        after = AuditEntry.objects.count()
        assert after == before + 2  # one list_tables + one describe_table

    def test_get_advisors_is_recorded_with_its_kind(self, project):
        class FakeAdapter:
            capabilities = frozenset({"advisors"})

            def get_advisors(self, kind):
                return [{"name": "rls_disabled_in_public", "level": "ERROR"}]

        audited = AuditedAdapter(FakeAdapter(), project=project, actor="claude-test")
        out = audited.get_advisors("security")
        assert out[0]["name"] == "rls_disabled_in_public"
        entry = AuditEntry.objects.get(action="get_advisors")
        assert entry.payload_in == {"kind": "security"}
        assert entry.payload_out["result"][0]["level"] == "ERROR"

    def test_auth_config_update_payload_is_redacted_in_the_trail(self, project):
        """Defense in depth: the adapter refuses secret writes, but even
        the ATTEMPT must not land in the audit trail with a readable value."""

        class FakeAdapter:
            capabilities = frozenset({"auth_config"})

            def update_auth_config(self, changes):
                raise AdapterError("Refusing to set secret keys through Diabase: smtp_pass")

        audited = AuditedAdapter(FakeAdapter(), project=project, actor="claude-test")
        with pytest.raises(AdapterError):
            audited.update_auth_config({"smtp_pass": "hunter2", "site_url": "https://x"})  # nosec B105
        entry = AuditEntry.objects.get(action="update_auth_config")
        assert entry.outcome == "error"
        assert entry.payload_in == {"changes": {"smtp_pass": "***set***", "site_url": "https://x"}}  # noqa: S105 # nosec B105


def _backdate(entry, days):
    """Test-only: created_at is auto_now_add and the model is append-only,
    so age an entry by calling the BASE QuerySet.update directly — the
    exact bypass production code must never take."""
    from datetime import timedelta

    from django.db import models
    from django.utils import timezone

    models.QuerySet.update(
        AuditEntry.objects.filter(pk=entry.pk), created_at=timezone.now() - timedelta(days=days)
    )


class TestRetention:
    def test_redaction_tombstones_payloads_but_keeps_the_row(self, project):
        from audit.services import apply_retention

        old = record(
            action="execute_sql",
            actor_type="agent",
            actor="claude",
            project=project,
            payload_in={"sql": "INSERT INTO users VALUES ('mario@rossi.it')"},
            payload_out={"rows_affected": 1},
        )
        _backdate(old, days=40)
        fresh = record(action="execute_sql", actor_type="agent", payload_in={"sql": "SELECT 1"})

        assert apply_retention(actor="francesco", days=30) == 1
        old.refresh_from_db()
        fresh.refresh_from_db()
        assert old.payload_in == {"redacted": True, "reason": "retention"}
        assert old.payload_out == {"redacted": True, "reason": "retention"}
        assert old.redacted_at is not None
        # the who/what/when survives
        assert old.action == "execute_sql" and old.actor == "claude" and old.outcome == "success"
        # inside the window: untouched
        assert fresh.payload_in == {"sql": "SELECT 1"} and fresh.redacted_at is None
        # the run itself is in the trail
        run = AuditEntry.objects.get(action="audit.redacted")
        assert run.actor == "francesco"
        assert run.payload_in == {"reason": "retention", "days": 30}
        assert run.payload_out == {"rows": 1}

    def test_zero_window_is_a_silent_noop(self, project):
        from audit.services import apply_retention

        e = record(action="x", actor_type="system", payload_in={"a": 1})
        _backdate(e, days=400)
        assert apply_retention(days=0) == 0
        assert apply_retention() == 0  # stored policy defaults to 0 = forever
        e.refresh_from_db()
        assert e.payload_in == {"a": 1}
        assert not AuditEntry.objects.filter(action="audit.redacted").exists()

    def test_redaction_is_idempotent(self, project):
        from audit.services import apply_retention

        e = record(action="x", actor_type="system", payload_in={"a": 1})
        _backdate(e, days=40)
        assert apply_retention(days=30) == 1
        assert apply_retention(days=30) == 0  # already-tombstoned rows are skipped
        e.refresh_from_db()
        assert e.payload_in == {"redacted": True, "reason": "retention"}

    def test_stored_policy_drives_the_window(self, project):
        from audit.models import RetentionPolicy
        from audit.services import apply_retention

        policy = RetentionPolicy.load()
        policy.audit_payload_days = 30
        policy.save()
        e = record(action="x", actor_type="system", payload_in={"a": 1})
        _backdate(e, days=40)
        assert apply_retention() == 1


class TestErasure:
    def test_needle_matches_in_and_out_and_error(self, project):
        from audit.services import erase_payloads

        hit_in = record(action="a", actor_type="agent", payload_in={"sql": "…Mario@Rossi.it…"})
        hit_out = record(action="b", actor_type="agent", payload_out={"rows": [{"email": "mario@rossi.it"}]})
        hit_err = record(action="c", actor_type="agent", outcome="error", error="dup mario@rossi.it")
        miss = record(action="d", actor_type="agent", payload_in={"sql": "SELECT 1"})

        assert erase_payloads(needle="mario@rossi.it", actor="francesco") == 3
        for e in (hit_in, hit_out, hit_err):
            e.refresh_from_db()
            assert e.payload_in == {"redacted": True, "reason": "erasure"}
            assert e.error == ""
        miss.refresh_from_db()
        assert miss.redacted_at is None

    def test_the_needle_never_lands_in_the_trail(self, project):
        from audit.services import erase_payloads

        record(action="a", actor_type="agent", payload_in={"x": "mario@rossi.it"})
        erase_payloads(needle="mario@rossi.it", actor="francesco")
        run = AuditEntry.objects.get(action="audit.redacted")
        assert "mario" not in str(run.payload_in) and "mario" not in str(run.payload_out)
        assert run.payload_in == {"reason": "erasure", "scope": "needle"}

    def test_project_scope(self, project, tmp_path):
        from audit.services import erase_payloads

        other_server = Server.objects.create(name="Other", adapter_type="sqlite", dsn=str(tmp_path / "o.db"))
        other = Project.objects.create(name="Other project", server=other_server)
        mine = record(action="a", actor_type="agent", project=project, payload_in={"x": "mario"})
        theirs = record(action="a", actor_type="agent", project=other, payload_in={"x": "mario"})

        assert erase_payloads(needle="mario", project=project, actor="francesco") == 1
        mine.refresh_from_db()
        theirs.refresh_from_db()
        assert mine.redacted_at is not None and theirs.redacted_at is None

    def test_requires_a_criterion(self):
        from audit.services import erase_payloads

        with pytest.raises(ValueError, match="needle"):
            erase_payloads()


class TestPurgeCommand:
    def test_retention_mode(self, project):
        from django.core.management import call_command

        e = record(action="x", actor_type="system", payload_in={"a": 1})
        _backdate(e, days=40)
        call_command("purge_audit_payloads", days=30)
        e.refresh_from_db()
        assert e.redacted_at is not None

    def test_erasure_mode(self, project):
        from django.core.management import call_command

        e = record(action="x", actor_type="system", project=project, payload_in={"x": "mario"})
        call_command("purge_audit_payloads", erase_contains="mario", project=project.pk)
        e.refresh_from_db()
        assert e.redacted_at is not None

    def test_unknown_project_errors(self):
        from django.core.management import CommandError, call_command

        with pytest.raises(CommandError, match="No project"):
            call_command("purge_audit_payloads", erase_contains="x", project=99999)

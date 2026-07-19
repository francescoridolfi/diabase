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

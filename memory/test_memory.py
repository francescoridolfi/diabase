"""Memory index tests: signal-driven indexing, schema cards, retrieval,
and the GDPR guarantee that derived chunks die with redacted payloads."""

import pytest

from audit.services import apply_retention, erase_payloads, record
from instances.models import Server
from memory.models import MemoryChunk
from memory.services import index_schema, reindex_project, search, stats
from workspaces.models import Conversation, Project
from workspaces.services import delete_context_file, save_context_file

pytestmark = pytest.mark.django_db


@pytest.fixture
def project(tmp_path):
    server = Server.objects.create(name="Local", adapter_type="sqlite", dsn=str(tmp_path / "m.db"))
    return Project.objects.create(name="Mem", server=server)


def chunks(project, source=None):
    qs = MemoryChunk.objects.filter(project=project)
    return qs.filter(source_type=source) if source else qs


class TestAuditIndexing:
    def test_write_actions_are_indexed(self, project):
        record(
            action="execute_sql",
            actor_type="agent",
            actor="claude",
            project=project,
            payload_in={"sql": "CREATE INDEX idx_orders_user ON orders(user_id)"},
        )
        chunk = chunks(project, "audit").get()
        assert "idx_orders_user" in chunk.text
        assert chunk.source_ref.startswith("audit:")

    def test_reads_and_recall_are_not_indexed(self, project):
        for action in ("list_tables", "query_sql", "recall", "read_context_file"):
            record(action=action, actor_type="agent", project=project, payload_in={"q": "x"})
        assert chunks(project, "audit").count() == 0

    def test_entries_without_project_are_skipped(self):
        record(action="connection.created", actor_type="user", payload_in={"name": "x"})
        assert MemoryChunk.objects.count() == 0


class TestGDPRConsistency:
    def test_erasure_purges_the_derived_chunks(self, project):
        record(
            action="execute_sql",
            actor_type="agent",
            project=project,
            payload_in={"sql": "INSERT INTO users VALUES ('mario@rossi.it')"},
        )
        assert "mario@rossi.it" in chunks(project, "audit").get().text
        erase_payloads(needle="mario@rossi.it", actor="francesco")
        assert chunks(project, "audit").count() == 0

    def test_retention_purges_the_derived_chunks(self, project):
        from audit.test_audit import _backdate

        entry = record(
            action="execute_sql", actor_type="agent", project=project, payload_in={"sql": "DROP TABLE x"}
        )
        _backdate(entry, days=40)
        assert chunks(project, "audit").count() == 1
        apply_retention(days=30)
        assert chunks(project, "audit").count() == 0


class TestChatIndexing:
    def test_messages_index_and_die_with_the_chat(self, project):
        from workspaces.models import ChatMessage

        conversation = Conversation.objects.create(project=project, title="Avatars bucket")
        ChatMessage.objects.create(
            project=project, conversation=conversation, role="user", content="create an avatars bucket"
        )
        chunk = chunks(project, "chat").get()
        assert "avatars bucket" in chunk.text and "Avatars bucket" in chunk.title
        conversation.delete()  # cascade fires the message post_delete
        assert chunks(project, "chat").count() == 0


class TestContextIndexing:
    def test_sections_split_on_headings(self, project):
        save_context_file(project, "conv.md", "# Naming\nuuid PKs everywhere\n# Testing\npytest only\n")
        refs = sorted(chunks(project, "context").values_list("source_ref", flat=True))
        assert refs == ["file:conv.md#0", "file:conv.md#1"]
        naming = chunks(project, "context").get(source_ref="file:conv.md#0")
        assert "uuid PKs" in naming.text and naming.title == "conv.md — Naming"

    def test_shrinking_a_file_drops_stale_sections(self, project):
        save_context_file(project, "c.md", "# A\naaa\n# B\nbbb\n")
        assert chunks(project, "context").count() == 2
        save_context_file(project, "c.md", "# A\naaa\n")
        assert chunks(project, "context").count() == 1

    def test_deleting_the_file_drops_all_its_chunks(self, project):
        save_context_file(project, "gone.md", "# X\ncontent\n")
        delete_context_file(project, "gone.md")
        assert chunks(project, "context").count() == 0


class TestSchemaCards:
    def _make_schema(self, project):
        from instances.adapters import SQLiteAdapter

        adapter = SQLiteAdapter(project.server.dsn)
        adapter.execute_sql("CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT NOT NULL)")
        adapter.execute_sql(
            "CREATE TABLE orders (id INTEGER PRIMARY KEY, "
            "user_id INTEGER NOT NULL REFERENCES users(id), total REAL)"
        )
        adapter.execute_sql("CREATE INDEX idx_orders_user ON orders(user_id)")

    def test_cards_carry_columns_both_fk_directions_and_indexes(self, project):
        self._make_schema(project)
        index_schema(project)
        cards = {c.source_ref: c.text for c in chunks(project, "schema")}
        assert set(cards) == {"table:users", "table:orders"}
        assert "user_id INTEGER NOT NULL -> users.id" in cards["table:orders"]
        assert "Referenced by: orders.user_id -> users.id" in cards["table:users"]
        assert "idx_orders_user" in cards["table:orders"]

    def test_dropped_tables_lose_their_cards(self, project):
        from instances.adapters import SQLiteAdapter

        self._make_schema(project)
        index_schema(project)
        SQLiteAdapter(project.server.dsn).execute_sql("DROP TABLE orders")
        index_schema(project)
        assert list(chunks(project, "schema").values_list("source_ref", flat=True)) == ["table:users"]

    def test_reindex_is_hash_idempotent(self, project):
        self._make_schema(project)
        assert index_schema(project) == 2
        assert index_schema(project) == 0  # unchanged content: no churn


class TestSearch:
    def test_ranks_the_relevant_chunk_first(self, project):
        save_context_file(project, "a.md", "# Payments\nStripe webhooks land in payment_events\n")
        save_context_file(project, "b.md", "# Style\nAlways write tests first\n")
        out = search(project, "stripe webhook payments")
        assert out[0]["source"] == "context"
        assert "Stripe" in out[0]["snippet"]

    def test_sources_filter(self, project):
        save_context_file(project, "a.md", "orders conventions\n")
        record(action="execute_sql", actor_type="agent", project=project, payload_in={"sql": "SELECT orders"})
        assert {r["source"] for r in search(project, "orders")} == {"context", "audit"}
        assert {r["source"] for r in search(project, "orders", sources=["audit"])} == {"audit"}

    def test_empty_query_and_no_matches(self, project):
        assert search(project, "   ") == []
        assert search(project, "zzz_nothing") == []

    def test_stats_counts_by_source(self, project):
        save_context_file(project, "a.md", "hello\n")
        out = stats(project)
        assert out["context"] == 1 and out["schema"] == 0


class TestReindex:
    def test_backfills_every_source(self, project):
        TestSchemaCards()._make_schema(project)
        from workspaces.models import ChatMessage

        conversation = Conversation.objects.create(project=project)
        ChatMessage.objects.create(project=project, conversation=conversation, role="user", content="hi")
        record(action="execute_sql", actor_type="agent", project=project, payload_in={"sql": "SELECT 1"})
        save_context_file(project, "n.md", "notes\n")
        MemoryChunk.objects.all().delete()  # wipe: everything must come back

        out = reindex_project(project)
        assert out["total"] == chunks(project).count() > 3
        assert "errors" not in out

    def test_unreachable_schema_is_reported_not_fatal(self, project):
        project.server.dsn = "/nonexistent/nope.db"
        project.server.save()
        save_context_file(project, "n.md", "still indexed\n")
        MemoryChunk.objects.all().delete()
        out = reindex_project(project)
        assert "schema" in out["errors"]
        assert chunks(project, "context").count() == 1

    def test_command_runs(self, project):
        from django.core.management import call_command

        save_context_file(project, "n.md", "via command\n")
        MemoryChunk.objects.all().delete()
        call_command("reindex_memory", project=project.pk)
        assert chunks(project, "context").count() == 1

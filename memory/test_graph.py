"""Graph memory tests: configuration gating, episode enqueueing from the
signal paths, and the GDPR guarantee that redaction reaches the graph.

Everything runs without graphiti-core and without Neo4j: the tests force
GRAPHITI_INSTALLED and capture queued jobs — the worker thread and the
Graphiti client stay out of the picture (they are I/O plumbing)."""

from datetime import UTC, datetime

import pytest

from agents.models import AgentConnection
from audit.services import payloads_redacted, record
from instances.models import Server
from memory import graph
from memory.models import GraphEpisode, GraphSettings
from workspaces.models import ChatMessage, Conversation, Project

pytestmark = pytest.mark.django_db


@pytest.fixture
def project(tmp_path):
    server = Server.objects.create(name="Local", adapter_type="sqlite", dsn=str(tmp_path / "g.db"))
    return Project.objects.create(name="Graph", server=server)


@pytest.fixture
def configured(monkeypatch):
    """A fully valid graph configuration with the queue captured."""
    monkeypatch.setattr(graph, "GRAPHITI_INSTALLED", True)
    llm = AgentConnection.objects.create(name="extract", backend="anthropic_api", model="claude-haiku-4-5")
    emb = AgentConnection.objects.create(
        name="embed", backend="openai_compat", base_url="http://localhost:11434/v1"
    )
    row = GraphSettings.load()
    row.enabled = True
    row.neo4j_uri = "bolt://localhost:7687"
    row.llm_connection = llm
    row.embedder_connection = emb
    row.save()
    jobs = []
    monkeypatch.setattr(graph, "_enqueue", jobs.append)
    return jobs


class TestConfigurationGate:
    def test_unconfigured_is_off(self, monkeypatch):
        monkeypatch.setattr(graph, "GRAPHITI_INSTALLED", True)
        assert graph.is_configured() is False

    def test_full_configuration_is_on(self, configured):
        assert graph.is_configured() is True

    def test_disabled_wins_over_complete_config(self, configured):
        GraphSettings.objects.filter(pk=1).update(enabled=False)
        assert graph.is_configured() is False

    def test_claude_code_cannot_extract(self, configured):
        cc = AgentConnection.objects.create(name="cc", backend="claude_code")
        GraphSettings.objects.filter(pk=1).update(llm_connection=cc)
        assert graph.is_configured() is False

    def test_embedder_must_be_openai_compat(self, configured):
        anthropic = AgentConnection.objects.create(name="a", backend="anthropic_api")
        GraphSettings.objects.filter(pk=1).update(embedder_connection=anthropic)
        assert graph.is_configured() is False

    def test_env_uri_fallback(self, configured, monkeypatch):
        GraphSettings.objects.filter(pk=1).update(neo4j_uri="")
        monkeypatch.setenv("DIABASE_NEO4J_URI", "bolt://neo4j:7687")
        assert graph.is_configured() is True

    def test_without_graphiti_everything_is_a_noop(self, project):
        assert graph.is_configured() is False or graph.GRAPHITI_INSTALLED
        stats = graph.stats()
        assert stats["episodes"] == 0


class TestAuditEpisodes:
    def test_write_action_becomes_episode(self, project, configured):
        record(
            action="execute_sql",
            actor_type="agent",
            actor="claude",
            project=project,
            payload_in={"sql": "DROP TABLE legacy_users"},
        )
        (job,) = [j for j in configured if j["kind"] == "episode"]
        assert job["source_ref"].startswith("audit:")
        assert "DROP TABLE legacy_users" in job["body"]
        assert job["project_id"] == project.pk

    def test_reads_produce_no_episode(self, project, configured):
        record(action="query_sql", actor_type="agent", project=project, payload_in={"q": "x"})
        assert [j for j in configured if j["kind"] == "episode"] == []

    def test_unconfigured_graph_produces_no_episode(self, project, configured):
        GraphSettings.objects.filter(pk=1).update(enabled=False)
        record(action="execute_sql", actor_type="agent", project=project, payload_in={"sql": "x"})
        assert configured == []


class TestChatEpisodes:
    def test_exchange_pairs_user_and_assistant(self, project, configured):
        conversation = Conversation.objects.create(project=project)
        ChatMessage.objects.create(
            project=project, conversation=conversation, role="user", content="rename orders to sales"
        )
        reply = ChatMessage.objects.create(
            project=project, conversation=conversation, role="assistant", content="done: orders is now sales"
        )
        exchange = [j for j in configured if j["source_ref"].startswith("exchange:")]
        (job,) = exchange
        assert job["source_ref"] == f"exchange:{conversation.pk}:{reply.pk}"
        assert "rename orders" in job["body"] and "now sales" in job["body"]
        assert job["conversational"] is True

    def test_user_message_alone_produces_nothing(self, project, configured):
        conversation = Conversation.objects.create(project=project)
        ChatMessage.objects.create(project=project, conversation=conversation, role="user", content="hi")
        assert [j for j in configured if j["source_ref"].startswith("exchange:")] == []

    def test_deleting_a_message_queues_graph_removal(self, project, configured):
        conversation = Conversation.objects.create(project=project)
        message = ChatMessage.objects.create(
            project=project, conversation=conversation, role="assistant", content="secret"
        )
        ref = f"exchange:{conversation.pk}:{message.pk}"
        GraphEpisode.objects.create(project=project, source_ref=ref, episode_uuid="ep-1")
        configured.clear()
        message.delete()
        (job,) = [j for j in configured if j["kind"] == "remove"]
        assert job["refs"] == [ref]


class TestGdprPropagation:
    def test_redaction_reaches_the_graph(self, project, configured):
        record(
            action="execute_sql",
            actor_type="agent",
            project=project,
            payload_in={"sql": "select email from users"},
        )
        entry = project.audit_entries.get(action="execute_sql")
        GraphEpisode.objects.create(project=project, source_ref=f"audit:{entry.pk}", episode_uuid="ep-2")
        configured.clear()
        payloads_redacted.send(sender=None, pks=[entry.pk])
        (job,) = [j for j in configured if j["kind"] == "remove"]
        assert job["refs"] == [f"audit:{entry.pk}"]

    def test_no_bridge_rows_means_no_job(self, project, configured):
        assert graph.remove_refs(project.pk, ["audit:999"]) is False


class TestGraphSearch:
    """graph.search: ephemeral client, project-scoped groups, and the
    guarantee that an unconfigured or failing graph returns nothing."""

    class FakeSearchClient:
        def __init__(self, edges):
            self._edges = edges
            self.calls = []
            self.closed = False

        async def search(self, query, group_ids=None, num_results=10):
            self.calls.append({"query": query, "group_ids": group_ids, "num_results": num_results})
            return self._edges

        async def close(self):
            self.closed = True

    def test_unconfigured_returns_empty(self, project):
        assert graph.search(project, "orders") == []

    def test_facts_come_back_with_temporal_validity(self, project, configured, monkeypatch):
        import types

        edge = types.SimpleNamespace(
            uuid="u1",
            name="RENAMED_TO",
            fact="orders was renamed to sales",
            valid_at=datetime(2026, 7, 30, tzinfo=UTC),
            invalid_at=None,
        )
        client = self.FakeSearchClient([edge])
        monkeypatch.setattr(graph, "_build_client", lambda: client)
        (hit,) = graph.search(project, "orders", k=5)
        assert hit["source"] == "graph" and hit["ref"] == "graph:u1"
        assert "renamed to sales" in hit["snippet"]
        assert hit["valid_at"].startswith("2026-07-30") and hit["invalid_at"] is None
        assert client.calls[0]["group_ids"] == [str(project.pk)]
        assert client.closed is True

    def test_a_failing_graph_degrades_to_no_results(self, project, configured, monkeypatch):
        def boom():
            raise ConnectionError("neo4j down")

        monkeypatch.setattr(graph, "_build_client", boom)
        assert graph.search(project, "orders") == []


class TestWorkerProcessing:
    """_process against a fake Graphiti client: the bridge table must
    mirror every add and remove."""

    class FakeClient:
        def __init__(self):
            self.added, self.removed = [], []

        async def add_episode(self, **kwargs):
            import types

            self.added.append(kwargs)
            return types.SimpleNamespace(episode=types.SimpleNamespace(uuid=f"uuid-{len(self.added)}"))

        async def remove_episode(self, uuid):
            self.removed.append(uuid)

    def run(self, client, job):
        import asyncio

        graph._handle(client, asyncio.run, job)

    @pytest.fixture(autouse=True)
    def _episode_type(self, monkeypatch):
        class FakeEpisodeType:
            text = "text"
            message = "message"

        monkeypatch.setattr(graph, "EpisodeType", FakeEpisodeType, raising=False)

    def test_episode_lands_in_bridge_table(self, project):
        client = self.FakeClient()
        self.run(
            client,
            {
                "kind": "episode",
                "project_id": project.pk,
                "source_ref": "audit:1",
                "body": "agent ran execute_sql",
                "description": "audited action",
                "when": datetime(2026, 8, 1, tzinfo=UTC),
                "conversational": False,
            },
        )
        row = GraphEpisode.objects.get(project=project, source_ref="audit:1")
        assert row.episode_uuid == "uuid-1"
        assert client.added[0]["group_id"] == str(project.pk)

    def test_reingesting_a_ref_replaces_the_episode(self, project):
        client = self.FakeClient()
        job = {
            "kind": "episode",
            "project_id": project.pk,
            "source_ref": "exchange:1:2",
            "body": "v1",
            "description": "project chat",
            "when": datetime(2026, 8, 1, tzinfo=UTC),
            "conversational": True,
        }
        self.run(client, job)
        self.run(client, {**job, "body": "v2"})
        assert client.removed == ["uuid-1"]
        assert GraphEpisode.objects.get(source_ref="exchange:1:2").episode_uuid == "uuid-2"

    def test_remove_deletes_bridge_rows(self, project):
        GraphEpisode.objects.create(project=project, source_ref="audit:9", episode_uuid="ep-9")
        client = self.FakeClient()
        self.run(client, {"kind": "remove", "project_id": project.pk, "refs": ["audit:9"]})
        assert client.removed == ["ep-9"]
        assert not GraphEpisode.objects.filter(source_ref="audit:9").exists()

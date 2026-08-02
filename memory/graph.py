"""Optional temporal knowledge graph over project memory (Graphiti + Neo4j).

Layered ON TOP of the lexical index, never instead of it: the graph is
active only when graphiti-core is installed, GraphSettings is enabled and
complete, and Neo4j answers. Everything here degrades to a no-op — the
rest of Diabase must behave identically with the graph off.

Ingestion is asynchronous by construction: episodes are queued and
processed by one background worker thread (entity extraction is an LLM
call per episode — it must never sit on a request or turn thread).
Every episode written lands in GraphEpisode, the bridge that lets GDPR
redaction reach into the graph (see remove_refs).
"""

import logging
import os
import queue
import threading

from django.utils import timezone

logger = logging.getLogger(__name__)

try:  # optional dependency: `uv sync --extra graph`
    from graphiti_core import Graphiti
    from graphiti_core.nodes import EpisodeType

    GRAPHITI_INSTALLED = True
except ImportError:  # pragma: no cover — exercised only without the extra
    GRAPHITI_INSTALLED = False

AUDIT_EPISODE_CHARS = 1500
EXCHANGE_CHARS = 3000


def _config():
    """Effective settings row + env fallbacks (compose sets the env)."""
    from .models import GraphSettings

    row = GraphSettings.load()
    uri = row.neo4j_uri or os.environ.get("DIABASE_NEO4J_URI", "")
    user = row.neo4j_user or os.environ.get("DIABASE_NEO4J_USER", "neo4j")
    password = row.neo4j_password or os.environ.get("DIABASE_NEO4J_PASSWORD", "")
    return row, uri, user, password


def is_configured() -> bool:
    """Cheap gate used by signal handlers — no I/O, just config shape."""
    if not GRAPHITI_INSTALLED:
        return False
    row, uri, _, _ = _config()
    return bool(
        row.enabled
        and uri
        and row.llm_connection_id
        and row.embedder_connection_id
        and row.llm_connection.backend != "claude_code"
        and row.embedder_connection.backend == "openai_compat"
    )


def _build_client():
    """A Graphiti client from the configured connections.

    Extraction accepts anthropic_api or any openai_compat endpoint;
    embeddings and reranking ride the (OpenAI-compatible) embedder
    connection — no hidden default to api.openai.com anywhere.
    """
    from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient
    from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
    from graphiti_core.llm_client import LLMConfig
    from graphiti_core.llm_client.anthropic_client import AnthropicClient
    from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient

    row, uri, user, password = _config()
    llm_conn, emb_conn = row.llm_connection, row.embedder_connection
    llm_config = LLMConfig(
        api_key=llm_conn.api_key or "unused", model=llm_conn.model or None, base_url=llm_conn.base_url or None
    )
    if llm_conn.backend == "anthropic_api":
        llm_client = AnthropicClient(llm_config)
    else:
        llm_client = OpenAIGenericClient(llm_config)
    emb_config = OpenAIEmbedderConfig(
        api_key=emb_conn.api_key or "unused",
        base_url=emb_conn.base_url or None,
        **({"embedding_model": row.embedding_model} if row.embedding_model else {}),
    )
    reranker = OpenAIRerankerClient(
        LLMConfig(
            api_key=emb_conn.api_key or "unused",
            base_url=emb_conn.base_url or None,
            model=llm_conn.model or None,
        )
    )
    return Graphiti(
        uri,
        user,
        password,
        llm_client=llm_client,
        embedder=OpenAIEmbedder(emb_config),
        cross_encoder=reranker,
    )


# ---------- background worker ----------
#
# One daemon thread owns one asyncio loop and one Graphiti client; jobs
# arrive on a queue. Same philosophy as the reindex thread: the request
# path only ever enqueues.

_QUEUE: "queue.Queue" = queue.Queue()
_WORKER: threading.Thread | None = None
_WORKER_LOCK = threading.Lock()


def _ensure_worker():
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker_main, daemon=True, name="memory-graph")
            _WORKER.start()


def _worker_main():  # pragma: no cover — thread plumbing, logic tested via _process
    import asyncio

    from django.db import connections

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = None
    try:
        while True:
            job = _QUEUE.get()
            if job is None:  # test hook / shutdown
                break
            try:
                if client is None:
                    client = _build_client()
                _handle(client, loop.run_until_complete, job)
            except Exception:
                logger.exception("graph job failed: %s", job.get("kind"))
            finally:
                connections.close_all()
    finally:
        if client is not None:
            loop.run_until_complete(client.close())
        loop.close()


def _handle(client, run, job: dict):
    """Execute one queued job: ORM stays synchronous on the worker
    thread, only Graphiti coroutines go through `run` (the loop)."""
    from .models import GraphEpisode

    if job["kind"] == "episode":
        # replacing content (a re-saved message) means remove-then-add:
        # Graphiti episodes are immutable facts in time
        existing = GraphEpisode.objects.filter(
            project_id=job["project_id"], source_ref=job["source_ref"]
        ).first()
        if existing:
            run(client.remove_episode(existing.episode_uuid))
            existing.delete()
        result = run(
            client.add_episode(
                name=job["source_ref"],
                episode_body=job["body"],
                source_description=job["description"],
                reference_time=job["when"],
                source=EpisodeType.message if job.get("conversational") else EpisodeType.text,
                group_id=str(job["project_id"]),
            )
        )
        GraphEpisode.objects.create(
            project_id=job["project_id"],
            source_ref=job["source_ref"],
            episode_uuid=result.episode.uuid,
        )
    elif job["kind"] == "remove":
        for row in GraphEpisode.objects.filter(project_id=job["project_id"], source_ref__in=job["refs"]):
            run(client.remove_episode(row.episode_uuid))
            row.delete()


def _enqueue(job: dict):
    _QUEUE.put(job)
    _ensure_worker()


# ---------- ingestion API (called from memory.signals) ----------


def episode_for_audit(entry) -> bool:
    """A write action becomes a text episode: who did what, when, outcome."""
    from .services import UNINDEXED_ACTIONS

    if not is_configured() or entry.project_id is None or entry.action in UNINDEXED_ACTIONS:
        return False
    if entry.redacted_at is not None:
        return False
    import json

    parts = [
        f"{entry.actor_type}"
        + (f" {entry.actor}" if entry.actor else "")
        + f" ran {entry.action} ({entry.outcome})"
    ]
    if entry.payload_in:
        parts.append("input: " + json.dumps(entry.payload_in, default=str)[:AUDIT_EPISODE_CHARS])
    if entry.error:
        parts.append("error: " + entry.error[:AUDIT_EPISODE_CHARS])
    _enqueue(
        {
            "kind": "episode",
            "project_id": entry.project_id,
            "source_ref": f"audit:{entry.pk}",
            "body": "\n".join(parts),
            "description": "audited action",
            "when": entry.created_at,
            "conversational": False,
        }
    )
    return True


def episode_for_exchange(assistant_message) -> bool:
    """One episode per user+assistant exchange, keyed by the assistant
    message: fewer extraction calls, more coherent context than 1:1."""
    if not is_configured() or assistant_message.role != "assistant":
        return False
    prior = (
        assistant_message.conversation.messages.filter(
            role="user", created_at__lte=assistant_message.created_at
        )
        .exclude(pk=assistant_message.pk)
        .order_by("-created_at")
        .first()
    )
    body = ""
    if prior:
        body += f"user: {prior.content[:EXCHANGE_CHARS]}\n"
    body += f"assistant: {assistant_message.content[:EXCHANGE_CHARS]}"
    _enqueue(
        {
            "kind": "episode",
            "project_id": assistant_message.project_id,
            "source_ref": f"exchange:{assistant_message.conversation_id}:{assistant_message.pk}",
            "body": body,
            "description": "project chat",
            "when": assistant_message.created_at or timezone.now(),
            "conversational": True,
        }
    )
    return True


def remove_refs(project_id: int, refs: list[str]) -> bool:
    """GDPR propagation: episodes derived from tombstoned/deleted sources
    leave the graph. Queued even when the graph looks unconfigured — a
    temporarily unreachable Neo4j must not swallow an erasure."""
    if not GRAPHITI_INSTALLED:
        return False
    from .models import GraphEpisode

    if not GraphEpisode.objects.filter(project_id=project_id, source_ref__in=refs).exists():
        return False
    _enqueue({"kind": "remove", "project_id": project_id, "refs": list(refs)})
    return True


# ---------- status / retrieval ----------


def stats(project=None) -> dict:
    """What the Memory tab needs: installed / enabled / configured, and
    the episode count as a liveness proxy (no Neo4j round trip)."""
    from .models import GraphEpisode, GraphSettings

    row = GraphSettings.load()
    episodes = GraphEpisode.objects.all()
    if project is not None:
        episodes = episodes.filter(project=project)
    return {
        "installed": GRAPHITI_INSTALLED,
        "enabled": row.enabled,
        "configured": is_configured(),
        "episodes": episodes.count(),
        "pending": _QUEUE.qsize(),
    }


def search(project, query: str, k: int = 8) -> list[dict]:
    """Hybrid graph search: Graphiti facts (entity relations with
    temporal validity) scoped to the project's group. Synchronous with
    an ephemeral client — recall calls this from the turn thread."""
    if not is_configured() or not query.strip():
        return []
    import asyncio

    async def _run():
        client = _build_client()
        try:
            return await client.search(
                query, group_ids=[str(project.pk)], num_results=max(1, min(int(k), 25))
            )
        finally:
            await client.close()

    try:
        edges = asyncio.run(_run())
    except Exception as e:
        logger.warning("graph search failed: %s", e)
        return []
    return [
        {
            "source": "graph",
            "ref": f"graph:{edge.uuid}",
            "title": edge.name,
            "snippet": edge.fact,
            "valid_at": edge.valid_at.isoformat() if edge.valid_at else None,
            "invalid_at": edge.invalid_at.isoformat() if edge.invalid_at else None,
        }
        for edge in edges
    ]

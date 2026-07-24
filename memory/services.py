"""Memory indexing and retrieval.

Indexing is incremental (content-hashed upserts driven by signals, see
memory.signals) and always rebuildable (`reindex_project`). Retrieval is
lexical BM25-style scoring in Python — honest and dependency-free at
Diabase's scale (thousands of chunks per project); hybrid scoring with
embeddings plugs into `search()` when an embedding connection exists.
"""

import hashlib
import json
import math
import re

from django.db.models import Count

from .models import MemoryChunk

# audit actions that are pure reads or memory's own plumbing: indexing
# them would fill the memory with noise about reading the memory
UNINDEXED_ACTIONS = {
    "list_tables",
    "describe_table",
    "query_sql",
    "list_functions",
    "read_function",
    "list_buckets",
    "get_auth_config",
    "get_advisors",
    "read_context_file",
    "search_context_files",
    "recall",
    "audit.redacted",
    "memory.reindexed",
}

AUDIT_PAYLOAD_CHARS = 800
CHAT_CHARS = 2000
CONTEXT_CHUNK_CHARS = 1800

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def upsert_chunk(project, source_type: str, source_ref: str, text: str, title: str = "") -> bool:
    """Write one chunk; hash-skips untouched content so signal-driven
    re-saves don't churn the table. Returns whether anything changed."""
    text = text.strip()
    if not text:
        MemoryChunk.objects.filter(project=project, source_type=source_type, source_ref=source_ref).delete()
        return False
    digest = _hash(title + "\n" + text)
    existing = MemoryChunk.objects.filter(
        project=project, source_type=source_type, source_ref=source_ref
    ).first()
    if existing and existing.content_hash == digest:
        return False
    if existing:
        existing.title = title
        existing.text = text
        existing.content_hash = digest
        existing.embedding = None  # stale by definition: content changed
        existing.save()
        return True
    MemoryChunk.objects.create(
        project=project,
        source_type=source_type,
        source_ref=source_ref,
        title=title,
        text=text,
        content_hash=digest,
    )
    return True


def drop_chunks(project, source_type: str, *, ref: str = "", ref_prefix: str = "") -> int:
    qs = MemoryChunk.objects.filter(project=project, source_type=source_type)
    if ref:
        qs = qs.filter(source_ref=ref)
    elif ref_prefix:
        qs = qs.filter(source_ref__startswith=ref_prefix)
    count, _ = qs.delete()
    return count


# ---------- indexers, one per source ----------


def index_audit_entry(entry) -> bool:
    """Write actions and lifecycle events only — the decisions, not the
    lookups. Redacted entries are dropped, never indexed: the memory
    index must not outlive the GDPR tombstone."""
    if entry.project is None or entry.action in UNINDEXED_ACTIONS:
        return False
    ref = f"audit:{entry.pk}"
    if entry.redacted_at is not None:
        drop_chunks(entry.project, "audit", ref=ref)
        return False
    parts = [
        f"{entry.created_at:%Y-%m-%d %H:%M} {entry.actor_type}"
        + (f":{entry.actor}" if entry.actor else "")
        + f" — {entry.action} ({entry.outcome})"
    ]
    if entry.payload_in:
        parts.append("in: " + json.dumps(entry.payload_in, default=str)[:AUDIT_PAYLOAD_CHARS])
    if entry.payload_out:
        parts.append("out: " + json.dumps(entry.payload_out, default=str)[:AUDIT_PAYLOAD_CHARS])
    if entry.error:
        parts.append("error: " + entry.error[:AUDIT_PAYLOAD_CHARS])
    return upsert_chunk(
        entry.project,
        "audit",
        ref,
        "\n".join(parts),
        title=f"{entry.action} — {entry.created_at:%Y-%m-%d %H:%M}",
    )


def purge_redacted_audit(pks) -> int:
    """GDPR consistency: chunks derived from redacted payloads die with
    them (see audit.services.payloads_redacted)."""
    count, _ = MemoryChunk.objects.filter(
        source_type="audit", source_ref__in=[f"audit:{pk}" for pk in pks]
    ).delete()
    return count


def index_chat_message(message) -> bool:
    if not message.content.strip():
        return False
    conversation = message.conversation
    title = f"{conversation.title or f'Chat #{conversation.pk}'} — {message.get_role_display()}"
    text = f"{message.role}: {message.content[:CHAT_CHARS]}"
    return upsert_chunk(
        message.project, "chat", f"chat:{message.conversation_id}:{message.pk}", text, title=title
    )


def _split_sections(content: str) -> list[tuple[str, str]]:
    """(heading, body) sections split on markdown headings; oversized
    bodies are re-split on a fixed budget so no chunk dwarfs the rest."""
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    for line in content.splitlines():
        if line.lstrip().startswith("#"):
            heading = line.strip().lstrip("#").strip()
            sections.append((heading, []))
            continue
        if not sections:
            sections.append(("", []))
        sections[-1][1].append(line)
    out = []
    for head, lines in sections:
        body = "\n".join(lines).strip()
        if not body and not head:
            continue
        while len(body) > CONTEXT_CHUNK_CHARS:
            cut = body.rfind("\n", 0, CONTEXT_CHUNK_CHARS)
            cut = cut if cut > 0 else CONTEXT_CHUNK_CHARS
            out.append((head, body[:cut].strip()))
            body = body[cut:].strip()
        out.append((head, body))
    return out


def index_context_file(file) -> int:
    """One chunk per heading section; stale section refs are dropped so
    a shrunken file doesn't leave ghosts behind."""
    sections = _split_sections(file.content)
    changed = 0
    for i, (heading, body) in enumerate(sections):
        text = (f"# {heading}\n{body}" if heading else body).strip()
        title = f"{file.name}" + (f" — {heading}" if heading else "")
        if upsert_chunk(file.project, "context", f"file:{file.name}#{i}", text, title=title):
            changed += 1
    stale = MemoryChunk.objects.filter(
        project=file.project, source_type="context", source_ref__startswith=f"file:{file.name}#"
    ).exclude(source_ref__in=[f"file:{file.name}#{i}" for i in range(len(sections))])
    changed += stale.delete()[0]
    return changed


def drop_context_file(project, name: str) -> int:
    return drop_chunks(project, "context", ref_prefix=f"file:{name}#")


def _table_card(table: str, columns: list[dict], referenced_by: list[str], indexes: list[str]) -> str:
    cols = []
    for c in columns:
        bits = [c["name"], str(c.get("type") or "")]
        if c.get("primary_key"):
            bits.append("PK")
        if not c.get("nullable", True):
            bits.append("NOT NULL")
        if c.get("default") not in (None, ""):
            bits.append(f"default {c['default']}")
        fk = c.get("references")
        if fk:
            bits.append(f"-> {fk['table']}.{fk['column']}")
        cols.append(" ".join(b for b in bits if b))
    parts = [f"Table {table} — {len(columns)} columns", "Columns: " + "; ".join(cols)]
    if referenced_by:
        parts.append("Referenced by: " + "; ".join(referenced_by))
    if indexes:
        parts.append("Indexes: " + "; ".join(indexes))
    return "\n".join(parts)


_PG_INDEXES_SQL = (
    "SELECT tablename AS table_name, indexname AS index_name FROM pg_indexes WHERE schemaname='public'"
)


def _table_indexes(project) -> dict[str, list[str]]:
    """Index names per table, best-effort in ONE read-only query; the
    card is complete without them."""
    from instances.adapters import get_adapter

    server = project.server
    sql = {
        "sqlite": (
            "SELECT tbl_name AS table_name, name AS index_name FROM sqlite_master "
            "WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ),
        "postgres": _PG_INDEXES_SQL,
        "supabase": _PG_INDEXES_SQL,
    }.get(server.adapter_type)
    if not sql:
        return {}
    try:
        rows = get_adapter(server.adapter_type, server.dsn).query_sql(sql)["rows"]
    except Exception:
        return {}
    out: dict[str, list[str]] = {}
    for r in rows:
        out.setdefault(str(r["table_name"]), []).append(str(r["index_name"]))
    return out


def index_schema(project) -> int:
    """The introspection cards: one per table — columns, both FK
    directions, indexes. What lets recall("where do avatars live?") or
    recall("what touches orders") answer from structure, not guesses."""
    from instances.adapters import get_adapter

    server = project.server
    schema = get_adapter(server.adapter_type, server.dsn).get_schema()
    referenced_by: dict[str, list[str]] = {}
    for table, columns in schema.items():
        for c in columns:
            fk = c.get("references")
            if fk:
                referenced_by.setdefault(fk["table"], []).append(
                    f"{table}.{c['name']} -> {fk['table']}.{fk['column']}"
                )
    indexes = _table_indexes(project)
    changed = 0
    for table, columns in schema.items():
        card = _table_card(table, columns, referenced_by.get(table, []), indexes.get(table, []))
        if upsert_chunk(project, "schema", f"table:{table}", card, title=f"Table {table}"):
            changed += 1
    stale = MemoryChunk.objects.filter(project=project, source_type="schema").exclude(
        source_ref__in=[f"table:{t}" for t in schema]
    )
    changed += stale.delete()[0]
    return changed


def reindex_project(project) -> dict:
    """Full rebuild from the sources of truth. Schema needs the instance
    reachable — its failure is reported, not fatal to the rest."""
    counts = {"schema": 0, "audit": 0, "chat": 0, "context": 0}
    errors = {}
    try:
        counts["schema"] = index_schema(project)
    except Exception as e:
        errors["schema"] = str(e)
    for entry in project.audit_entries.all().iterator():
        if index_audit_entry(entry):
            counts["audit"] += 1
    for message in project.messages.all().iterator():
        if index_chat_message(message):
            counts["chat"] += 1
    for file in project.context_files.all():
        counts["context"] += index_context_file(file)
    return {
        "changed": counts,
        "total": MemoryChunk.objects.filter(project=project).count(),
        **({"errors": errors} if errors else {}),
    }


# ---------- retrieval ----------

SNIPPET_CHARS = 500


def stats(project) -> dict:
    by_source = dict(
        MemoryChunk.objects.filter(project=project).values_list("source_type").annotate(n=Count("id"))
    )
    return {source: by_source.get(source, 0) for source, _ in MemoryChunk.SOURCES}


def search(project, query: str, sources: list[str] | None = None, k: int = 8) -> list[dict]:
    """BM25-style lexical ranking over the project's chunks. Loads the
    candidate set in memory — fine by design at this scale; revisit with
    pgvector/FTS when instances outgrow it."""
    qtokens = [t for t in dict.fromkeys(_tokens(query)) if len(t) > 1]
    if not qtokens:
        return []
    qs = MemoryChunk.objects.filter(project=project)
    valid = {s for s, _ in MemoryChunk.SOURCES}
    if sources:
        qs = qs.filter(source_type__in=[s for s in sources if s in valid])
    chunks = list(qs.only("source_type", "source_ref", "title", "text"))
    if not chunks:
        return []
    docs = [_tokens(c.title + " " + c.text) for c in chunks]
    avg_len = sum(len(d) for d in docs) / len(docs)
    df = {t: sum(1 for d in docs if t in d) for t in qtokens}
    n = len(docs)
    scored = []
    for chunk, doc in zip(chunks, docs, strict=True):
        score = 0.0
        counts = None
        for t in qtokens:
            if not df[t]:
                continue
            if counts is None:
                counts = {}
                for tok in doc:
                    counts[tok] = counts.get(tok, 0) + 1
            tf = counts.get(t, 0)
            if not tf:
                continue
            idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
            score += idf * (tf * 2.2) / (tf + 1.2 * (0.25 + 0.75 * len(doc) / avg_len))
        if score > 0:
            scored.append((score, chunk))
    scored.sort(key=lambda pair: -pair[0])
    return [
        {
            "source": c.source_type,
            "ref": c.source_ref,
            "title": c.title,
            "snippet": c.text[:SNIPPET_CHARS],
            "score": round(s, 3),
        }
        for s, c in scored[: max(1, min(int(k), 25))]
    ]

# Data processing & retention

Diabase is self-hosted: **you** (the operator) are the data controller for
everything it stores. This page documents what that is, where it lives, and
the tools Diabase gives you to meet retention and erasure obligations
(GDPR or otherwise).

## What Diabase stores

| Data | Where | Contains personal data? |
|---|---|---|
| Audit trail rows (actor, action, outcome, timestamps) | `AuditEntry` | Usernames only |
| Audit **payloads** (tool inputs/outputs) | `AuditEntry.payload_in/out`, `error` | **Potentially yes** — SQL statements, query results, deployed function code, auth settings |
| Chat history | `ChatMessage` | Whatever you and the agent typed |
| Context files | `ContextFile` | Whatever you uploaded |
| Function sources | `EdgeFunctionSource` | The code you deployed |
| LLM API keys | `AgentConnection` | Encrypted at rest (Fernet), never displayed back |

Auth configuration secrets from managed Supabase projects (SMTP password,
OAuth secrets) are **redacted at the adapter** before they can reach the
model, the audit trail, or the GUI — they are never stored.

## The audit trail: permanent rows, disposable payloads

The trail is append-only by design: no code path can update or delete an
entry, so "who did what, when, with what outcome" survives forever — that
is the product's accountability guarantee.

Personal data lives only in the **payloads**. The one sanctioned mutation
(`redact_payloads`) replaces them in place with a tombstone
(`{"redacted": true, "reason": ...}`) and stamps `redacted_at`; the row —
actor, action, outcome, timestamp — is untouched. Redacted entries show a
`redacted` pill in the audit log. Every redaction run records its own
`audit.redacted` entry (criterion and row count, never the searched text).

## Retention policy

*Settings → Audit & privacy → "Keep payloads for N days".*

- Default **0 = keep forever**: nothing is thrown away until you turn the
  window on.
- With a positive window, payloads older than N days are redacted on each
  purge run. Runs happen when you click **Run cleanup now**, or on schedule:

```bash
python manage.py purge_audit_payloads          # applies the stored window
python manage.py purge_audit_payloads --days 30  # one-off override
```

Schedule the bare command daily (cron, systemd timer, container sidecar).

## Right to erasure

For a request like "remove my email address from your records":

*Settings → Audit & privacy → Right to erasure* — enter the text (an email,
a name), optionally scope to one project, confirm. Every payload containing
the text (case-insensitive) is tombstoned with reason `erasure`.

From the shell:

```bash
python manage.py purge_audit_payloads --erase-contains "mario@example.com"
python manage.py purge_audit_payloads --erase-contains "mario" --project 3
```

The searched text is deliberately **not** written to the audit trail — the
`audit.redacted` entry records only the scope and the row count.

Erasure of chat history and context files is direct: delete the chat
(sidebar ✕) or the file (context editor → Delete file); both deletions are
audited without retaining the content.

## The memory index

The recall index (`MemoryChunk`) is **derived data**: chunks built from
the schema, the audit trail, chats and context files, always rebuildable
(`reindex_memory`). It follows its sources' lifecycle automatically —
chunks die with the chat or file they came from, and GDPR redaction of
audit payloads purges the chunks derived from them in the same operation
(the `payloads_redacted` broadcast). Nothing in the index survives its
source of truth.

## Scope notes

- Redaction covers Diabase's own records. Data inside **your managed
  instances** is yours to handle with the agent or SQL directly.
- The dev default database is SQLite (`db.sqlite3`); production deployments
  should point `DATABASE_URL` at Postgres. Disk-level erasure (VACUUM,
  backups, snapshots) is the operator's responsibility on either engine.

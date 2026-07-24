"""Audit recording: the single gate every recorded action goes through.

Two entry points:
- `record()` — explicit logging of human/system actions (called from views)
- `AuditedAdapter` — wraps any adapter so every tool call the agent makes
  lands in the trail automatically, success or failure

Dependency direction: every other app calls into `audit`; `audit` only
depends on `workspaces` (for the Project FK) and on the adapter interface.
"""

from django.dispatch import Signal

from instances.adapters import AdapterError, BaseAdapter
from workspaces.models import Project

from .models import AuditEntry


def record(
    *,
    action: str,
    actor_type: str,
    actor: str = "",
    project: Project | None = None,
    payload_in: dict | None = None,
    payload_out: dict | None = None,
    outcome: str = "success",
    error: str = "",
) -> AuditEntry:
    return AuditEntry.objects.create(
        project=project,
        project_name=project.name if project else "",
        server_name=project.server.name if project else "",
        adapter_type=project.server.adapter_type if project else "",
        actor_type=actor_type,
        actor=actor,
        action=action,
        payload_in=payload_in or {},
        payload_out=payload_out or {},
        outcome=outcome,
        error=error,
    )


class AuditedAdapter:
    """Adapter proxy: same interface, every call recorded.

    The agent runtime always receives its adapter wrapped in this class —
    there is no unaudited path to a managed instance.
    """

    def __init__(self, adapter: BaseAdapter, *, project: Project, actor_type: str = "agent", actor: str = ""):
        self._adapter = adapter
        self._project = project
        self._actor_type = actor_type
        self._actor = actor

    def _call(self, action: str, payload_in: dict, fn):
        try:
            result = fn()
        except AdapterError as e:
            record(
                action=action,
                actor_type=self._actor_type,
                actor=self._actor,
                project=self._project,
                payload_in=payload_in,
                outcome="error",
                error=str(e),
            )
            raise
        except Exception as e:
            record(
                action=action,
                actor_type=self._actor_type,
                actor=self._actor,
                project=self._project,
                payload_in=payload_in,
                outcome="error",
                error=f"{type(e).__name__}: {e}",
            )
            raise
        record(
            action=action,
            actor_type=self._actor_type,
            actor=self._actor,
            project=self._project,
            payload_in=payload_in,
            payload_out=result if isinstance(result, dict) else {"result": result},
            outcome="success",
        )
        return result

    def list_tables(self):
        return self._call("list_tables", {}, self._adapter.list_tables)

    def describe_table(self, table: str):
        return self._call("describe_table", {"table": table}, lambda: self._adapter.describe_table(table))

    def execute_sql(self, sql: str):
        return self._call("execute_sql", {"sql": sql}, lambda: self._adapter.execute_sql(sql))

    def query_sql(self, sql: str):
        return self._call("query_sql", {"sql": sql}, lambda: self._adapter.query_sql(sql))

    @property
    def capabilities(self):
        return self._adapter.capabilities

    def list_functions(self):
        return self._call("list_functions", {}, self._adapter.list_functions)

    def get_function_body(self, slug: str):
        return self._call("read_function", {"slug": slug}, lambda: self._adapter.get_function_body(slug))

    def deploy_function(self, slug: str, body: str, *, name: str = "", verify_jwt: bool = True):
        # the full body rides in the audit payload: the trail must show
        # exactly what code went live, not a summary of it
        return self._call(
            "deploy_function",
            {"slug": slug, "name": name or slug, "verify_jwt": verify_jwt, "body": body},
            lambda: self._adapter.deploy_function(slug, body, name=name, verify_jwt=verify_jwt),
        )

    def delete_function(self, slug: str):
        return self._call("delete_function", {"slug": slug}, lambda: self._adapter.delete_function(slug))

    def get_advisors(self, kind: str):
        return self._call("get_advisors", {"kind": kind}, lambda: self._adapter.get_advisors(kind))

    def list_buckets(self):
        return self._call("list_buckets", {}, self._adapter.list_buckets)

    def create_bucket(self, name: str, *, public=False, file_size_limit=None, allowed_mime_types=None):
        return self._call(
            "create_bucket",
            {
                "name": name,
                "public": bool(public),
                "file_size_limit": file_size_limit,
                "allowed_mime_types": allowed_mime_types,
            },
            lambda: self._adapter.create_bucket(
                name, public=public, file_size_limit=file_size_limit, allowed_mime_types=allowed_mime_types
            ),
        )

    def update_bucket(self, name: str, *, public=None, file_size_limit=None, allowed_mime_types=None):
        return self._call(
            "update_bucket",
            {
                "name": name,
                "public": public,
                "file_size_limit": file_size_limit,
                "allowed_mime_types": allowed_mime_types,
            },
            lambda: self._adapter.update_bucket(
                name, public=public, file_size_limit=file_size_limit, allowed_mime_types=allowed_mime_types
            ),
        )

    def delete_bucket(self, name: str):
        return self._call("delete_bucket", {"name": name}, lambda: self._adapter.delete_bucket(name))

    def get_auth_config(self):
        return self._call("get_auth_config", {}, self._adapter.get_auth_config)

    def update_auth_config(self, changes: dict):
        # defense in depth: the adapter refuses secret writes, but even the
        # attempt must not land in the trail with a readable value
        from instances.adapters import redact_auth_config

        return self._call(
            "update_auth_config",
            {"changes": redact_auth_config(changes) if isinstance(changes, dict) else changes},
            lambda: self._adapter.update_auth_config(changes),
        )

    def get_schema(self):
        # composed of audited calls: each underlying list/describe is recorded
        return {t: self.describe_table(t) for t in self.list_tables()}


# ---------- GDPR: retention & erasure (issue #1) ----------
# Both operations tombstone payloads through the single sanctioned path
# (AppendOnlyQuerySet.redact_payloads) and record THEMSELVES in the
# trail: a redaction that left no trace would defeat the trail's point.

# broadcast after payloads are tombstoned (kwarg: pks) so DERIVED data —
# e.g. the memory index — dies with them; audit itself depends on nobody
payloads_redacted = Signal()


def apply_retention(*, actor: str = "", days: int | None = None) -> int:
    """Redact payloads older than the retention window. `days` overrides
    the stored policy (used by the cron command's --days); 0 or an unset
    policy means keep forever and is a no-op that records nothing."""
    from datetime import timedelta

    from django.utils import timezone

    from .models import RetentionPolicy

    if days is None:
        days = RetentionPolicy.load().audit_payload_days
    if not days:
        return 0
    cutoff = timezone.now() - timedelta(days=days)
    expired = AuditEntry.objects.filter(created_at__lt=cutoff, redacted_at__isnull=True)
    pks = list(expired.values_list("pk", flat=True))
    count = AuditEntry.objects.filter(pk__in=pks).redact_payloads(reason="retention")
    if count:
        payloads_redacted.send(sender=AuditEntry, pks=pks)
    record(
        action="audit.redacted",
        actor_type="user" if actor else "system",
        actor=actor,
        payload_in={"reason": "retention", "days": days},
        payload_out={"rows": count},
    )
    return count


def erase_payloads(*, needle: str = "", project=None, actor: str = "") -> int:
    """Right-to-erasure: redact the payloads matching a request — every
    entry of a project, or the entries whose payload contains `needle`
    (an email, a name...). The needle itself is NOT recorded: writing
    "erase mario@rossi.it" into the trail would re-create the data the
    request asked to remove."""
    import json

    if not needle and project is None:
        raise ValueError("erase_payloads needs a needle and/or a project")
    qs = AuditEntry.objects.filter(redacted_at__isnull=True)
    if project is not None:
        qs = qs.filter(project=project)
    if needle:
        lowered = needle.lower()
        pks = [
            e.pk
            for e in qs.only("pk", "payload_in", "payload_out", "error")
            if lowered in json.dumps(e.payload_in, default=str).lower()
            or lowered in json.dumps(e.payload_out, default=str).lower()
            or lowered in e.error.lower()
        ]
        qs = AuditEntry.objects.filter(pk__in=pks)
    redacted_pks = list(qs.values_list("pk", flat=True))
    count = qs.redact_payloads(reason="erasure")
    if count:
        payloads_redacted.send(sender=AuditEntry, pks=redacted_pks)
    record(
        action="audit.redacted",
        actor_type="user" if actor else "system",
        actor=actor,
        project=project,
        payload_in={"reason": "erasure", "scope": "project" if project is not None else "needle"},
        payload_out={"rows": count},
    )
    return count

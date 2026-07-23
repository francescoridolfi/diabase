"""Audit recording: the single gate every recorded action goes through.

Two entry points:
- `record()` — explicit logging of human/system actions (called from views)
- `AuditedAdapter` — wraps any adapter so every tool call the agent makes
  lands in the trail automatically, success or failure

Dependency direction: every other app calls into `audit`; `audit` only
depends on `workspaces` (for the Project FK) and on the adapter interface.
"""

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

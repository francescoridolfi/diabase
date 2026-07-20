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

    def get_schema(self):
        # composed of audited calls: each underlying list/describe is recorded
        return {t: self.describe_table(t) for t in self.list_tables()}

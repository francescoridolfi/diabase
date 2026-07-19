"""Tool registry: tools are declared once, backends render them.

A ToolSpec is the neutral description (name, description, JSON schema,
risk level). A BoundToolset binds the specs to a concrete audited
adapter and a policy: it is the single execution gate — backends never
call an adapter directly.
"""

from dataclasses import dataclass
from enum import StrEnum

from audit.services import AuditedAdapter
from instances.adapters import AdapterError

from .policy import AutonomyPolicy


class Risk(StrEnum):
    READ = "read"  # inspection only, never mutates the instance
    WRITE = "write"  # can mutate schema or data


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    risk: Risk


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="list_tables",
        description="List the tables in the project's database.",
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.READ,
    ),
    ToolSpec(
        name="describe_table",
        description="Describe the columns of a table (name, type, nullable, primary key).",
        input_schema={
            "type": "object",
            "properties": {"table": {"type": "string", "description": "The table to inspect."}},
            "required": ["table"],
        },
        risk=Risk.READ,
    ),
    ToolSpec(
        name="execute_sql",
        description="Execute a single SQL statement (DDL or DML) on the project's database.",
        input_schema={
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "The SQL statement to execute."}},
            "required": ["sql"],
        },
        risk=Risk.WRITE,
    ),
]


class ToolDenied(Exception):
    """Raised when the policy refuses a tool call. Backends report it
    to the model as an error result and emit ToolCallDenied."""


class BoundToolset:
    """The specs bound to one audited adapter + one policy.

    `execute()` returns the tool result as a dict; adapter errors come
    back as {"error": ...} so the model can read and react to them
    (same philosophy as the original prototype's _safe pattern).
    Policy refusals raise ToolDenied instead: they are not the model's
    to negotiate.
    """

    def __init__(
        self, *, adapter: AuditedAdapter, policy: AutonomyPolicy, specs: list[ToolSpec] | None = None
    ):
        self.adapter = adapter
        self.policy = policy
        self.specs = specs if specs is not None else TOOLS

    def allowed_specs(self) -> list[ToolSpec]:
        """The specs this policy level exposes at all (denied tools are
        not even advertised to the model)."""
        return [s for s in self.specs if self.policy.allows(s)]

    def execute(self, name: str, payload: dict) -> dict:
        spec = next((s for s in self.specs if s.name == name), None)
        if spec is None:
            return {"error": f"Unknown tool: {name!r}"}
        decision = self.policy.check(spec)
        if not decision.allowed:
            raise ToolDenied(decision.reason)
        try:
            if name == "list_tables":
                return {"tables": self.adapter.list_tables()}
            if name == "describe_table":
                return {"columns": self.adapter.describe_table(payload["table"])}
            if name == "execute_sql":
                return self.adapter.execute_sql(payload["sql"])
        except AdapterError as e:
            return {"error": str(e)}
        except KeyError as e:
            return {"error": f"Missing required argument: {e}"}
        return {"error": f"Tool {name!r} has no execution mapping"}  # pragma: no cover

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
    ToolSpec(
        name="read_context_file",
        description=(
            "Read part of a project context file (see the context index in the system prompt). "
            "Prefer search_context_files first to find WHERE to read, then read the relevant "
            "range — avoid reading whole large files."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The context file name."},
                "offset": {"type": "integer", "description": "1-based line to start from (default 1)."},
                "limit": {"type": "integer", "description": "Max lines to return (default 200)."},
            },
            "required": ["name"],
        },
        risk=Risk.READ,
    ),
    ToolSpec(
        name="search_context_files",
        description=(
            "Search all project context files for a case-insensitive text match. Returns matching "
            "lines with file names and line numbers — use it to locate relevant sections before "
            "reading them with read_context_file."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Text to search for."}},
            "required": ["query"],
        },
        risk=Risk.READ,
    ),
]

READ_DEFAULT_LIMIT = 200
SEARCH_MAX_MATCHES = 40


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
        self,
        *,
        adapter: AuditedAdapter,
        policy: AutonomyPolicy,
        specs: list[ToolSpec] | None = None,
        project=None,
        actor: str = "",
    ):
        self.adapter = adapter
        self.policy = policy
        self.specs = specs if specs is not None else TOOLS
        self.project = project
        self.actor = actor

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
            if name == "read_context_file":
                return self._read_context_file(
                    payload["name"], payload.get("offset") or 1, payload.get("limit") or READ_DEFAULT_LIMIT
                )
            if name == "search_context_files":
                return self._search_context_files(payload["query"])
        except AdapterError as e:
            return {"error": str(e)}
        except KeyError as e:
            return {"error": f"Missing required argument: {e}"}
        return {"error": f"Tool {name!r} has no execution mapping"}  # pragma: no cover

    def _read_context_file(self, file_name: str, offset: int, limit: int) -> dict:
        """Context files live in Diabase's own DB, not behind the adapter,
        so this read is audited here (the trail also shows WHICH context
        the agent consulted before acting). Line-ranged so large files are
        read in targeted slices instead of one giant tool result."""
        from audit.services import record

        if self.project is None:
            return {"error": "No project bound to this toolset"}
        file = self.project.context_files.filter(name=file_name).first()
        if file is None:
            available = list(self.project.context_files.values_list("name", flat=True))
            return {"error": f"No context file named {file_name!r}", "available": available}
        lines = file.content.splitlines()
        offset = max(1, int(offset))
        limit = max(1, int(limit))
        window = lines[offset - 1 : offset - 1 + limit]
        record(
            action="read_context_file",
            actor_type="agent",
            actor=self.actor,
            project=self.project,
            payload_in={"name": file_name, "offset": offset, "limit": limit},
            payload_out={"returned_lines": len(window), "total_lines": len(lines)},
        )
        return {
            "name": file.name,
            "content": "\n".join(window),
            "offset": offset,
            "returned_lines": len(window),
            "total_lines": len(lines),
            "has_more": offset - 1 + limit < len(lines),
        }

    def _search_context_files(self, query: str) -> dict:
        """grep across every context file: the cheap 80% of RAG for text
        docs — locate, then read the exact range."""
        from audit.services import record

        if self.project is None:
            return {"error": "No project bound to this toolset"}
        needle = query.strip().lower()
        if not needle:
            return {"error": "Empty search query"}
        matches = []
        truncated = False
        for file in self.project.context_files.order_by("name"):
            for i, line in enumerate(file.content.splitlines(), start=1):
                if needle in line.lower():
                    if len(matches) >= SEARCH_MAX_MATCHES:
                        truncated = True
                        break
                    matches.append({"file": file.name, "line": i, "text": line.strip()[:200]})
            if truncated:
                break
        record(
            action="search_context_files",
            actor_type="agent",
            actor=self.actor,
            project=self.project,
            payload_in={"query": query},
            payload_out={"matches": len(matches), "truncated": truncated},
        )
        return {"query": query, "matches": matches, "truncated": truncated}

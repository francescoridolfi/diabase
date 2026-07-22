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
    # tools bound to an adapter capability ("functions", …) are only
    # advertised — and executable — when the instance supports it
    capability: str | None = None


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
        name="query_sql",
        description=(
            "Run a single READ-ONLY SQL statement (SELECT over tables or system catalogs like "
            "pg_policies, pg_indexes, information_schema). The database itself rejects writes on "
            "this path, so use it freely to inspect state; use execute_sql for anything that mutates."
        ),
        input_schema={
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "One read-only SQL statement."}},
            "required": ["sql"],
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
        name="list_functions",
        description="List the project's edge functions (slug, status, version, last update).",
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.READ,
        capability="functions",
    ),
    ToolSpec(
        name="read_function",
        description="Read the deployed source of an edge function.",
        input_schema={
            "type": "object",
            "properties": {"slug": {"type": "string", "description": "The function slug."}},
            "required": ["slug"],
        },
        risk=Risk.READ,
        capability="functions",
    ),
    ToolSpec(
        name="deploy_function",
        description=(
            "Create or update an edge function with the given TypeScript source (single file, "
            "Deno runtime). Read the current source first when updating — the user reviews a diff."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "slug": {"type": "string", "description": "Function slug (letters, digits, - _)."},
                "body": {"type": "string", "description": "The complete function source."},
                "name": {"type": "string", "description": "Display name (defaults to the slug)."},
                "verify_jwt": {"type": "boolean", "description": "Require a valid JWT (default true)."},
            },
            "required": ["slug", "body"],
        },
        risk=Risk.WRITE,
        capability="functions",
    ),
    ToolSpec(
        name="delete_function",
        description="Delete an edge function.",
        input_schema={
            "type": "object",
            "properties": {"slug": {"type": "string", "description": "The function slug."}},
            "required": ["slug"],
        },
        risk=Risk.WRITE,
        capability="functions",
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
        turn=None,
    ):
        self.adapter = adapter
        self.policy = policy
        self.specs = specs if specs is not None else TOOLS
        self.project = project
        self.actor = actor
        self.turn = turn  # the running Turn: where queued plan steps attach

    def _supported(self, spec: ToolSpec) -> bool:
        return spec.capability is None or spec.capability in getattr(self.adapter, "capabilities", ())

    def allowed_specs(self) -> list[ToolSpec]:
        """The specs this policy level exposes at all: denied tools are
        not even advertised to the model, and neither are tools whose
        capability the instance doesn't have."""
        return [s for s in self.specs if self._supported(s) and self.policy.allows(s)]

    def execute(self, name: str, payload: dict) -> dict:
        spec = next((s for s in self.specs if s.name == name), None)
        if spec is None:
            return {"error": f"Unknown tool: {name!r}"}
        if not self._supported(spec):
            return {"error": f"Tool {name!r} is not supported by this instance ({spec.capability})"}
        decision = self.policy.check(spec)
        if not decision.allowed:
            raise ToolDenied(decision.reason)
        if decision.requires_plan:
            return self._queue_step(spec, payload)
        try:
            if name == "list_tables":
                return {"tables": self.adapter.list_tables()}
            if name == "describe_table":
                return {"columns": self.adapter.describe_table(payload["table"])}
            if name == "query_sql":
                return self.adapter.query_sql(payload["sql"])
            if name == "execute_sql":
                return self.adapter.execute_sql(payload["sql"])
            if name == "list_functions":
                return {"functions": self.adapter.list_functions()}
            if name == "read_function":
                return {"slug": payload["slug"], "body": self.adapter.get_function_body(payload["slug"])}
            if name == "deploy_function":
                return self.adapter.deploy_function(
                    payload["slug"],
                    payload["body"],
                    name=payload.get("name") or "",
                    verify_jwt=payload.get("verify_jwt", True),
                )
            if name == "delete_function":
                return self.adapter.delete_function(payload["slug"])
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

    def _queue_step(self, spec: ToolSpec, payload: dict) -> dict:
        """The plan gate: instead of executing, append the call to the
        turn's draft Plan and tell the model it was queued. The model
        keeps planning in the same turn; nothing touches the instance
        until the user approves. Backends detect the "planned" key to
        emit ToolCallPlanned instead of ToolCallFinished."""
        from audit.services import record

        from .models import Plan, PlanStep

        if self.turn is None or self.project is None:
            return {"error": "This project requires a plan for writes, but no turn is bound to queue into"}
        plan, _ = Plan.objects.get_or_create(
            turn=self.turn, status="draft", defaults={"project": self.project}
        )
        step = PlanStep.objects.create(
            plan=plan,
            order=plan.steps.count() + 1,
            tool=spec.name,
            payload=payload,
            meta=self._step_review_meta(spec, payload),
        )
        record(
            action="plan.step_queued",
            actor_type="agent",
            actor=self.actor,
            project=self.project,
            payload_in={"plan": plan.pk, "step": step.order, "tool": spec.name, **payload},
        )
        return {
            "planned": {"plan": plan.pk, "step": step.order},
            "note": (
                "Queued as step "
                f"{step.order} of the proposed plan — it will run only after the user approves. "
                "Queue any further writes, then summarize the plan for the user."
            ),
        }

    def _step_review_meta(self, spec: ToolSpec, payload: dict) -> dict:
        """Review context captured at QUEUE time, so the card shows what
        the decision is really about. For deploy_function: a unified diff
        against the live source (the fetch is an audited read)."""
        if spec.name != "deploy_function":
            return {}
        import difflib

        slug = str(payload.get("slug") or "")
        proposed = str(payload.get("body") or "")
        try:
            current = self.adapter.get_function_body(slug)
            exists = True
        except Exception:  # new function, bundle-deployed, or API hiccup
            current = ""
            exists = False
        diff = "\n".join(
            difflib.unified_diff(
                current.splitlines(),
                proposed.splitlines(),
                fromfile=f"{slug} (live)",
                tofile=f"{slug} (proposed)",
                lineterm="",
            )
        )
        return {"updates_existing": exists, "diff": diff if exists else ""}

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

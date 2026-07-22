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
        name="list_buckets",
        description="List the project's storage buckets (name, public flag).",
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.READ,
        capability="storage",
    ),
    ToolSpec(
        name="create_bucket",
        description=(
            "Create a storage bucket. Buckets are private by default; storage POLICIES "
            "(RLS on storage.objects) are plain SQL — write them with execute_sql."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Bucket name (letters, digits, . _ -)."},
                "public": {
                    "type": "boolean",
                    "description": "Objects readable without auth (default false).",
                },
                "file_size_limit": {
                    "type": "integer",
                    "description": "Max object size in bytes (omit for none).",
                },
                "allowed_mime_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Accepted MIME types (omit for any).",
                },
            },
            "required": ["name"],
        },
        risk=Risk.WRITE,
        capability="storage",
    ),
    ToolSpec(
        name="update_bucket",
        description=(
            "Change a bucket's settings: pass only what changes. file_size_limit 0 and "
            "allowed_mime_types [] clear their restriction."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "The bucket to update."},
                "public": {"type": "boolean", "description": "Objects readable without auth."},
                "file_size_limit": {
                    "type": "integer",
                    "description": "Max object size in bytes (0 clears it).",
                },
                "allowed_mime_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Accepted MIME types ([] clears the restriction).",
                },
            },
            "required": ["name"],
        },
        risk=Risk.WRITE,
        capability="storage",
    ),
    ToolSpec(
        name="delete_bucket",
        description="Delete an EMPTY storage bucket (the server rejects non-empty ones).",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "description": "The bucket to delete."}},
            "required": ["name"],
        },
        risk=Risk.WRITE,
        capability="storage",
    ),
    ToolSpec(
        name="get_auth_config",
        description=(
            "Read the project's auth (GoTrue) configuration: signup, providers, email templates, "
            "security settings. Secret values are masked as ***set*** — you never see them."
        ),
        input_schema={"type": "object", "properties": {}, "required": []},
        risk=Risk.READ,
        capability="auth_config",
    ),
    ToolSpec(
        name="update_auth_config",
        description=(
            "Change auth configuration: pass ONLY the keys to change (the user reviews each one, "
            "and email template changes as an HTML diff). Secrets (smtp_pass, oauth secrets...) "
            "cannot be set through Diabase — the user enters them in the Supabase dashboard."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "changes": {
                    "type": "object",
                    "description": "The auth config keys to change, with their new values.",
                }
            },
            "required": ["changes"],
        },
        risk=Risk.WRITE,
        capability="auth_config",
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
        if name == "update_auth_config":
            # secrets must not even be QUEUED: a queued step's payload is
            # stored and rendered in the plan card
            from instances.adapters import is_auth_secret

            secrets = sorted(k for k in (payload.get("changes") or {}) if is_auth_secret(k))
            if secrets:
                return {
                    "error": (
                        f"Refusing to set secret keys through Diabase: {', '.join(secrets)} — "
                        "ask the user to enter them in the Supabase dashboard"
                    )
                }
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
                return self._read_function(payload["slug"])
            if name == "deploy_function":
                out = self.adapter.deploy_function(
                    payload["slug"],
                    payload["body"],
                    name=payload.get("name") or "",
                    verify_jwt=payload.get("verify_jwt", True),
                )
                self._track_deploy(payload, out)
                return out
            if name == "delete_function":
                out = self.adapter.delete_function(payload["slug"])
                self._untrack(payload["slug"])
                return out
            if name == "list_buckets":
                return {"buckets": self.adapter.list_buckets()}
            if name == "create_bucket":
                return self.adapter.create_bucket(
                    payload["name"],
                    public=payload.get("public", False),
                    file_size_limit=payload.get("file_size_limit"),
                    allowed_mime_types=payload.get("allowed_mime_types"),
                )
            if name == "update_bucket":
                return self.adapter.update_bucket(
                    payload["name"],
                    public=payload.get("public"),
                    file_size_limit=payload.get("file_size_limit"),
                    allowed_mime_types=payload.get("allowed_mime_types"),
                )
            if name == "delete_bucket":
                return self.adapter.delete_bucket(payload["name"])
            if name == "get_auth_config":
                return {"config": self.adapter.get_auth_config()}
            if name == "update_auth_config":
                return self.adapter.update_auth_config(payload["changes"])
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

    def _tracked_source(self, slug: str):
        """The locally kept source (EdgeFunctionSource), if any."""
        from instances.services import get_function_source

        if self.project is None:
            return None
        return get_function_source(self.project.server, slug)

    def _read_function(self, slug: str) -> dict:
        """Local-first: Diabase keeps the source of every function it
        deployed. Untracked functions (deployed outside Diabase) fall
        back to the API body — readable only for legacy deployments."""
        src = self._tracked_source(slug)
        if src is not None:
            return {
                "slug": slug,
                "body": src.body,
                "tracked": True,
                "deployed_version": src.deployed_version,
                "note": (
                    "Source tracked by Diabase at version "
                    f"{src.deployed_version} — compare with list_functions to spot deploys "
                    "made outside Diabase."
                ),
            }
        return {
            "slug": slug,
            "body": self.adapter.get_function_body(slug),
            "tracked": False,
            "note": "This function was deployed outside Diabase: source read from the API.",
        }

    def _track_deploy(self, payload: dict, out: dict):
        from instances.services import save_function_source

        if self.project is None or "error" in out:
            return
        save_function_source(
            self.project.server,
            payload["slug"],
            payload["body"],
            name=payload.get("name") or "",
            verify_jwt=payload.get("verify_jwt", True),
            version=out.get("version"),
            actor=self.actor,
        )

    def _untrack(self, slug: str):
        from instances.services import delete_function_source

        if self.project is not None:
            delete_function_source(self.project.server, slug)

    def _step_review_meta(self, spec: ToolSpec, payload: dict) -> dict:
        """Review context captured at QUEUE time, so the card shows what
        the decision is really about. For deploy_function: a unified diff
        against the source we track locally (or the API body for legacy
        deployments). For update_auth_config: each changed key with its
        live value, long text (email templates) as a unified diff."""
        if spec.name == "update_auth_config":
            return self._auth_review_meta(payload)
        if spec.name != "deploy_function":
            return {}
        import difflib

        slug = str(payload.get("slug") or "")
        proposed = str(payload.get("body") or "")
        src = self._tracked_source(slug)
        if src is not None:
            current, exists = src.body, True
        else:
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

    # long-text auth values (email templates above all) review better as a
    # diff than as a from→to pair
    AUTH_DIFF_THRESHOLD = 120

    def _auth_review_meta(self, payload: dict) -> dict:
        import difflib

        changes = payload.get("changes") or {}
        if not isinstance(changes, dict) or not changes:
            return {}
        try:
            current = self.adapter.get_auth_config()
        except Exception:  # config unreadable: review falls back to new values only
            current = {}
        fields, diffs = [], []
        for key in sorted(changes):
            live, proposed = current.get(key), changes[key]
            long_text = isinstance(proposed, str) and (
                "\n" in proposed or len(proposed) > self.AUTH_DIFF_THRESHOLD
            )
            if long_text:
                diffs.append(
                    "\n".join(
                        difflib.unified_diff(
                            str(live or "").splitlines(),
                            proposed.splitlines(),
                            fromfile=f"{key} (live)",
                            tofile=f"{key} (proposed)",
                            lineterm="",
                        )
                    )
                )
            else:
                fields.append({"key": key, "from": live, "to": proposed})
        return {"changes": fields, "diff": "\n".join(d for d in diffs if d)}

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

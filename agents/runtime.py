"""The orchestrator: run_turn() and start_turn() are the ONLY entry points
to execute an agent.

Both guarantee the invariants the backends cannot break:
- the adapter is always wrapped in AuditedAdapter (no unaudited path)
- the policy is always attached to the toolset
- the user message and the reply are persisted and audited
- every execution leaves a Turn row (backend, model, duration, outcome)

run_turn() is a plain synchronous generator — used directly by tests and
any code that wants to drive a turn to completion in-process.

start_turn() is what the GUI uses: it returns as soon as the Turn row
exists, and runs the actual turn in a background thread, persisting
every event to TurnEvent as it happens. This decouples a turn's lifetime
from the HTTP request that started it — a page refresh, or any number of
tabs, can reconnect to the same turn's event stream mid-flight instead
of losing it.
"""

import dataclasses
import os
import threading
from collections.abc import Iterator

from django.utils import timezone

from audit.services import AuditedAdapter, record
from instances.adapters import get_adapter
from workspaces.models import ChatMessage, Project

from .backends.anthropic_api import AnthropicAPIBackend
from .backends.base import AgentBackend, PlanProposed, TurnCompleted, TurnEvent, TurnFailed
from .backends.claude_code import ClaudeCodeBackend
from .backends.openai_compat import OpenAICompatBackend
from .policy import DEFAULT_LEVEL, AutonomyPolicy
from .prompts import build_system_prompt
from .tools import BoundToolset

BACKENDS: dict[str, type[AgentBackend]] = {
    "anthropic_api": AnthropicAPIBackend,
    "claude_code": ClaudeCodeBackend,
    "openai_compat": OpenAICompatBackend,
}


def get_backend(name: str | None = None, project: Project | None = None) -> AgentBackend:
    """Resolution order: explicit name → the project's configured
    connection → AGENT_BACKEND env → first available backend.

    A connection carries backend family, model, endpoint and (encrypted)
    API key — configured once on the Connections page, selected per
    project."""
    conn = project.agent_connection if project else None
    if not name and conn:
        if conn.backend == "openai_compat":
            return OpenAICompatBackend(
                base_url=conn.base_url or None, api_key=conn.api_key or None, model=conn.model or None
            )
        if conn.backend == "anthropic_api":
            return AnthropicAPIBackend(model=conn.model or None, api_key=conn.api_key or None)
        return ClaudeCodeBackend(model=conn.model or None)

    name = name or os.environ.get("AGENT_BACKEND", "").strip()
    if name:
        if name not in BACKENDS:
            raise ValueError(f"Unknown agent backend: {name!r} (expected one of {sorted(BACKENDS)})")
        return BACKENDS[name]()
    for cls in BACKENDS.values():
        backend = cls()
        if backend.availability()[0]:
            return backend
    raise RuntimeError("No agent backend is available (see each backend's configuration)")


def _prepare(project: Project, backend: AgentBackend, autonomy_level: str | None, turn=None):
    server = project.server
    level = autonomy_level or getattr(project, "autonomy_level", None) or DEFAULT_LEVEL
    adapter = AuditedAdapter(
        get_adapter(server.adapter_type, server.dsn),
        project=project,
        actor_type="agent",
        actor=backend.name,
    )
    toolset = BoundToolset(
        adapter=adapter, policy=AutonomyPolicy(level), project=project, actor=backend.name, turn=turn
    )
    history = [
        {"role": m.role, "content": m.content}
        for m in project.messages.all()
        if m.role in ("user", "assistant") and m.content.strip()
    ]
    return toolset, history


def _start(
    project: Project,
    user_message: str,
    user: str,
    backend_name: str | None,
    autonomy_level: str | None,
    message_kind: str = "chat",
):
    """Shared synchronous setup: resolve backend, create the Turn row,
    persist + audit the user's message. Both run_turn and start_turn need
    exactly this, before execution (sync or threaded) takes over."""
    from .models import Turn

    backend = get_backend(backend_name, project)
    turn = Turn.objects.create(
        project=project,
        backend=backend.name,
        model=getattr(backend, "model", ""),
        user_message=user_message,
    )
    # the toolset needs the Turn: queued plan steps attach to it
    toolset, history = _prepare(project, backend, autonomy_level, turn)
    ChatMessage.objects.create(project=project, role="user", content=user_message, kind=message_kind)
    record(
        action="chat.message",
        actor_type="user" if message_kind == "chat" else "system",
        actor=user,
        project=project,
        payload_in={"message": user_message},
    )
    return turn, backend, toolset, history


def _drive(
    turn, project: Project, backend: AgentBackend, toolset, history, user_message: str
) -> Iterator[TurnEvent]:
    def finalize(status: str, *, reply: str = "", error: str = ""):
        turn.status = status
        turn.error = error
        turn.finished_at = timezone.now()
        turn.save(update_fields=["status", "error", "finished_at"])
        if reply:
            ChatMessage.objects.create(project=project, role="assistant", content=reply)
            record(
                action="chat.reply",
                actor_type="agent",
                actor=backend.name,
                project=project,
                payload_out={"reply": reply},
            )

    try:
        for event in backend.run(
            system_prompt=build_system_prompt(project),
            history=history,
            user_message=user_message,
            toolset=toolset,
        ):
            if isinstance(event, TurnCompleted):
                finalize("completed", reply=event.reply)
                plan = _propose_draft_plan(turn, backend.name)
                if plan:
                    yield PlanProposed(plan_id=plan.pk, steps=plan.steps.count())
            elif isinstance(event, TurnFailed):
                finalize("failed", error=event.error)
                _discard_draft_plan(turn)
            yield event
    except Exception as e:
        finalize("failed", error=f"{type(e).__name__}: {e}")
        _discard_draft_plan(turn)
        yield TurnFailed(error=f"{type(e).__name__}: {e}")


def _propose_draft_plan(turn, actor: str):
    """A completed turn that queued write steps leaves them as a proposed
    plan for the user to decide on. Empty drafts are discarded."""
    plan = turn.plans.filter(status="draft").first()
    if plan is None:
        return None
    if not plan.steps.exists():
        plan.delete()
        return None
    plan.status = "proposed"
    plan.save(update_fields=["status"])
    record(
        action="plan.proposed",
        actor_type="agent",
        actor=actor,
        project=turn.project,
        payload_out={"plan": plan.pk, "steps": plan.steps.count()},
    )
    return plan


def _discard_draft_plan(turn):
    """A failed turn's half-built draft must never reach the user."""
    turn.plans.filter(status="draft").update(status="superseded")


def run_turn(
    project: Project,
    user_message: str,
    *,
    backend_name: str | None = None,
    autonomy_level: str | None = None,
    user: str = "",
) -> Iterator[TurnEvent]:
    """Drive a turn to completion synchronously, yielding events as they
    happen. Used by tests and any in-process caller."""
    turn, backend, toolset, history = _start(project, user_message, user, backend_name, autonomy_level)
    yield from _drive(turn, project, backend, toolset, history, user_message)


def start_turn(
    project: Project,
    user_message: str,
    *,
    backend_name: str | None = None,
    autonomy_level: str | None = None,
    user: str = "",
    message_kind: str = "chat",
):
    """Create the Turn synchronously, execute it on a background thread.

    Every event is persisted to TurnEvent as it's produced. The caller
    gets the Turn back immediately (its pk is what the GUI subscribes
    to); the HTTP request that called this never has to stay open for
    the turn's whole duration.
    """
    from .models import TurnEvent as TurnEventRow

    turn, backend, toolset, history = _start(
        project, user_message, user, backend_name, autonomy_level, message_kind
    )

    def worker():
        try:
            for event in _drive(turn, project, backend, toolset, history, user_message):
                TurnEventRow.objects.create(
                    turn=turn, kind=type(event).__name__, data=dataclasses.asdict(event)
                )
        finally:
            from django.db import connections

            connections.close_all()  # this thread's connection must not linger in the pool

    threading.Thread(target=worker, daemon=True).start()
    return turn


# ---------------------------------------------------------------------------
# Plan decisions (phase 2): every path below is user-initiated and audited.
# ---------------------------------------------------------------------------

STEP_OUTPUT_PREVIEW_CHARS = 500


def approve_plan(plan, *, user: str = ""):
    """Mark the plan approved and apply it on a background thread.

    Each step runs through an AuditedAdapter with the USER as actor — the
    trail shows who authorized every statement. When the last step is done
    (or one fails), the real results are handed back to the agent as an
    automatic continuation turn (the incremental-apply loop the user chose
    over blind multi-step plans).
    """
    _decide(plan, "applying", "plan.approved", user)

    def worker():
        try:
            _apply(plan, user)
        finally:
            from django.db import connections

            connections.close_all()

    threading.Thread(target=worker, daemon=True).start()
    return plan


def reject_plan(plan, *, user: str = ""):
    """Reject without executing anything; the agent learns about it from
    a plan_result message the next time it runs."""
    _decide(plan, "rejected", "plan.rejected", user)
    ChatMessage.objects.create(
        project=plan.project,
        role="user",
        kind="plan_result",
        content=f"Plan #{plan.pk} was rejected by the user. None of its steps were executed.",
    )
    return plan


def revise_plan(plan, comment: str, *, user: str = ""):
    """Supersede the plan and hand the user's comment to the agent as a
    fresh turn — it proposes a new plan informed by the feedback."""
    _decide(plan, "superseded", "plan.superseded", user)
    message = f"Plan #{plan.pk} was not applied. Revise it: {comment}"
    return start_turn(plan.project, message, user=user)


def _decide(plan, status: str, action: str, user: str):
    from django.utils import timezone as tz

    plan.status = status
    plan.decided_by = user
    plan.decided_at = tz.now()
    plan.save(update_fields=["status", "decided_by", "decided_at"])
    record(
        action=action,
        actor_type="user",
        actor=user,
        project=plan.project,
        payload_in={"plan": plan.pk, "steps": plan.steps.count()},
    )


def _apply(plan, user: str):
    """Execute the approved steps in order; stop at the first failure."""
    project = plan.project
    server = project.server
    adapter = AuditedAdapter(
        get_adapter(server.adapter_type, server.dsn), project=project, actor_type="user", actor=user
    )
    failed = False
    for step in plan.steps.order_by("order"):
        if failed:
            step.status = "skipped"
            step.save(update_fields=["status"])
            continue
        if step.tool == "execute_sql":
            try:
                output = adapter.execute_sql(step.payload.get("sql", ""))
            except Exception as e:
                output = {"error": f"{type(e).__name__}: {e}"}
        else:
            output = {"error": f"Plan step has no executor for tool {step.tool!r}"}
        step.output = output if isinstance(output, dict) else {"result": output}
        step.status = "failed" if "error" in step.output else "applied"
        step.save(update_fields=["status", "output"])
        if step.status == "failed":
            failed = True
            plan.error = str(step.output.get("error", ""))

    plan.status = "failed" if failed else "applied"
    plan.save(update_fields=["status", "error"])
    record(
        action="plan.applied" if not failed else "plan.apply_failed",
        actor_type="user",
        actor=user,
        project=project,
        payload_out={"plan": plan.pk, "steps": plan.steps.count()},
        outcome="success" if not failed else "error",
        error=plan.error,
    )

    # incremental apply: the agent continues from the REAL results
    turn = start_turn(project, _apply_report(plan), user=user, message_kind="plan_result")
    plan.continuation_turn = turn
    plan.save(update_fields=["continuation_turn"])


def _apply_report(plan) -> str:
    """The plan_result message: what actually happened, step by step."""
    import json

    lines = [
        f"Plan #{plan.pk} was approved and applied."
        if plan.status == "applied"
        else f"Plan #{plan.pk} was approved but FAILED while applying."
    ]
    for step in plan.steps.order_by("order"):
        preview = json.dumps(step.output, default=str)[:STEP_OUTPUT_PREVIEW_CHARS]
        if step.status == "skipped":
            lines.append(f"Step {step.order} ({step.tool}): skipped (a previous step failed).")
        else:
            lines.append(f"Step {step.order} ({step.tool}): {step.status} — {preview}")
    lines.append(
        "Verify the outcome and continue toward the goal."
        if plan.status == "applied"
        else "Revise your approach based on the error above and propose a new plan."
    )
    return "\n".join(lines)

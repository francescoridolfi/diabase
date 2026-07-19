"""The orchestrator: run_turn() is the ONLY entry point to execute an agent.

It guarantees the invariants the backends cannot break:
- the adapter is always wrapped in AuditedAdapter (no unaudited path)
- the policy is always attached to the toolset
- the user message and the reply are persisted and audited
- every execution leaves a Turn row (backend, model, duration, outcome)
"""

import os
from collections.abc import Iterator

from django.utils import timezone

from audit.services import AuditedAdapter, record
from instances.adapters import get_adapter
from workspaces.models import ChatMessage, Project

from .backends.anthropic_api import AnthropicAPIBackend
from .backends.base import AgentBackend, TurnCompleted, TurnEvent, TurnFailed
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


def get_backend(name: str | None = None) -> AgentBackend:
    """Explicit name, or AGENT_BACKEND env, or first available backend."""
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


def run_turn(
    project: Project,
    user_message: str,
    *,
    backend_name: str | None = None,
    autonomy_level: str | None = None,
    user: str = "",
) -> Iterator[TurnEvent]:
    from .models import Turn

    backend = get_backend(backend_name)
    server = project.server
    level = autonomy_level or getattr(project, "autonomy_level", None) or DEFAULT_LEVEL
    adapter = AuditedAdapter(
        get_adapter(server.adapter_type, server.dsn),
        project=project,
        actor_type="agent",
        actor=backend.name,
    )
    toolset = BoundToolset(adapter=adapter, policy=AutonomyPolicy(level), project=project, actor=backend.name)

    history = [
        {"role": m.role, "content": m.content}
        for m in project.messages.all()
        if m.role in ("user", "assistant") and m.content.strip()
    ]

    turn = Turn.objects.create(
        project=project,
        backend=backend.name,
        model=getattr(backend, "model", ""),
        user_message=user_message,
    )
    ChatMessage.objects.create(project=project, role="user", content=user_message)
    record(
        action="chat.message",
        actor_type="user",
        actor=user,
        project=project,
        payload_in={"message": user_message},
    )

    def finalize(status: str, *, reply: str = "", error: str = ""):
        turn.status = status
        turn.error = error
        turn.finished_at = timezone.now()
        turn.save()
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
            elif isinstance(event, TurnFailed):
                finalize("failed", error=event.error)
            yield event
    except Exception as e:
        finalize("failed", error=f"{type(e).__name__}: {e}")
        yield TurnFailed(error=f"{type(e).__name__}: {e}")

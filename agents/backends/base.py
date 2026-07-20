"""The backend contract: every LLM provider implements this, nothing more.

A backend receives a fully prepared turn (system prompt, history, bound
toolset) and yields typed events as it works. Everything that makes
Diabase safe — policy checks, audit, persistence — lives OUTSIDE the
backends: they only translate between a provider's wire format and the
event stream.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field


@dataclass(frozen=True)
class TurnEvent:
    pass


@dataclass(frozen=True)
class TextDelta(TurnEvent):
    """A chunk of assistant text (granularity depends on the backend)."""

    text: str


@dataclass(frozen=True)
class ToolCallStarted(TurnEvent):
    tool: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallFinished(TurnEvent):
    tool: str
    payload: dict = field(default_factory=dict)
    output: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCallPlanned(TurnEvent):
    """A write call intercepted by the plan gate: queued as a plan step
    instead of executing (see agents.tools.BoundToolset._queue_step)."""

    tool: str
    payload: dict = field(default_factory=dict)
    step: int = 0


@dataclass(frozen=True)
class PlanProposed(TurnEvent):
    """Emitted by the runtime when a completed turn leaves a plan with
    steps waiting for the user's decision."""

    plan_id: int
    steps: int = 0


@dataclass(frozen=True)
class ToolCallDenied(TurnEvent):
    tool: str
    payload: dict = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class TurnCompleted(TurnEvent):
    reply: str


@dataclass(frozen=True)
class TurnFailed(TurnEvent):
    error: str


class AgentBackend(ABC):
    """One LLM provider. Stateless: configuration comes in via __init__."""

    name: str = ""

    @abstractmethod
    def run(
        self,
        *,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        toolset,
    ) -> Iterator[TurnEvent]:
        """Execute one agentic turn, yielding events until TurnCompleted/TurnFailed.

        `history` is a list of {"role": "user"|"assistant", "content": str}.
        `toolset` is a BoundToolset: the ONLY way a backend may touch an
        instance — execution, policy and audit are inside it.
        """

    @abstractmethod
    def availability(self) -> tuple[bool, str]:
        """(available, human-readable reason when not)."""

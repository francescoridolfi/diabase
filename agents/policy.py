"""Autonomy policy: what the agent may do is decided in our code, not in
the prompt. Every tool call passes through `check()` before execution.

Levels:
- read_only  → inspection tools only (Risk.READ)
- plan       → reads execute; writes are queued into a Plan the user
               must approve before anything touches the instance
- full       → everything, always audited
"""

from dataclasses import dataclass

AUTONOMY_LEVELS = ["read_only", "plan", "full"]
DEFAULT_LEVEL = "plan"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    requires_plan: bool = False


class AutonomyPolicy:
    def __init__(self, level: str = DEFAULT_LEVEL):
        if level not in AUTONOMY_LEVELS:
            raise ValueError(f"Unknown autonomy level: {level!r} (expected one of {AUTONOMY_LEVELS})")
        self.level = level

    def allows(self, spec) -> bool:
        return self.check(spec).allowed

    def check(self, spec) -> Decision:
        if self.level == "read_only" and spec.risk != "read":
            return Decision(
                allowed=False,
                reason=f"Tool {spec.name!r} is blocked: this project is in read-only mode",
            )
        if self.level == "plan" and spec.risk != "read":
            return Decision(allowed=True, requires_plan=True)
        return Decision(allowed=True)

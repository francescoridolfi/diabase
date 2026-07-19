"""Autonomy policy: what the agent may do is decided in our code, not in
the prompt. Every tool call passes through `check()` before execution.

Phase 1 levels:
- read_only  → inspection tools only (Risk.READ)
- full       → everything, always audited

The Decision contract already includes `requires_plan` so phase 2
(plan & approve) plugs in here without touching the backends.
"""

from dataclasses import dataclass

AUTONOMY_LEVELS = ["read_only", "full"]
DEFAULT_LEVEL = "full"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    requires_plan: bool = False  # phase 2 hook


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
        return Decision(allowed=True)

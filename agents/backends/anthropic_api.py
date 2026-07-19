"""Anthropic Messages API backend (pay-per-use).

Runs OUR agentic loop on the plain Messages API: tools come from the
registry, every call goes through the BoundToolset, policy denials are
reported to the model as tool errors (it cannot negotiate them away).
"""

import os
from collections.abc import Iterator

from ..tools import BoundToolset, ToolDenied
from .base import (
    AgentBackend,
    TextDelta,
    ToolCallDenied,
    ToolCallFinished,
    ToolCallStarted,
    TurnCompleted,
    TurnEvent,
    TurnFailed,
)

DEFAULT_MODEL = "claude-sonnet-5"
MAX_ITERATIONS = 15
MAX_TOKENS = 8192


class AnthropicAPIBackend(AgentBackend):
    name = "anthropic_api"

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("DIABASE_ANTHROPIC_MODEL", DEFAULT_MODEL)

    def availability(self) -> tuple[bool, str]:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return True, ""
        return False, "ANTHROPIC_API_KEY is not set"

    def run(
        self, *, system_prompt: str, history: list[dict], user_message: str, toolset: BoundToolset
    ) -> Iterator[TurnEvent]:
        import anthropic

        client = anthropic.Anthropic()
        tools = [
            {"name": s.name, "description": s.description, "input_schema": s.input_schema}
            for s in toolset.allowed_specs()
        ]
        messages = [*history, {"role": "user", "content": user_message}]
        reply_parts: list[str] = []

        try:
            for _ in range(MAX_ITERATIONS):
                response = client.messages.create(
                    model=self.model,
                    max_tokens=MAX_TOKENS,
                    # cache breakpoint: the system prompt is deterministic by design
                    # (see workspaces.context), so later turns pay ~10% on this part
                    system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
                    tools=tools,
                    messages=messages,
                )
                tool_results = []
                for block in response.content:
                    if block.type == "text" and block.text:
                        reply_parts.append(block.text)
                        yield TextDelta(text=block.text)
                    elif block.type == "tool_use":
                        payload = dict(block.input or {})
                        yield ToolCallStarted(tool=block.name, payload=payload)
                        try:
                            output = toolset.execute(block.name, payload)
                            yield ToolCallFinished(tool=block.name, payload=payload, output=output)
                        except ToolDenied as e:
                            output = {"error": f"Denied by policy: {e}"}
                            yield ToolCallDenied(tool=block.name, payload=payload, reason=str(e))
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": _as_result_text(output),
                            }
                        )
                if response.stop_reason == "tool_use":
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})
                    continue
                yield TurnCompleted(reply="".join(reply_parts) or "(no reply)")
                return
            yield TurnFailed(error=f"Agent did not converge within {MAX_ITERATIONS} iterations")
        except anthropic.AuthenticationError:
            yield TurnFailed(error="Invalid API key: check ANTHROPIC_API_KEY")
        except anthropic.APIStatusError as e:
            yield TurnFailed(error=f"Anthropic API error ({e.status_code}): {e.message}")
        except anthropic.APIConnectionError:
            yield TurnFailed(error="Cannot reach the Anthropic API (network)")


def _as_result_text(output: dict) -> str:
    import json

    return json.dumps(output, default=str)

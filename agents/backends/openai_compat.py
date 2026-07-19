"""OpenAI-compatible backend: one implementation, most of the ecosystem.

Speaks the chat-completions protocol against a configurable endpoint —
OpenAI itself, but also local models (Ollama, vLLM), Mistral, Groq,
DeepSeek, OpenRouter and anything else exposing the same API. The
agentic loop is ours: every iteration passes through toolset + policy
in our code, with no provider SDK in between.

Configuration (per-project settings arrive with the llm-settings PR):
- DIABASE_OPENAI_BASE_URL  (default https://api.openai.com/v1)
- DIABASE_OPENAI_API_KEY   (optional for local endpoints like Ollama)
- DIABASE_OPENAI_MODEL
"""

import json
import os
import urllib.error
import urllib.request
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

MAX_ITERATIONS = 15


class OpenAICompatBackend(AgentBackend):
    name = "openai_compat"

    def __init__(self, base_url: str | None = None, api_key: str | None = None, model: str | None = None):
        self.base_url = (
            base_url or os.environ.get("DIABASE_OPENAI_BASE_URL", "https://api.openai.com/v1")
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("DIABASE_OPENAI_API_KEY", "")
        self.model = model or os.environ.get("DIABASE_OPENAI_MODEL", "")

    def availability(self) -> tuple[bool, str]:
        if not self.model:
            return False, "DIABASE_OPENAI_MODEL is not set"
        return True, ""

    def _request(self, payload: dict) -> dict:
        headers = {"Content-Type": "application/json", "User-Agent": "diabase/0.1"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(  # noqa: S310 — endpoint configured by the operator
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 # nosec B310
            return json.loads(resp.read().decode())

    def run(
        self, *, system_prompt: str, history: list[dict], user_message: str, toolset: BoundToolset
    ) -> Iterator[TurnEvent]:
        tools = [
            {
                "type": "function",
                "function": {"name": s.name, "description": s.description, "parameters": s.input_schema},
            }
            for s in toolset.allowed_specs()
        ]
        messages = [
            {"role": "system", "content": system_prompt},
            *history,
            {"role": "user", "content": user_message},
        ]
        reply_parts: list[str] = []

        try:
            for _ in range(MAX_ITERATIONS):
                data = self._request({"model": self.model, "messages": messages, "tools": tools})
                message = data["choices"][0]["message"]
                if message.get("content"):
                    reply_parts.append(message["content"])
                    yield TextDelta(text=message["content"])
                tool_calls = message.get("tool_calls") or []
                if not tool_calls:
                    yield TurnCompleted(reply="".join(reply_parts) or "(no reply)")
                    return
                messages.append(message)
                for call in tool_calls:
                    name = call["function"]["name"]
                    try:
                        payload = json.loads(call["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        payload = {}
                        output = {"error": "Malformed tool arguments (invalid JSON)"}
                        yield ToolCallStarted(tool=name, payload=payload)
                        yield ToolCallFinished(tool=name, payload=payload, output=output)
                        messages.append(_tool_result(call["id"], output))
                        continue
                    yield ToolCallStarted(tool=name, payload=payload)
                    try:
                        output = toolset.execute(name, payload)
                        yield ToolCallFinished(tool=name, payload=payload, output=output)
                    except ToolDenied as e:
                        output = {"error": f"Denied by policy: {e}"}
                        yield ToolCallDenied(tool=name, payload=payload, reason=str(e))
                    messages.append(_tool_result(call["id"], output))
            yield TurnFailed(error=f"Agent did not converge within {MAX_ITERATIONS} iterations")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            yield TurnFailed(error=f"LLM endpoint error {e.code}: {detail}")
        except urllib.error.URLError as e:
            yield TurnFailed(error=f"LLM endpoint unreachable: {e.reason}")
        except (KeyError, IndexError, json.JSONDecodeError) as e:
            yield TurnFailed(error=f"Unexpected response shape from LLM endpoint: {type(e).__name__}: {e}")


def _tool_result(call_id: str, output: dict) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": json.dumps(output, default=str)}

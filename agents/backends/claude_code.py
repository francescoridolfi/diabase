"""Claude Code backend (subscription, via the Claude Agent SDK).

Uses the user's Claude Code login — no API key. Registry tools are
exposed as an in-process MCP server; the agent may use ONLY those
(allowed_tools), with filesystem and shell explicitly denied.

The SDK's query() is an async generator, but AgentBackend.run() is a
plain sync generator (that's the contract every backend implements).
To bridge them WITHOUT buffering the whole turn, the async work runs on
a background thread with its own event loop, and every event is pushed
onto a thread-safe queue the instant it happens; run() just blocks on
that queue and yields as items arrive. This is what makes tool calls
and text appear live instead of all at once when the turn ends.
"""

import asyncio
import json
import queue
import shutil
import threading
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

MAX_TURNS = 15
_DONE = object()  # sentinel: the worker thread has nothing left to push


class ClaudeCodeBackend(AgentBackend):
    name = "claude_code"

    def availability(self) -> tuple[bool, str]:
        if shutil.which("claude"):
            return True, ""
        return False, "the `claude` CLI is not on PATH"

    def run(
        self, *, system_prompt: str, history: list[dict], user_message: str, toolset: BoundToolset
    ) -> Iterator[TurnEvent]:
        q: queue.Queue = queue.Queue()

        def worker():
            try:
                result = asyncio.run(self._run_async(system_prompt, history, user_message, toolset, q))
            except Exception as e:  # SDK spawn/transport failures
                q.put(TurnFailed(error=f"Claude Code backend: {type(e).__name__}: {e}"))
            else:
                q.put(result)
            finally:
                q.put(_DONE)

        threading.Thread(target=worker, daemon=True).start()
        while True:
            item = q.get()
            if item is _DONE:
                return
            yield item

    async def _run_async(
        self,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        toolset: BoundToolset,
        q: "queue.Queue[TurnEvent]",
    ) -> TurnEvent:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            create_sdk_mcp_server,
            query,
            tool,
        )

        def make_handler(spec):
            @tool(spec.name, spec.description, spec.input_schema)
            async def handler(args):
                payload = dict(args or {})
                q.put(ToolCallStarted(tool=spec.name, payload=payload))
                try:
                    # toolset.execute hits the Django ORM (audit writes): it must
                    # run off the event loop thread or Django raises
                    # SynchronousOnlyOperation
                    output = await asyncio.to_thread(toolset.execute, spec.name, payload)
                    q.put(ToolCallFinished(tool=spec.name, payload=payload, output=output))
                except ToolDenied as e:
                    output = {"error": f"Denied by policy: {e}"}
                    q.put(ToolCallDenied(tool=spec.name, payload=payload, reason=str(e)))
                return {"content": [{"type": "text", "text": json.dumps(output, default=str)}]}

            return handler

        specs = toolset.allowed_specs()
        server = create_sdk_mcp_server(name="db", version="1.0.0", tools=[make_handler(s) for s in specs])

        # each turn is a fresh SDK session: prior conversation rides in the prompt
        parts = [f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in history[-20:]]
        parts.append(f"User: {user_message}")

        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            mcp_servers={"db": server},
            allowed_tools=[f"mcp__db__{s.name}" for s in specs],
            # Task is denied too: subagents don't inherit the in-process MCP
            # tools, so delegating to them just loops on inaccessible reads
            disallowed_tools=[
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "WebFetch",
                "WebSearch",
                "Task",
            ],
            max_turns=MAX_TURNS,
            setting_sources=[],
        )

        texts: list[str] = []
        async for message in query(prompt="\n\n".join(parts), options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text.strip():
                        texts.append(block.text)
                        q.put(TextDelta(text=block.text))
            elif isinstance(message, ResultMessage) and message.is_error:
                return TurnFailed(error=f"Claude Code: {message.result or 'unknown error'}")
        return TurnCompleted(reply="\n\n".join(texts) or "(no reply)")

"""Context block builder: how project files reach the agent's prompt.

Hybrid strategy (Claude Code-style):
- small files (<= INLINE_MAX_BYTES) are inlined in full
- large files appear as an index entry only, readable on demand via the
  read_context_file tool
- a total inline budget keeps the per-turn cost bounded: past it, even
  small files degrade to index entries

The output is DETERMINISTIC (files sorted by name, no timestamps): the
system prompt must be byte-identical across turns so provider prompt
caching can do its job. Do not add anything volatile here.
"""

from .models import Project

INLINE_MAX_BYTES = 4 * 1024
TOTAL_INLINE_BUDGET = 16 * 1024


def build_context_block(project: Project) -> str:
    files = list(project.context_files.order_by("name"))
    if not files:
        return ""

    inlined: list[str] = []
    indexed: list[str] = []
    budget = TOTAL_INLINE_BUDGET

    for f in files:
        size = f.size
        if size <= INLINE_MAX_BYTES and size <= budget:
            budget -= size
            inlined.append(f"## {f.name}\n{f.content.strip()}")
        else:
            first_line = f.content.strip().splitlines()[0][:120] if f.content.strip() else ""
            indexed.append(f"- {f.name} ({size} bytes) — {first_line}")

    parts = ["# Project context"]
    if inlined:
        parts.extend(inlined)
    if indexed:
        parts.append(
            "## Other context files (read on demand)\n"
            "Use the read_context_file tool to read any of these when relevant:\n" + "\n".join(indexed)
        )
    return "\n\n".join(parts)

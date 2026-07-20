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
            indexed.append(_index_entry(f, size))

    parts = ["# Project context"]
    if inlined:
        parts.extend(inlined)
    if indexed:
        parts.append(
            "## Other context files (search, then read)\n"
            "Locate what you need with search_context_files, then read the relevant range with "
            "read_context_file (offset/limit) — do not read whole large files:\n" + "\n".join(indexed)
        )
    return "\n\n".join(parts)


MAX_OUTLINE_HEADINGS = 8


def _index_entry(f, size: int) -> str:
    """A file's index line: name, size, line count, and a heading outline —
    a table of contents the agent can target reads with, instead of a
    single blind first-line preview."""
    lines = f.content.splitlines()
    headings = []
    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith("#"):
            headings.append(f"{line.strip()[:80]} (line {i})")
            if len(headings) >= MAX_OUTLINE_HEADINGS:
                headings.append("…")
                break
    entry = f"- {f.name} ({size} bytes, {len(lines)} lines)"
    if headings:
        entry += "\n  " + "\n  ".join(headings)
    elif lines:
        entry += f" — {lines[0].strip()[:120]}"
    return entry

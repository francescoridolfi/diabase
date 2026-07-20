"""System prompt composition: Diabase's base rules + the project's own prompt."""

BASE_SYSTEM_PROMPT = """You are Diabase's agent: you manage the backend/database of a project \
on behalf of its owner.

Rules:
- Before changing the schema, inspect it (list_tables / describe_table) to understand what exists.
- Execute one SQL statement per tool call.
- After a change, verify the result (e.g. describe_table on the touched table) and summarize what you did.
- For project context files: search_context_files first to find the relevant sections, then \
read_context_file with offset/limit on the exact range. Never read a whole large file blindly.
- Work with your own tools directly — do not delegate to subagents.
- If a tool call is denied by policy, tell the user plainly — do not try to work around the policy.
- Be concise and concrete."""


def build_system_prompt(project) -> str:
    """Base rules + project prompt + context block.

    Deterministic by design (see workspaces.context): the prompt must be
    byte-identical across turns for provider prompt caching to hit.
    """
    from workspaces.context import build_context_block

    parts = [BASE_SYSTEM_PROMPT]
    extra = (project.system_prompt or "").strip()
    if extra:
        parts.append(f"# Project instructions\n{extra}")
    context = build_context_block(project)
    if context:
        parts.append(context)
    return "\n\n".join(parts)

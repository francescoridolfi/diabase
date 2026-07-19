"""Workspace mutations that must leave an audit trace.

Views (and future API endpoints) go through these helpers instead of
touching the models directly, so every human change to prompt and
context files is recorded.
"""

from audit.services import record

from .models import ContextFile, Project


class ContextFileTooLarge(ValueError):
    pass


def set_system_prompt(project: Project, text: str, *, user: str = "") -> Project:
    project.system_prompt = text
    project.save(update_fields=["system_prompt"])
    record(
        action="project.prompt_updated",
        actor_type="user",
        actor=user,
        project=project,
        payload_in={"system_prompt": text},
    )
    return project


def save_context_file(project: Project, name: str, content: str, *, user: str = "") -> ContextFile:
    size = len(content.encode())
    if size > ContextFile.MAX_SIZE:
        raise ContextFileTooLarge(
            f"Context file {name!r} is {size} bytes; the limit is {ContextFile.MAX_SIZE}"
        )
    file, created = project.context_files.update_or_create(name=name, defaults={"content": content})
    record(
        action="context_file.added" if created else "context_file.updated",
        actor_type="user",
        actor=user,
        project=project,
        payload_in={"name": name, "content": content},
    )
    return file


def delete_context_file(project: Project, name: str, *, user: str = "") -> None:
    file = project.context_files.get(name=name)
    file.delete()
    record(
        action="context_file.removed",
        actor_type="user",
        actor=user,
        project=project,
        payload_in={"name": name},
    )

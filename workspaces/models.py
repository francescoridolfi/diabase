from django.db import models

from instances.models import Server


class Project(models.Model):
    """A workspace bound to one instance: chat, context, settings, audit."""

    AUTONOMY_LEVELS = [
        ("read_only", "Read-only"),
        ("plan", "Plan & approve"),
        ("full", "Full (audited)"),
    ]

    name = models.CharField(max_length=100)
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="projects")
    system_prompt = models.TextField(
        blank=True,
        help_text="Appended to Diabase's base prompt: project conventions, constraints, tone.",
    )
    # safe-by-default: writes need an approved plan until the user raises this
    autonomy_level = models.CharField(max_length=20, choices=AUTONOMY_LEVELS, default="plan")
    # which configured connection drives this project's agent; null = auto
    # (env AGENT_BACKEND or first available). String ref: agents imports us.
    agent_connection = models.ForeignKey(
        "agents.AgentConnection",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="projects",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class ContextFile(models.Model):
    """A text/markdown file giving the agent project context.

    Small files are inlined into the system prompt; large ones are only
    indexed there and read on demand via the read_context_file tool
    (see workspaces.context). Text only in v1 — binary formats are
    memory-phase territory.
    """

    MAX_SIZE = 100 * 1024  # bytes of content, enforced by services.save_context_file

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="context_files")
    name = models.CharField(max_length=120)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["project", "name"], name="unique_context_file_name_per_project")
        ]

    def __str__(self):
        return f"{self.name} ({self.project})"

    @property
    def size(self) -> int:
        return len(self.content.encode())


class ChatMessage(models.Model):
    """One message of a project's conversation (user or agent)."""

    ROLES = [("user", "User"), ("assistant", "Assistant")]
    KINDS = [("chat", "Chat"), ("plan_result", "Plan result")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=ROLES)
    # plan_result rows are system-generated (apply outcomes fed back to the
    # agent); they ride in history as user-role turns but render differently
    kind = models.CharField(max_length=12, choices=KINDS, default="chat")
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:60]}"

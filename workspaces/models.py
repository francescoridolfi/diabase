from django.db import models

from instances.models import Server


class Project(models.Model):
    """A workspace bound to one instance: chat, context, settings, audit."""

    name = models.CharField(max_length=100)
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="projects")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class ChatMessage(models.Model):
    """One message of a project's conversation (user or agent)."""

    ROLES = [("user", "User"), ("assistant", "Assistant")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=10, choices=ROLES)
    content = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.role}: {self.content[:60]}"

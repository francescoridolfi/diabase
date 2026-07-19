from django.db import models

from instances.models import Server


class Project(models.Model):
    """A workspace bound to one instance: chat, context, settings, audit."""

    name = models.CharField(max_length=100)
    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="projects")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

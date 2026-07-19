from django.db import models

from workspaces.models import Project


class Turn(models.Model):
    """Operational metadata for one agent execution.

    The per-tool detail lives in the audit trail; the Turn records what
    the trail doesn't: which backend/model ran, how long it took, how it
    ended. Fine-grained history is audit's job — Turn rows may be pruned.
    """

    STATUSES = [("running", "Running"), ("completed", "Completed"), ("failed", "Failed")]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="turns")
    backend = models.CharField(max_length=30)
    model = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=10, choices=STATUSES, default="running")
    error = models.TextField(blank=True)
    user_message = models.TextField()
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.backend} turn on {self.project} → {self.status}"

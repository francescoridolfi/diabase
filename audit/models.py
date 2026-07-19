from django.db import models

from workspaces.models import Project


class AppendOnlyQuerySet(models.QuerySet):
    """Blocks every bulk mutation path on the audit trail."""

    def update(self, **kwargs):
        raise TypeError("AuditEntry is append-only: update is not allowed")

    def delete(self):
        raise TypeError("AuditEntry is append-only: delete is not allowed")

    def bulk_update(self, objs, fields, **kwargs):
        raise TypeError("AuditEntry is append-only: bulk_update is not allowed")


class AuditEntry(models.Model):
    """One immutable row per action, human or AI.

    Append-only is enforced at the application level: no code path may
    update or delete an entry (DB-level enforcement arrives with the
    dedicated Postgres in the compose setup).

    Payloads are stored in full — including query results from managed
    instances. GDPR retention/erasure policy is tracked in issue #1 and
    is a release blocker.
    """

    ACTOR_TYPES = [("user", "User"), ("agent", "Agent"), ("system", "System")]
    OUTCOMES = [("success", "Success"), ("error", "Error")]

    # SET_NULL + denormalized names: audit rows outlive the objects they describe.
    project = models.ForeignKey(
        Project, null=True, blank=True, on_delete=models.SET_NULL, related_name="audit_entries"
    )
    project_name = models.CharField(max_length=100, blank=True)
    server_name = models.CharField(max_length=100, blank=True)
    adapter_type = models.CharField(max_length=20, blank=True)

    actor_type = models.CharField(max_length=10, choices=ACTOR_TYPES)
    actor = models.CharField(max_length=200, blank=True, help_text="Username, or agent model identifier")

    action = models.CharField(
        max_length=100, help_text="Tool name (execute_sql) or event slug (project.created)"
    )
    payload_in = models.JSONField(default=dict, blank=True)
    payload_out = models.JSONField(default=dict, blank=True)
    outcome = models.CharField(max_length=10, choices=OUTCOMES)
    error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "audit entries"

    def __str__(self):
        return (
            f"[{self.created_at:%Y-%m-%d %H:%M:%S}] "
            f"{self.actor_type}:{self.actor} {self.action} → {self.outcome}"
        )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise TypeError("AuditEntry is append-only: existing rows cannot be modified")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise TypeError("AuditEntry is append-only: rows cannot be deleted")

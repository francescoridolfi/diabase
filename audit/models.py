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

    def redact_payloads(self, *, reason: str) -> int:
        """The ONE sanctioned mutation on existing rows (GDPR, issue #1):
        replaces the payloads with a tombstone and stamps redacted_at.
        WHO did WHAT, WHEN, with what outcome stays forever — only the
        data content dies. Callers go through audit.services (which
        records the redaction itself in the trail); already-redacted
        rows are skipped so the operation is idempotent."""
        from django.utils import timezone

        tombstone = {"redacted": True, "reason": reason}
        return super(AppendOnlyQuerySet, self.filter(redacted_at__isnull=True)).update(
            payload_in=tombstone,
            payload_out=tombstone,
            error="",
            redacted_at=timezone.now(),
        )


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
    # set when the payloads were tombstoned (retention or erasure);
    # the row itself never goes away
    redacted_at = models.DateTimeField(null=True, blank=True)

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


class RetentionPolicy(models.Model):
    """Site-wide audit retention (singleton, pk=1).

    0 = keep payloads forever: whoever self-hosts Diabase is the data
    controller, so nothing is thrown away unless they turn the window
    on. A positive value redacts payloads older than that many days on
    every purge run (Settings button or the purge_audit_payloads cron
    command); the rows and their who/what/when survive redaction."""

    audit_payload_days = models.PositiveIntegerField(
        default=0, help_text="Days to keep audit payloads; 0 keeps them forever"
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "retention policy"

    def __str__(self):
        days = self.audit_payload_days
        return f"audit payloads: {f'{days}d' if days else 'forever'}"

    @classmethod
    def load(cls) -> "RetentionPolicy":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

"""The memory index listens; the other apps never know it exists.

Signals keep the dependency direction intact (everything may call into
audit, audit calls into nothing): audit/chat/context writes land in the
index as a side effect, and the GDPR redaction broadcast purges the
chunks derived from tombstoned payloads.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from audit.models import AuditEntry
from audit.services import payloads_redacted
from workspaces.models import ChatMessage, ContextFile

from . import services


@receiver(post_save, sender=AuditEntry, dispatch_uid="memory.index_audit")
def _on_audit_entry(sender, instance, created, **kwargs):
    if created:
        services.index_audit_entry(instance)


@receiver(post_save, sender=ChatMessage, dispatch_uid="memory.index_chat")
def _on_chat_message(sender, instance, **kwargs):
    services.index_chat_message(instance)


@receiver(post_delete, sender=ChatMessage, dispatch_uid="memory.drop_chat")
def _on_chat_message_deleted(sender, instance, **kwargs):
    if instance.project_id:
        services.drop_chunks(instance.project, "chat", ref=f"chat:{instance.conversation_id}:{instance.pk}")


@receiver(post_save, sender=ContextFile, dispatch_uid="memory.index_context")
def _on_context_file(sender, instance, **kwargs):
    services.index_context_file(instance)


@receiver(post_delete, sender=ContextFile, dispatch_uid="memory.drop_context")
def _on_context_file_deleted(sender, instance, **kwargs):
    if instance.project_id:
        services.drop_context_file(instance.project, instance.name)


@receiver(payloads_redacted, dispatch_uid="memory.purge_redacted")
def _on_payloads_redacted(sender, pks, **kwargs):
    services.purge_redacted_audit(pks)

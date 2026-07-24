from django.db import models

from workspaces.models import Project


class MemoryChunk(models.Model):
    """One retrievable unit of project memory.

    The index is DERIVED, never a source of truth: every chunk points
    back at its origin (`source_ref`) and can be rebuilt from it at any
    time (reindex_memory). Four sources feed it: schema table cards,
    audited write actions, chat history, context files.

    `embedding` stays empty until an embedding connection is configured
    (hybrid retrieval, phase 4 part 2): lexical search works without it.
    """

    SOURCES = [
        ("schema", "Schema"),
        ("audit", "Audit trail"),
        ("chat", "Chat"),
        ("context", "Context file"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="memory_chunks")
    source_type = models.CharField(max_length=10, choices=SOURCES)
    # live pointer back to the origin: "table:orders", "audit:123",
    # "chat:4:56", "file:notes.md#2"
    source_ref = models.CharField(max_length=200)
    title = models.CharField(max_length=200, blank=True)
    text = models.TextField()
    content_hash = models.CharField(max_length=64)
    embedding = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["project", "source_type", "source_ref"], name="unique_chunk_per_source"
            )
        ]
        indexes = [models.Index(fields=["project", "source_type"])]

    def __str__(self):
        return f"[{self.source_type}] {self.source_ref} ({self.project})"

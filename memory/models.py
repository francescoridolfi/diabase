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


class GraphSettings(models.Model):
    """Site-wide temporal knowledge graph configuration (singleton, pk=1).

    The graph is an optional enhancement layered on the lexical index:
    it is active only when `enabled` is on, graphiti-core is installed,
    Neo4j is reachable AND both connections are set. Anything missing
    degrades to exactly the pre-graph behavior.

    Two separate connections because entity extraction and embeddings
    are different jobs: extraction accepts any chat-capable connection
    (Anthropic included), the embedder must be an OpenAI-compatible
    endpoint (Anthropic has no embeddings API).
    """

    enabled = models.BooleanField(default=False)
    # env fallbacks let compose configure this without touching the GUI
    neo4j_uri = models.CharField(max_length=300, blank=True)
    neo4j_user = models.CharField(max_length=100, blank=True)
    neo4j_password_encrypted = models.TextField(blank=True, editable=False)
    llm_connection = models.ForeignKey(
        "agents.AgentConnection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    embedder_connection = models.ForeignKey(
        "agents.AgentConnection", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    embedding_model = models.CharField(max_length=100, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Graph settings (enabled={self.enabled})"

    @property
    def neo4j_password(self) -> str:
        from agents import crypto

        return crypto.decrypt(self.neo4j_password_encrypted)

    @neo4j_password.setter
    def neo4j_password(self, value: str):
        from agents import crypto

        self.neo4j_password_encrypted = crypto.encrypt(value or "")

    @classmethod
    def load(cls) -> "GraphSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class GraphEpisode(models.Model):
    """Bridge row: which Graphiti episode a source record produced.

    Exists for GDPR parity with the lexical index: when audit payloads
    are tombstoned (or a chat message is deleted), the episodes derived
    from them are looked up here and removed from the graph.
    """

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="graph_episodes")
    # same shape as MemoryChunk.source_ref: "audit:123", "exchange:4:56"
    source_ref = models.CharField(max_length=200)
    episode_uuid = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["project", "source_ref"], name="unique_episode_per_source")
        ]

    def __str__(self):
        return f"{self.source_ref} -> {self.episode_uuid}"

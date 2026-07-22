from django.db import models


class Server(models.Model):
    """A connected Supabase deployment (self-hosted or cloud)."""

    ADAPTER_CHOICES = [
        ("sqlite", "SQLite (local file)"),
        ("postgres", "PostgreSQL / Supabase self-hosted"),
        ("supabase", "Supabase Cloud (Management API)"),
    ]

    name = models.CharField(max_length=100)
    adapter_type = models.CharField(max_length=20, choices=ADAPTER_CHOICES, default="sqlite")
    dsn = models.CharField(
        max_length=500,
        help_text=(
            "SQLite: path to the .db file — Postgres: postgresql://user:pass@host:5432/db — "
            "Supabase Cloud: project ref (token in SUPABASE_ACCESS_TOKEN)"
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.adapter_type})"


class EdgeFunctionSource(models.Model):
    """The source of truth for a function's code, kept on OUR side.

    Deploys go through the Management API's bundle endpoint, whose
    artifact (eszip) is not meant to be read back — so every deploy that
    goes through Diabase (an agent plan or the user's editor) saves the
    exact source here, and every read (viewer, agent's read_function,
    plan diffs) is served locally. `deployed_version` is the guardrail:
    when the live version drifts from it, something deployed outside
    Diabase and the UI says so instead of pretending.
    """

    server = models.ForeignKey(Server, on_delete=models.CASCADE, related_name="function_sources")
    slug = models.CharField(max_length=80)
    name = models.CharField(max_length=120, blank=True)
    body = models.TextField()
    verify_jwt = models.BooleanField(default=True)
    deployed_version = models.IntegerField(null=True, blank=True)
    deployed_by = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]
        constraints = [
            models.UniqueConstraint(fields=["server", "slug"], name="unique_function_source_per_server")
        ]

    def __str__(self):
        return f"{self.slug}@{self.server} (v{self.deployed_version})"

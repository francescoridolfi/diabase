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

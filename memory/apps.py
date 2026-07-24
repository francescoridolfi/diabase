from django.apps import AppConfig


class MemoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "memory"

    def ready(self):
        from . import signals  # noqa: F401 — connects the index listeners

from django.contrib import admin

from .models import AuditEntry


@admin.register(AuditEntry)
class AuditEntryAdmin(admin.ModelAdmin):
    """Read-only surface: the trail is inspectable, never editable."""

    list_display = ("created_at", "actor_type", "actor", "action", "project_name", "outcome")
    list_filter = ("actor_type", "outcome", "action")
    search_fields = ("actor", "action", "project_name", "error")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

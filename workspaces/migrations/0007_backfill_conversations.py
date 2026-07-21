"""Fold each project's pre-existing history into one default conversation.

Before multi-chat, messages and turns hung directly off the project; every
project that has any gets a single Conversation titled from its first user
message, so nothing changes for the user except that the thread now has a
name in the sidebar.
"""

from django.db import migrations


def backfill(apps, schema_editor):
    Project = apps.get_model("workspaces", "Project")
    Conversation = apps.get_model("workspaces", "Conversation")
    ChatMessage = apps.get_model("workspaces", "ChatMessage")
    Turn = apps.get_model("agents", "Turn")

    for project in Project.objects.all():
        has_history = (
            ChatMessage.objects.filter(project=project).exists()
            or Turn.objects.filter(project=project).exists()
        )
        if not has_history:
            continue
        first = (
            ChatMessage.objects.filter(project=project, role="user").order_by("created_at").first()
        )
        title = (first.content.splitlines()[0][:80] if first and first.content else "") or "Chat"
        conversation = Conversation.objects.create(project=project, title=title)
        ChatMessage.objects.filter(project=project).update(conversation=conversation)
        Turn.objects.filter(project=project).update(conversation=conversation)


def noop(apps, schema_editor):
    pass  # reverse: the FKs just go back to null when the fields are removed


class Migration(migrations.Migration):
    dependencies = [
        ("workspaces", "0006_conversation_chatmessage_conversation"),
        ("agents", "0005_turn_conversation"),
    ]

    operations = [migrations.RunPython(backfill, noop)]

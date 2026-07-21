"""Messages always belong to a conversation from here on (0007 backfilled
every existing row, so there are no NULLs left to worry about)."""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("workspaces", "0007_backfill_conversations")]

    operations = [
        migrations.AlterField(
            model_name="chatmessage",
            name="conversation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="messages",
                to="workspaces.conversation",
            ),
        ),
    ]

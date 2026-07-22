"""Bootstrap credential for a fresh instance: admin / admin.

Runs on the very first `migrate` (Docker entrypoint included). Only when
NO user exists at all — an instance with real users is never touched.
The login flow forces this factory password to be changed before anything
else can be done (see web.auth).
"""

from django.db import migrations


def seed_admin(apps, schema_editor):
    from django.contrib.auth.hashers import make_password

    User = apps.get_model("auth", "User")
    if User.objects.exists():
        return
    User.objects.create(
        username="admin",
        password=make_password("admin"),
        is_staff=True,
        is_superuser=True,
    )


def unseed(apps, schema_editor):
    pass  # never delete users on rollback


class Migration(migrations.Migration):
    dependencies = [("auth", "0012_alter_user_first_name_max_length")]

    operations = [migrations.RunPython(seed_admin, unseed)]

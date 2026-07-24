"""Rebuild the memory index from its sources of truth.

Normally the index maintains itself (signal-driven, hash-skipped); this
covers backfilling a pre-memory project, recovering from a wiped index,
and cron-refreshing schema cards for instances edited outside Diabase.
"""

from django.core.management.base import BaseCommand, CommandError

from memory.services import reindex_project
from workspaces.models import Project


class Command(BaseCommand):
    help = "Rebuild the memory index (all projects, or --project ID)"

    def add_arguments(self, parser):
        parser.add_argument("--project", type=int, default=None, metavar="ID")

    def handle(self, *, project, **options):
        projects = Project.objects.select_related("server")
        if project is not None:
            projects = projects.filter(pk=project)
            if not projects.exists():
                raise CommandError(f"No project with id {project}")
        for p in projects:
            out = reindex_project(p)
            line = f"{p.name}: {out['total']} chunks (changed: {out['changed']})"
            if out.get("errors"):
                line += f" — errors: {out['errors']}"
            self.stdout.write(line)

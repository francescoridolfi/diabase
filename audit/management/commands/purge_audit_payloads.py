"""Cron entry point for the GDPR retention/erasure policy (issue #1).

Retention (default): redacts audit payloads older than the configured
window — schedule it daily. Erasure: --erase-contains handles a
right-to-erasure request from the shell; the GUI covers the common
cases, this covers scripting.
"""

from django.core.management.base import BaseCommand, CommandError

from audit.services import apply_retention, erase_payloads
from workspaces.models import Project


class Command(BaseCommand):
    help = "Redact audit payloads per the retention policy (or a targeted erasure request)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=None, help="Override the stored retention window (0 = no-op)"
        )
        parser.add_argument(
            "--erase-contains",
            default="",
            metavar="TEXT",
            help="Erasure mode: redact entries whose payload contains TEXT (case-insensitive)",
        )
        parser.add_argument(
            "--project", type=int, default=None, metavar="ID", help="Limit erasure to one project id"
        )

    def handle(self, *, days, erase_contains, project, **options):
        if erase_contains or project is not None:
            target = Project.objects.filter(pk=project).first() if project is not None else None
            if project is not None and target is None:
                raise CommandError(f"No project with id {project}")
            count = erase_payloads(needle=erase_contains, project=target, actor="cli")
            self.stdout.write(self.style.SUCCESS(f"Erasure: redacted {count} payload(s)"))
            return
        count = apply_retention(actor="cli", days=days)
        if count:
            self.stdout.write(self.style.SUCCESS(f"Retention: redacted {count} payload(s)"))
        else:
            self.stdout.write("Retention: nothing to redact (window off or no expired entries)")

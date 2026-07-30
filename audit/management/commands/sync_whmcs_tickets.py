"""Sync tickets from the WHMCS REST API into local TicketSnapshot/TicketReply
records and recompute their SLA status, for every active API-sourced Project.
Safe to run repeatedly/on an interval.

Usage: python manage.py sync_whmcs_tickets
"""

from django.core.management.base import BaseCommand

from audit.models import Project
from audit.sync import sync_tickets


class Command(BaseCommand):
    help = "Sync tickets from WHMCS and evaluate their SLA status, for every API-sourced project."

    def handle(self, *args, **options):
        projects = Project.objects.filter(source_type=Project.SOURCE_API, active=True)
        if not projects:
            self.stdout.write("No active API-sourced projects configured.")
            return
        for project in projects:
            synced, errored = sync_tickets(project)
            if errored:
                self.stdout.write(
                    self.style.WARNING(
                        f"[{project.name}] Synced {synced} ticket(s), {errored} error(s) - see logs."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(f"[{project.name}] Synced {synced} ticket(s).")
                )

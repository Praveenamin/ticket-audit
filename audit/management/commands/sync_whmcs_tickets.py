"""Sync tickets from the WHMCS REST API into local TicketSnapshot/TicketReply
records and recompute their SLA status. Safe to run repeatedly/on an interval.

Usage: python manage.py sync_whmcs_tickets
"""

from django.core.management.base import BaseCommand

from audit.sync import sync_tickets


class Command(BaseCommand):
    help = "Sync tickets from WHMCS and evaluate their SLA status."

    def handle(self, *args, **options):
        synced, errored = sync_tickets()
        if errored:
            self.stdout.write(
                self.style.WARNING(f"Synced {synced} ticket(s), {errored} error(s) - see logs.")
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Synced {synced} ticket(s)."))

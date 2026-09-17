"""One-time backfill: re-fetch GetTicket for every API-sourced ticket whose
last_reply_at falls in the ratings report's lookback window, so tickets synced before
the `rating` field existed (and not touched since) pick it up. Safe to run more than
once. Not scheduled -- run manually, once, after deploying.

Usage: python manage.py backfill_reply_ratings [--days=35] [--project=<name>]
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from audit.models import Project, TicketSnapshot
from audit.sync import refresh_ticket_replies
from audit.whmcs_client import WHMCSAPIError


class Command(BaseCommand):
    help = "Backfill TicketReply.rating for already-synced tickets by re-fetching GetTicket."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days", type=int, default=35,
            help="Refresh tickets whose last_reply_at is within this many days (default 35 -- "
            "a small safety margin over the ratings report's 30-day default window).",
        )
        parser.add_argument("--project", type=str, default=None, help="Limit to one project by name.")

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=options["days"])
        projects = Project.objects.filter(source_type=Project.SOURCE_API, active=True)
        if options["project"]:
            projects = projects.filter(name=options["project"])

        for project in projects:
            tickets = TicketSnapshot.objects.filter(project=project, last_reply_at__gte=cutoff)
            total = tickets.count()
            updated, errored = 0, 0
            for ticket in tickets.iterator(chunk_size=200):
                try:
                    if refresh_ticket_replies(project, ticket):
                        updated += 1
                    else:
                        errored += 1
                except WHMCSAPIError as exc:
                    errored += 1
                    self.stderr.write(self.style.WARNING(f"Ticket {ticket.id}: {exc}"))

            self.stdout.write(self.style.SUCCESS(
                f"[{project.name}] Backfilled {updated}/{total}, {errored} error(s)."
            ))

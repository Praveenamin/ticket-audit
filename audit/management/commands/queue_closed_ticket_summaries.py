"""One-off catch-up queueing for ClosedTicketSummary, outside the scheduler's
own automatic tick (which already does this every 30s using the
CLOSED_TICKET_SUMMARY_QUEUE_WINDOW_DAYS setting -- default 3 days). Useful
when the report's own display window (e.g. 7/30 days) is wider than the
standing auto-queue window and you want to catch a specific batch up now,
without changing the ongoing default.

Usage: python manage.py queue_closed_ticket_summaries [--window-days=N]
"""

from django.core.management.base import BaseCommand

from audit.closed_ticket_summary import queue_recent_closed_tickets


class Command(BaseCommand):
    help = "Queue ClosedTicketSummary rows for Closed tickets within a window (one-off catch-up)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--window-days", type=int, default=None,
            help="Days back to look for Closed tickets with no summary yet. "
            "Omit to use the CLOSED_TICKET_SUMMARY_QUEUE_WINDOW_DAYS setting.",
        )

    def handle(self, *args, **options):
        created = queue_recent_closed_tickets(window_days=options["window_days"])
        self.stdout.write(self.style.SUCCESS(f"Queued {created} closed-ticket summary/summaries."))

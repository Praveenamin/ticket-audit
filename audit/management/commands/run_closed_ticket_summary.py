"""Run one queued closed-ticket summary: build a prompt from the reply
thread, call Ollama, validate the structured JSON response, and store the
result for a human to review.

Usage: python manage.py run_closed_ticket_summary --summary-id=<id>
"""

from django.core.management.base import BaseCommand, CommandError

from audit.closed_ticket_summary import run_closed_ticket_summary


class Command(BaseCommand):
    help = "Run one queued ClosedTicketSummary."

    def add_arguments(self, parser):
        parser.add_argument("--summary-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            run_closed_ticket_summary(options["summary_id"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Processed summary {options['summary_id']}."))

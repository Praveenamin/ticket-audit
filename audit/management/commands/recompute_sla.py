"""Re-run apply_sla_evaluation() against already-stored tickets/replies, no
re-sync/re-import needed -- for when the breach engine's logic changes (or
an SLAPolicy's targets are edited) and existing tickets' stored SLA fields
need to catch up.

--open-only additionally powers a scheduled job (audit_scheduler.py): a quiet,
still-open, unanswered ticket needs its SLA status re-evaluated purely because
wall-clock time passed, even though WHMCS itself reports no change at all -- this
needs no fresh WHMCS data, only current time vs. already-stored reply timestamps,
so it can run often and entirely locally. Closed tickets are excluded for that use
case since their SLA status is already frozen at closed_at (_overall_status's own
documented behavior) -- recomputing them on a timer would be pure waste.

Usage: python manage.py recompute_sla [--project=<name>] [--open-only]
"""

from django.core.management.base import BaseCommand

from audit.models import Project, TicketSnapshot
from audit.sla import apply_sla_evaluation


class Command(BaseCommand):
    help = "Recompute stored SLA fields for existing tickets without a full re-sync/re-import."

    def add_arguments(self, parser):
        parser.add_argument("--project", type=str, default=None, help="Limit to one project by name.")
        parser.add_argument(
            "--open-only", action="store_true",
            help="Skip Closed tickets -- their SLA status is already frozen at closed_at.",
        )

    def handle(self, *args, **options):
        qs = TicketSnapshot.objects.all().prefetch_related("ticket_replies")
        if options["project"]:
            qs = qs.filter(project__name=options["project"])
        if options["open_only"]:
            qs = qs.exclude(status="Closed")

        total = qs.count()
        for i, ticket in enumerate(qs.iterator(chunk_size=500), start=1):
            try:
                apply_sla_evaluation(ticket)
            except Exception as exc:
                self.stderr.write(self.style.WARNING(f"Ticket {ticket.id}: {exc}"))
            if i % 2000 == 0:
                self.stdout.write(f"{i}/{total}...")

        self.stdout.write(self.style.SUCCESS(f"Recomputed SLA for {total} ticket(s)."))

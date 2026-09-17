"""Run one queued LLM ticket audit: build a prompt from the reply thread,
call Ollama, validate the structured JSON response, and store the result.

Usage: python manage.py run_ticket_audit --audit-id=<id>
"""

from django.core.management.base import BaseCommand, CommandError

from audit.llm_audit import run_ticket_audit


class Command(BaseCommand):
    help = "Run one queued TicketAudit."

    def add_arguments(self, parser):
        parser.add_argument("--audit-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            run_ticket_audit(options["audit_id"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Processed audit {options['audit_id']}."))

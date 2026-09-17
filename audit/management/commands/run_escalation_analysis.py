"""Run one queued ticket escalation analysis: build a prompt from the reply thread,
call Ollama, validate the structured JSON response, and store the result.

Usage: python manage.py run_escalation_analysis --analysis-id=<id>
"""

from django.core.management.base import BaseCommand, CommandError

from audit.escalation_analysis import run_escalation_analysis


class Command(BaseCommand):
    help = "Run one queued TicketEscalationAnalysis."

    def add_arguments(self, parser):
        parser.add_argument("--analysis-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            run_escalation_analysis(options["analysis_id"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Processed escalation analysis {options['analysis_id']}."))

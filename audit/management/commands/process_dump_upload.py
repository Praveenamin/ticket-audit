"""Process one queued WHMCS dump upload: extract tables, stage in MySQL,
transform into that upload's project's TicketSnapshot/TicketReply/Department
rows, and evaluate SLA status.

Usage: python manage.py process_dump_upload --upload-id=<id>
"""

from django.core.management.base import BaseCommand, CommandError

from audit.dump_import import process_dump_upload


class Command(BaseCommand):
    help = "Process one queued DumpUpload."

    def add_arguments(self, parser):
        parser.add_argument("--upload-id", type=int, required=True)

    def handle(self, *args, **options):
        try:
            process_dump_upload(options["upload_id"])
        except Exception as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f"Processed upload {options['upload_id']}."))

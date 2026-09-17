"""Retry archiving any TicketReply whose S3 upload previously failed (or was never
attempted). Runs automatically via the scheduler's own guard-block (see
audit_scheduler.py, s3_archive_retry_interval) -- this command is the thin
delegate to audit.s3_archive.retry_unarchived_replies, same shape as every other
management command in this app.

Usage: python manage.py retry_s3_archive [--limit=N]
"""

from django.core.management.base import BaseCommand

from audit.s3_archive import retry_unarchived_replies


class Command(BaseCommand):
    help = "Retry archiving any TicketReply whose S3 upload previously failed."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=50,
            help="Maximum number of not-yet-archived replies to attempt this run.",
        )

    def handle(self, *args, **options):
        count = retry_unarchived_replies(limit=options["limit"])
        self.stdout.write(self.style.SUCCESS(f"Archived {count} reply/replies on retry."))

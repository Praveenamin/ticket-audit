import time
import signal
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ticket_audit.settings")
django.setup()

from django.core.management import call_command
from django.utils import timezone

from audit.closed_ticket_summary import queue_recent_closed_tickets
from audit.models import ClosedTicketSummary, DumpUpload, TicketAudit

running = True


def signal_handler(signum, frame):
    global running
    print("\nReceived shutdown signal. Stopping scheduler...")
    running = False


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

print("Starting WHMCS ticket-audit scheduler...")
interval = 30  # tick length (seconds)
sync_interval = 120  # sync tickets from WHMCS every 2 minutes
dump_import_interval = 30  # check for a queued dump upload every 30 seconds
ai_audit_interval = 30  # check for a queued AI ticket audit every 30 seconds
closed_ticket_summary_interval = 30  # check for newly-closed tickets / a queued summary every 30 seconds
s3_archive_retry_interval = 120  # retry any failed S3 archive uploads every 2 minutes

last_sync = timezone.now()
last_dump_import_check = timezone.now()
last_ai_audit_check = timezone.now()
last_closed_ticket_summary_check = timezone.now()
last_s3_archive_retry_check = timezone.now()

while running:
    try:
        time_since_last_sync = (timezone.now() - last_sync).total_seconds()
        if time_since_last_sync >= sync_interval:
            try:
                print(f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] Syncing WHMCS tickets...")
                call_command("sync_whmcs_tickets", verbosity=1)
                last_sync = timezone.now()
                print("WHMCS ticket sync completed.")
            except Exception as e:
                print(f"Error in WHMCS ticket sync: {str(e)}")

        # One dump at a time, oldest first -- a large dump's processing time
        # simply delays the next tick, same accepted tradeoff as every other
        # job in this loop.
        time_since_last_dump_check = (timezone.now() - last_dump_import_check).total_seconds()
        if time_since_last_dump_check >= dump_import_interval:
            try:
                queued = DumpUpload.objects.filter(
                    status=DumpUpload.STATUS_QUEUED
                ).order_by("uploaded_at").first()
                if queued:
                    print(
                        f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"Processing dump upload {queued.id} ({queued.project.name})..."
                    )
                    call_command("process_dump_upload", upload_id=queued.id)
                    print(f"Dump upload {queued.id} processed.")
                last_dump_import_check = timezone.now()
            except Exception as e:
                print(f"Error processing dump upload: {str(e)}")

        # One AI audit at a time, oldest first -- same accepted tradeoff as
        # every other job in this loop; a slow Ollama call just delays the
        # next tick.
        time_since_last_ai_audit_check = (timezone.now() - last_ai_audit_check).total_seconds()
        if time_since_last_ai_audit_check >= ai_audit_interval:
            try:
                queued = TicketAudit.objects.filter(
                    status=TicketAudit.STATUS_QUEUED
                ).order_by("requested_at").first()
                if queued:
                    print(
                        f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"Running AI audit {queued.id} (ticket {queued.ticket_id})..."
                    )
                    call_command("run_ticket_audit", audit_id=queued.id)
                    print(f"AI audit {queued.id} processed.")
                last_ai_audit_check = timezone.now()
            except Exception as e:
                print(f"Error processing AI audit: {str(e)}")

        # Auto-queue any newly-closed tickets in the recent window that don't
        # have a summary yet, then process one queued summary at a time,
        # oldest first -- same accepted tradeoff as every other job in this
        # loop; a slow Ollama call just delays the next tick.
        time_since_last_closed_ticket_summary_check = (
            timezone.now() - last_closed_ticket_summary_check
        ).total_seconds()
        if time_since_last_closed_ticket_summary_check >= closed_ticket_summary_interval:
            try:
                queue_recent_closed_tickets()
                queued = ClosedTicketSummary.objects.filter(
                    status=ClosedTicketSummary.STATUS_QUEUED
                ).order_by("queued_at").first()
                if queued:
                    print(
                        f"[{timezone.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"Running closed-ticket summary {queued.id} (ticket {queued.ticket_id})..."
                    )
                    call_command("run_closed_ticket_summary", summary_id=queued.id)
                    print(f"Closed-ticket summary {queued.id} processed.")
                last_closed_ticket_summary_check = timezone.now()
            except Exception as e:
                print(f"Error processing closed-ticket summary: {str(e)}")

        # Backstop for a reply whose S3 upload failed on the exact sync pass it
        # arrived and then went permanently dormant (see Addendum 5's "known
        # limitation" note) -- self-limiting query, cheap enough to run indefinitely.
        time_since_last_s3_archive_retry = (timezone.now() - last_s3_archive_retry_check).total_seconds()
        if time_since_last_s3_archive_retry >= s3_archive_retry_interval:
            try:
                call_command("retry_s3_archive")
                last_s3_archive_retry_check = timezone.now()
            except Exception as e:
                print(f"Error retrying S3 archive: {str(e)}")
    except Exception as e:
        print(f"Error: {str(e)}")

    for _ in range(interval):
        if not running:
            break
        time.sleep(1)

print("\nScheduler stopped.")

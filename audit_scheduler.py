import time
import signal
import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ticket_audit.settings")
django.setup()

from django.core.management import call_command
from django.utils import timezone

from audit.models import DumpUpload

running = True


def signal_handler(signum, frame):
    global running
    print("\nReceived shutdown signal. Stopping scheduler...")
    running = False


signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

print("Starting WHMCS ticket-audit scheduler...")
interval = 30  # tick length (seconds)
sync_interval = 300  # sync tickets from WHMCS every 5 minutes
dump_import_interval = 30  # check for a queued dump upload every 30 seconds

last_sync = timezone.now()
last_dump_import_check = timezone.now()

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
    except Exception as e:
        print(f"Error: {str(e)}")

    for _ in range(interval):
        if not running:
            break
        time.sleep(1)

print("\nScheduler stopped.")

"""WHMCS DB dump -> one Project's TicketSnapshot/TicketReply/Department rows.

Mirrors audit/sync.py's shape (same upsert-then-apply_sla_evaluation pattern)
but sources from a raw MySQL dump instead of the WHMCS API. Reuses the exact
extract-relevant-tables + throwaway-MySQL-import approach already proven on
the separate WHMCS-PDF-export side task, formalized here as a permanent
internal `staging_db` service instead of an ad hoc container.
"""

import logging
import os
import re
import subprocess
from datetime import timezone as dt_timezone, timedelta

import pymysql
from django.conf import settings
from django.utils import timezone

from .models import Department, DumpUpload, TicketReply, TicketSnapshot
from .sla import apply_sla_evaluation

logger = logging.getLogger("audit")

WANTED_TABLES = {"tbltickets", "tblticketreplies", "tblticketdepartments"}
_TABLE_RE = re.compile(r"^-- Table structure for table `([^`]+)`")

# Assumption, not yet independently verified for production dumps: same admin
# timezone (IST) as the API-synced Dev project (Assistanz/StackBill is
# India-based) -- flagged to Praveen; correct here if a dump import's SLA
# timestamps ever look off by a fixed few hours.
DUMP_TZ = dt_timezone(timedelta(hours=5, minutes=30))

STAGING_HOST = getattr(settings, "STAGING_DB_HOST", "staging_db")
STAGING_PORT = int(getattr(settings, "STAGING_DB_PORT", 3306))
STAGING_USER = getattr(settings, "STAGING_DB_ROOT_USER", "root")
STAGING_PASSWORD = getattr(settings, "STAGING_DB_ROOT_PASSWORD", "staging")


def derive_author_type(admin_name, contactid):
    """The WHMCS API gives us `requestor_type` (Owner/Contact/Operator)
    directly on each reply; the raw `tblticketreplies` table has no such
    column, so it's derived here. Hypothesis (from handling this exact schema
    on the PDF-export side task, not yet independently verified for SLA
    scoring): staff replies always populate `admin`; client replies leave it
    blank and identify the requester via `contactid` (0 = primary account
    "Owner", non-zero = a named sub-contact "Contact")."""
    if admin_name:
        return TicketReply.OPERATOR
    if contactid:
        return TicketReply.CONTACT
    return TicketReply.OWNER


def _safe_dt(value):
    """pymysql returns DATETIME columns as real datetime objects already (no
    string parsing needed, unlike the API's string timestamps) -- just guard
    against MySQL's zero-date sentinel and attach the assumed source timezone."""
    if value is None:
        return None
    if value.year <= 1:
        return None
    return value.replace(tzinfo=DUMP_TZ).astimezone(dt_timezone.utc)


def _extract_subset(dump_path, subset_path):
    """Pull only the tables we need out of the full dump -- same approach as
    whmcs_ticket_export/extract_tables.py. We don't need tblclients/
    tblcontacts here: unlike the PDF task, the audit app never needs client
    identity, only department/ticket/reply data."""
    capturing = False
    kept = set()
    with open(dump_path, "r", encoding="utf-8", errors="replace") as f_in, open(
        subset_path, "w", encoding="utf-8"
    ) as f_out:
        f_out.write("SET FOREIGN_KEY_CHECKS=0;\nSET NAMES utf8mb4;\nSET sql_mode = '';\n\n")
        for line in f_in:
            m = _TABLE_RE.match(line)
            if m:
                capturing = m.group(1) in WANTED_TABLES
                if capturing:
                    kept.add(m.group(1))
            if capturing:
                f_out.write(line)
    missing = WANTED_TABLES - kept
    if missing:
        raise ValueError(f"Dump is missing expected table(s): {sorted(missing)}")


def _admin_connection():
    return pymysql.connect(
        host=STAGING_HOST, port=STAGING_PORT, user=STAGING_USER, password=STAGING_PASSWORD,
        charset="utf8mb4",
    )


def _staging_connection(db_name):
    return pymysql.connect(
        host=STAGING_HOST, port=STAGING_PORT, user=STAGING_USER, password=STAGING_PASSWORD,
        database=db_name, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def _create_staging_database(db_name):
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
            cur.execute(f"CREATE DATABASE `{db_name}`")
        conn.commit()
    finally:
        conn.close()


def _drop_staging_database(db_name):
    conn = _admin_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
        conn.commit()
    finally:
        conn.close()


def _run_mysql_import(db_name, subset_path):
    with open(subset_path, "rb") as f_in:
        subprocess.run(
            [
                "mysql", "--skip-ssl",
                f"-h{STAGING_HOST}", f"-P{STAGING_PORT}",
                f"-u{STAGING_USER}", f"-p{STAGING_PASSWORD}",
                db_name,
            ],
            stdin=f_in, check=True, stderr=subprocess.PIPE,
        )


def _import_departments(project, conn):
    id_map = {}
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM tblticketdepartments")
        for row in cur.fetchall():
            dept, _ = Department.objects.update_or_create(
                project=project, whmcs_deptid=row["id"],
                defaults={
                    "name": row["name"] or f"Department {row['id']}",
                    "last_seen_at": timezone.now(),
                },
            )
            id_map[row["id"]] = dept
    return id_map


def _import_replies_for_ticket(ticket, conn, ticket_row):
    """Mirrors sync.py's _upsert_replies: the ticket's own opening message
    becomes reply id '0' (matching the API's convention), followed by every
    real tblticketreplies row. NOTE: tblticketreplies.tid is the NUMERIC
    ticket id (joins to tbltickets.id) -- not the friendly ticket number."""
    opened_at = _safe_dt(ticket_row["date"])
    TicketReply.objects.update_or_create(
        ticket=ticket, whmcs_reply_id="0",
        defaults={
            "author_name": ticket_row["name"] or "",
            "author_type": TicketReply.OWNER,
            "admin_name": "",
            "message": ticket_row["message"] or "",
            "posted_at": opened_at or timezone.now(),
        },
    )

    first_response_at = None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, name, email, date, message, admin, contactid "
            "FROM tblticketreplies WHERE tid = %s ORDER BY date ASC",
            (ticket_row["id"],),
        )
        for reply_row in cur.fetchall():
            posted_at = _safe_dt(reply_row["date"])
            if posted_at is None:
                continue
            author_type = derive_author_type(reply_row["admin"], reply_row["contactid"])
            TicketReply.objects.update_or_create(
                ticket=ticket, whmcs_reply_id=str(reply_row["id"]),
                defaults={
                    "author_name": reply_row["name"] or "",
                    "author_type": author_type,
                    "admin_name": reply_row["admin"] or "",
                    "message": reply_row["message"] or "",
                    "posted_at": posted_at,
                },
            )
            if author_type == TicketReply.OPERATOR:
                if first_response_at is None or posted_at < first_response_at:
                    first_response_at = posted_at
    return first_response_at


def _import_tickets(project, conn, dept_map):
    imported = 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, tid, did, date, title, message, name, email, status, urgency, lastreply "
            "FROM tbltickets"
        )
        rows = cur.fetchall()

    for row in rows:
        opened_at = _safe_dt(row["date"])
        if opened_at is None:
            logger.warning(
                "Skipping ticket id=%s for project %s: unparseable/zero open date",
                row["id"], project.name,
            )
            continue

        last_reply_at = _safe_dt(row["lastreply"])
        department = dept_map.get(row["did"])

        ticket, _ = TicketSnapshot.objects.update_or_create(
            project=project, whmcs_ticket_id=row["id"],
            defaults={
                "tid": row["tid"] or "",
                "department": department,
                "subject": row["title"] or "",
                "status": row["status"] or "",
                "priority": row["urgency"] or "",
                "requestor_name": row["name"] or "",
                "requestor_email": row["email"] or "",
                "opened_at": opened_at,
                "last_reply_at": last_reply_at,
                "synced_at": timezone.now(),
            },
        )

        if ticket.status == "Closed" and ticket.closed_at is None:
            ticket.closed_at = last_reply_at or timezone.now()
            ticket.save(update_fields=["closed_at"])

        first_response_at = _import_replies_for_ticket(ticket, conn, row)
        if first_response_at is not None and ticket.first_response_at != first_response_at:
            ticket.first_response_at = first_response_at
            ticket.save(update_fields=["first_response_at"])

        apply_sla_evaluation(ticket)
        imported += 1

    return imported


def process_dump_upload(upload_id):
    """Full pipeline for one DumpUpload: extract -> stage -> transform ->
    evaluate -> clean up. Always drops the staging DB and subset file, whether
    it succeeds or fails; deletes the raw uploaded dump only on success (kept
    on failure so it's available for debugging)."""
    upload = DumpUpload.objects.select_related("project").get(id=upload_id)
    upload.status = DumpUpload.STATUS_PROCESSING
    upload.started_at = timezone.now()
    upload.save(update_fields=["status", "started_at"])

    db_name = f"staging_upload_{upload.id}"
    subset_path = f"/tmp/dump_upload_{upload.id}_subset.sql"
    dump_path = upload.file.path

    try:
        _extract_subset(dump_path, subset_path)
        _create_staging_database(db_name)
        _run_mysql_import(db_name, subset_path)

        conn = _staging_connection(db_name)
        try:
            dept_map = _import_departments(upload.project, conn)
            imported = _import_tickets(upload.project, conn, dept_map)
        finally:
            conn.close()

        upload.status = DumpUpload.STATUS_DONE
        upload.tickets_imported = imported
        upload.finished_at = timezone.now()
        upload.save(update_fields=["status", "tickets_imported", "finished_at"])
        upload.file.delete(save=True)
    except Exception as exc:
        logger.exception("Dump import failed for upload %s", upload_id)
        upload.status = DumpUpload.STATUS_FAILED
        upload.error_message = str(exc)[:2000]
        upload.finished_at = timezone.now()
        upload.save(update_fields=["status", "error_message", "finished_at"])
        raise
    finally:
        _drop_staging_database(db_name)
        if os.path.exists(subset_path):
            os.remove(subset_path)

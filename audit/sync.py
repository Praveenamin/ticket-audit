"""WHMCS -> local ticket sync. Thin management command delegates here (fat
service-module split, matching calculate_sli_compliance.py's convention)."""

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from .models import Department, TicketReply, TicketSnapshot
from .sla import apply_sla_evaluation
from .whmcs_client import WHMCSClient

logger = logging.getLogger("audit")

# WHMCS timestamps are plain 'YYYY-MM-DD HH:MM:SS' strings with no timezone info.
# Confirmed with Praveen (2026-07-28): this WHMCS instance's admin timezone is IST
# (UTC+5:30, no DST) -- a fixed offset is exact, no zoneinfo/tzdata needed.
WHMCS_TZ = dt_timezone(timedelta(hours=5, minutes=30))


def _parse_whmcs_dt(value):
    if not value:
        return None
    naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=WHMCS_TZ).astimezone(dt_timezone.utc)


def _upsert_department(deptid, deptname):
    if not deptid:
        return None
    department, _ = Department.objects.update_or_create(
        whmcs_deptid=deptid,
        defaults={"name": deptname or f"Department {deptid}", "last_seen_at": timezone.now()},
    )
    return department


def _upsert_replies(ticket, replies_payload):
    replies = (replies_payload or {}).get("reply", [])
    if isinstance(replies, dict):
        replies = [replies]

    first_response_at = None
    for reply in replies:
        posted_at = _parse_whmcs_dt(reply.get("date"))
        author_type = reply.get("requestor_type", "")
        TicketReply.objects.update_or_create(
            ticket=ticket,
            whmcs_reply_id=str(reply.get("replyid")),
            defaults={
                "author_name": reply.get("name", ""),
                "author_type": author_type,
                "admin_name": reply.get("admin", ""),
                "message": reply.get("message", ""),
                "posted_at": posted_at,
            },
        )
        if author_type == TicketReply.OPERATOR and posted_at is not None:
            if first_response_at is None or posted_at < first_response_at:
                first_response_at = posted_at
    return first_response_at


def _sync_one_ticket(client, summary):
    whmcs_ticket_id = summary["id"]
    last_reply_at = _parse_whmcs_dt(summary.get("lastreply"))

    existing = TicketSnapshot.objects.filter(whmcs_ticket_id=whmcs_ticket_id).first()
    needs_full_refresh = existing is None or existing.last_reply_at != last_reply_at

    department = _upsert_department(summary.get("deptid"), summary.get("deptname"))

    ticket, _ = TicketSnapshot.objects.update_or_create(
        whmcs_ticket_id=whmcs_ticket_id,
        defaults={
            "tid": summary.get("tid", ""),
            "department": department,
            "subject": summary.get("subject", ""),
            "status": summary.get("status", ""),
            "priority": summary.get("priority", ""),
            "requestor_name": summary.get("requestor_name", ""),
            "requestor_email": summary.get("requestor_email", ""),
            "opened_at": _parse_whmcs_dt(summary.get("date")),
            "last_reply_at": last_reply_at,
            "synced_at": timezone.now(),
        },
    )

    if ticket.status == "Closed" and ticket.closed_at is None:
        ticket.closed_at = last_reply_at or timezone.now()
        ticket.save(update_fields=["closed_at"])

    if needs_full_refresh:
        detail = client.get_ticket(whmcs_ticket_id)
        if detail.get("result") == "success":
            first_response_at = _upsert_replies(ticket, detail.get("replies"))
            if first_response_at is not None and ticket.first_response_at != first_response_at:
                ticket.first_response_at = first_response_at
                ticket.save(update_fields=["first_response_at"])
        else:
            logger.warning(
                "GetTicket failed for #%s: %s", whmcs_ticket_id, detail.get("message")
            )

    apply_sla_evaluation(ticket)
    return ticket


def sync_tickets():
    """Pages through GetTickets and upserts each ticket in its own try/except
    (per-item isolation, matching run_synthetic_checks.py) so one bad ticket
    can't abort the whole pass. Returns (synced_count, errored_count)."""
    client = WHMCSClient()
    synced, errored = 0, 0
    for summary in client.iter_tickets():
        try:
            _sync_one_ticket(client, summary)
            synced += 1
        except Exception:
            errored += 1
            logger.exception("Failed to sync WHMCS ticket %s", summary.get("id"))
    return synced, errored

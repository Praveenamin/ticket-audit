"""WHMCS -> local ticket sync. Thin management command delegates here (fat
service-module split, matching calculate_sli_compliance.py's convention).

Project-scoped: every row this writes is tagged with the Project it came from,
so multiple WHMCS instances' data never mixes (see audit/dump_import.py for
the DB-dump-based counterpart to this same ingestion shape).
"""

import logging
from datetime import datetime, timedelta, timezone as dt_timezone

from django.db.models import Max
from django.utils import timezone

from .models import Department, TicketEscalationAnalysis, TicketReply, TicketSnapshot
from .s3_archive import archive_reply
from .sla import apply_sla_evaluation
from .tid_format import format_tid
from .whmcs_client import WHMCSClient

logger = logging.getLogger("audit")

# WHMCS timestamps are plain 'YYYY-MM-DD HH:MM:SS' strings with no timezone info.
# Confirmed with Praveen (2026-07-28): the Dev WHMCS instance's admin timezone is
# IST (UTC+5:30, no DST) -- a fixed offset is exact, no zoneinfo/tzdata needed.
WHMCS_TZ = dt_timezone(timedelta(hours=5, minutes=30))


def _parse_whmcs_dt(value):
    if not value:
        return None
    naive = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=WHMCS_TZ).astimezone(dt_timezone.utc)


def _format_whmcs_dt(value):
    # Inverse of _parse_whmcs_dt -- WHMCS_TZ (IST) stays owned by sync.py, not
    # whmcs_client.py, matching the existing split of responsibilities.
    if value is None:
        return None
    return timezone.localtime(value, WHMCS_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _upsert_department(project, deptid, deptname):
    if not deptid:
        return None
    department, _ = Department.objects.update_or_create(
        project=project, whmcs_deptid=deptid,
        defaults={"name": deptname or f"Department {deptid}", "last_seen_at": timezone.now()},
    )
    return department


def _upsert_replies(project, ticket, replies_payload):
    replies = (replies_payload or {}).get("reply", [])
    if isinstance(replies, dict):
        replies = [replies]

    should_archive = project.s3_archive_enabled and ticket.s3_archive_eligible
    should_analyze_escalation = project.escalation_analysis_enabled and ticket.status != "Closed"
    tid_display = ticket.tid or ticket.whmcs_ticket_id

    first_response_at = None
    for reply in replies:
        posted_at = _parse_whmcs_dt(reply.get("date"))
        # `or ""`, not `.get(key, "")` -- see the identical fix/comment on
        # _sync_one_ticket's ticket fields: WHMCS can return an explicit
        # JSON null here too, which .get()'s default wouldn't catch.
        author_type = reply.get("requestor_type") or ""
        reply_obj, _created = TicketReply.objects.update_or_create(
            ticket=ticket,
            whmcs_reply_id=str(reply.get("replyid")),
            defaults={
                "author_name": reply.get("name") or "",
                "author_type": author_type,
                "admin_name": reply.get("admin") or "",
                "message": reply.get("message") or "",
                "rating": reply.get("rating") or 0,
                "posted_at": posted_at,
            },
        )
        if author_type == TicketReply.OPERATOR and posted_at is not None:
            if first_response_at is None or posted_at < first_response_at:
                first_response_at = posted_at

        if should_archive and reply_obj.archived_to_s3_at is None:
            try:
                archive_reply(reply_obj, project, tid_display)
            except Exception:
                # archive_reply already catches boto3's own expected failures and
                # returns False -- this is only a backstop against a genuinely
                # unexpected bug, so one reply's failure can never also skip
                # apply_sla_evaluation for the rest of this ticket.
                logger.exception("Unexpected error archiving reply %s to S3", reply_obj.id)

    if should_analyze_escalation:
        # update_or_create, not get_or_create: unlike ClosedTicketSummary's "queue
        # once, never again" character, this row needs to flip back to QUEUED on
        # EVERY qualifying sync so the score stays current as the conversation
        # evolves. Being inside _upsert_replies at all already means the thread just
        # changed (the only caller gates on needs_full_refresh), so no extra "did
        # anything change" check is needed here. This also self-heals a stuck
        # processing/needs_review/failed row the next time real activity resumes --
        # no separate retry job required.
        TicketEscalationAnalysis.objects.update_or_create(
            ticket=ticket, defaults={"status": TicketEscalationAnalysis.STATUS_QUEUED},
        )

    return first_response_at


def _sync_one_ticket(project, client, summary):
    whmcs_ticket_id = summary["id"]
    last_reply_at = _parse_whmcs_dt(summary.get("lastreply"))

    existing = TicketSnapshot.objects.filter(
        project=project, whmcs_ticket_id=whmcs_ticket_id,
    ).first()
    needs_full_refresh = existing is None or existing.last_reply_at != last_reply_at

    department = _upsert_department(project, summary.get("deptid"), summary.get("deptname"))

    ticket, _ = TicketSnapshot.objects.update_or_create(
        project=project, whmcs_ticket_id=whmcs_ticket_id,
        defaults={
            "tid": format_tid(summary.get("tid", ""), whmcs_ticket_id, synthesize=project.use_synthetic_tid),
            "department": department,
            # .get(key, "") only falls back to "" when the key is *absent* --
            # WHMCS returns some of these as an explicit JSON null on older
            # tickets (confirmed: requestor_name), which .get() passes
            # through as None and violates these columns' NOT NULL
            # constraint. `or ""` catches None too, not just a missing key.
            "subject": summary.get("subject") or "",
            "status": summary.get("status") or "",
            "priority": summary.get("priority") or "",
            "requestor_name": summary.get("requestor_name") or "",
            "requestor_email": summary.get("requestor_email") or "",
            "opened_at": _parse_whmcs_dt(summary.get("date")),
            "last_reply_at": last_reply_at,
            "synced_at": timezone.now(),
        },
    )

    if ticket.status == "Closed" and TicketSnapshot.should_bump_closed_at(
        ticket.closed_at, last_reply_at
    ):
        ticket.closed_at = last_reply_at or timezone.now()
        ticket.save(update_fields=["closed_at"])

    # Decided once, the first time this ticket is ever synced -- never added to
    # update_or_create's defaults above, so a later sync can't structurally flip it
    # back to False. Known, accepted boundary case: a ticket opened at 23:58 IST
    # whose first sync lands at 00:02 IST the next day is judged by *discovery*
    # date, not literal open time -- an inherent edge of any calendar-day
    # definition, not worth engineering around.
    if existing is None and project.s3_archive_enabled:
        opened_local_date = timezone.localtime(ticket.opened_at).date() if ticket.opened_at else None
        if opened_local_date == timezone.localdate():
            ticket.s3_archive_eligible = True
            ticket.save(update_fields=["s3_archive_eligible"])

    if needs_full_refresh:
        detail = client.get_ticket(whmcs_ticket_id)
        if detail.get("result") == "success":
            first_response_at = _upsert_replies(project, ticket, detail.get("replies"))
            if first_response_at is not None and ticket.first_response_at != first_response_at:
                ticket.first_response_at = first_response_at
                ticket.save(update_fields=["first_response_at"])
        else:
            logger.warning(
                "GetTicket failed for #%s: %s", whmcs_ticket_id, detail.get("message")
            )

    apply_sla_evaluation(ticket)
    return ticket


def refresh_ticket_replies(project, ticket):
    """Re-fetch GetTicket and re-upsert this ticket's replies right now, regardless of
    whether last_reply_at changed -- unlike _sync_one_ticket's needs_full_refresh gate.
    Used by the one-time ratings backfill (backfill_reply_ratings) to pick up the new
    `rating` field on tickets already synced before it existed. Returns True on a
    successful GetTicket call. Deliberately does not touch first_response_at/
    apply_sla_evaluation -- backfilling ratings on an unchanged reply set has no reason
    to perturb SLA state; keep the blast radius to exactly the new field."""
    client = WHMCSClient(
        base_url=project.whmcs_base_url, identifier=project.whmcs_api_identifier,
        secret=project.whmcs_api_secret,
    )
    detail = client.get_ticket(ticket.whmcs_ticket_id)
    if detail.get("result") != "success":
        logger.warning("GetTicket failed for #%s: %s", ticket.whmcs_ticket_id, detail.get("message"))
        return False
    _upsert_replies(project, ticket, detail.get("replies"))
    return True


def sync_tickets(project):
    """Pages through GetTickets for one API-sourced Project and upserts each
    ticket in its own try/except (per-item isolation, matching
    run_synthetic_checks.py) so one bad ticket can't abort the whole pass.
    Returns (synced_count, errored_count).

    Incremental: the watermark is MAX(last_reply_at) already stored for this
    project -- not a separately-persisted field, so it's always exactly consistent
    with what actually got saved (self-healing: a ticket whose save failed/errored
    partway through a previous pass simply doesn't contribute to the watermark, so
    it's naturally re-included next time). None (a project's very first sync --
    no TicketSnapshot rows yet) makes iter_tickets walk every ticket, unordered,
    exactly as before this optimization existed."""
    client = WHMCSClient(
        base_url=project.whmcs_base_url,
        identifier=project.whmcs_api_identifier,
        secret=project.whmcs_api_secret,
    )
    watermark = TicketSnapshot.objects.filter(project=project).aggregate(Max("last_reply_at"))["last_reply_at__max"]
    synced, errored = 0, 0
    for summary in client.iter_tickets(stop_at_lastreply=_format_whmcs_dt(watermark)):
        try:
            _sync_one_ticket(project, client, summary)
            synced += 1
        except Exception:
            errored += 1
            logger.exception(
                "Failed to sync WHMCS ticket %s for project %s", summary.get("id"), project.name
            )
    return synced, errored

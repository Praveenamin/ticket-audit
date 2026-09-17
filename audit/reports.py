"""Aggregate reporting queries, kept separate from admin.py so the query
logic is directly testable without going through the view/HTTP layer --
matches sla.py/sync.py's plain-service-module convention.
"""

from datetime import timedelta

from django.db.models import Case, CharField, Count, Value, When
from django.utils import timezone

from .models import ClosedTicketSummary, TicketReply, TicketSnapshot

# The 4 buckets the Closed Tickets Summary report's status filter offers --
# coarser than ClosedTicketSummary.STATUS_CHOICES on purpose (an operator
# thinks "is this done or not", not "drafted vs needs_review vs failed"):
# every one of those three terminal-but-not-drafted-cleanly statuses reads
# as "completed" here, since the pipeline did finish running either way.
CLOSED_TICKETS_STATUS_FILTERS = {
    "not_queued": None,  # handled separately below (no ClosedTicketSummary row at all)
    "queued": [ClosedTicketSummary.STATUS_QUEUED],
    "processing": [ClosedTicketSummary.STATUS_PROCESSING],
    "completed": [
        ClosedTicketSummary.STATUS_DRAFTED,
        ClosedTicketSummary.STATUS_NEEDS_REVIEW,
        ClosedTicketSummary.STATUS_FAILED,
    ],
}

def _client_label(prefix=""):
    return Case(
        When(**{f"{prefix}client__name__isnull": False}, then=f"{prefix}client__name"),
        When(**{f"{prefix}requestor_name__gt": ""}, then=f"{prefix}requestor_name"),
        default=Value("(unknown)"),
        output_field=CharField(),
    )


# Zero-prefix case, unchanged for client_monthly_report/closed_tickets_summary below
# (both already rooted at TicketSnapshot). rated_replies_report is rooted at
# TicketReply instead, so it calls _client_label("ticket__") directly.
_CLIENT_LABEL = _client_label()


def client_monthly_report(project_id, year, month):
    """Ticket count per client, opened in the given calendar month. Prefers
    the linked Client account's name (from tblclients, when the dump import
    populated it); falls back to the ticket's own often-blank requestor_name
    field, then to "(unknown)" -- rather than splintering into many blank
    rows or, worse, silently treating blank as its own "client"."""
    rows = (
        TicketSnapshot.objects.filter(
            project_id=project_id, opened_at__year=year, opened_at__month=month,
        )
        .annotate(client_label=_CLIENT_LABEL)
        .values("client_label")
        .annotate(ticket_count=Count("id"))
        .order_by("-ticket_count", "client_label")
    )
    return [{"label": row["client_label"], "count": row["ticket_count"]} for row in rows]


def tech_monthly_report(project_id, year, month):
    """Distinct ticket count per operator who posted at least one reply
    during the given calendar month -- credited by REPLY activity that
    month, not by when the ticket was originally opened, so ongoing work on
    an older ticket still counts."""
    rows = (
        TicketReply.objects.filter(
            ticket__project_id=project_id, author_type=TicketReply.OPERATOR,
            posted_at__year=year, posted_at__month=month,
        )
        .exclude(admin_name="")
        .values("admin_name")
        .annotate(ticket_count=Count("ticket", distinct=True))
        .order_by("-ticket_count", "admin_name")
    )
    return [{"label": row["admin_name"], "count": row["ticket_count"]} for row in rows]


def top_tickets_by_replies(project_id, days, limit=10):
    """Top `limit` tickets by reply count within the last `days` days."""
    cutoff = timezone.now() - timedelta(days=days)
    rows = (
        TicketReply.objects.filter(ticket__project_id=project_id, posted_at__gte=cutoff)
        .values("ticket_id")
        .annotate(reply_count=Count("id"))
        .order_by("-reply_count")[:limit]
    )
    counts = {row["ticket_id"]: row["reply_count"] for row in rows}
    tickets = TicketSnapshot.objects.in_bulk(counts.keys())
    return [
        {"ticket": tickets[ticket_id], "reply_count": count}
        for ticket_id, count in counts.items()
        if ticket_id in tickets
    ]


def closed_tickets_summary(project_id, date_from=None, date_to=None, status_filter=None):
    """One row per Closed ticket, optionally bounded by `closed_at` (either
    side None = unbounded on that side). Callers pass timezone-aware
    datetimes -- date-string parsing/defaulting lives in the admin view, not
    here, matching this module's existing convention of taking ready
    primitives rather than raw GET params. Deliberately INCLUDES tickets
    with no ClosedTicketSummary yet (summary=None in the returned row) --
    legitimate whenever this window is wider than the auto-queue window
    (closed_ticket_summary.DEFAULT_QUEUE_WINDOW_DAYS), rather than silently
    hiding not-yet-audited tickets from a report whose whole point is
    auditing closed tickets.

    `status_filter` is one of CLOSED_TICKETS_STATUS_FILTERS's keys (None/
    unrecognized = no filtering, every status included)."""
    tickets = TicketSnapshot.objects.filter(project_id=project_id, status="Closed")
    if date_from is not None:
        tickets = tickets.filter(closed_at__gte=date_from)
    if date_to is not None:
        tickets = tickets.filter(closed_at__lt=date_to)
    if status_filter == "not_queued":
        tickets = tickets.filter(closed_summary__isnull=True)
    elif status_filter in CLOSED_TICKETS_STATUS_FILTERS:
        tickets = tickets.filter(closed_summary__status__in=CLOSED_TICKETS_STATUS_FILTERS[status_filter])
    tickets = (
        tickets.select_related("client", "closed_summary")
        .annotate(client_label=_CLIENT_LABEL)
        .order_by("-closed_at")
    )
    return [
        {"ticket": ticket, "client_label": ticket.client_label, "summary": getattr(ticket, "closed_summary", None)}
        for ticket in tickets
    ]


def rated_replies_report(project_id, date_from=None, date_to=None):
    """One row per client-rated reply -- rating > 0 only; 0 means unrated, not "rated
    zero stars" (confirmed against real data: unrated is the overwhelming majority).
    Bounded by the rated reply's own posted_at -- WHMCS records no separate "date the
    client submitted the rating" anywhere, so this is the closest available proxy, not
    an exact one (a client can rate a reply well after it was posted, and there is no
    way to detect that here)."""
    replies = TicketReply.objects.filter(ticket__project_id=project_id, rating__gt=0)
    if date_from is not None:
        replies = replies.filter(posted_at__gte=date_from)
    if date_to is not None:
        replies = replies.filter(posted_at__lt=date_to)
    replies = (
        replies.select_related("ticket", "ticket__client")
        .annotate(client_label=_client_label("ticket__"))
        .order_by("-posted_at")
    )
    return [
        {
            "ticket": reply.ticket, "client_label": reply.client_label,
            "rating": reply.rating, "tech": reply.admin_name or "(unknown)",
        }
        for reply in replies
    ]

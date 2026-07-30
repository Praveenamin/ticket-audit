"""SLA policy resolution + per-exchange breach evaluation.

Follows the same compute-then-save(update_fields=...) shape as StackSense's
apply_service_health: resolve state in Python, write it once via update_fields.

The breach model is a per-exchange reply-turnaround check, not a single
ticket-level deadline: WHMCS's "Answered" status recurs on every staff reply
(including a mere clarifying question) and cycles with "Customer-Reply", so
there is no single "resolution instant" to anchor on. Instead:
  - First response: the first Operator reply must land within target of
    `opened_at` (unchanged, single check).
  - Every subsequent client message ("turn") must get an Operator reply within
    target of that client turn's LAST message -- checked independently across
    the whole thread.
"""

import re
from datetime import timedelta

from django.utils import timezone

from .models import SLAPolicy, TicketReply, TicketSnapshot

SLA_FIELDS = [
    "sla_policy",
    "first_response_due_at",
    "first_response_met",
    "follow_up_met",
    "first_response_target_minutes_at_eval",
    "follow_up_target_minutes_at_eval",
    "sla_status",
]

_TAG_RE = re.compile(r"<[^>]+>")


def resolve_policy(project_id, department_id):
    """Most-specific-wins precedence: department-specific beats the global
    (department-null) default. Ties broken by most recently created policy.
    Scoped to `project_id` first -- a global policy in one project must never
    apply to another project's tickets."""
    candidates = [
        policy
        for policy in SLAPolicy.objects.filter(project_id=project_id, active=True)
        if policy.matches(department_id)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.specificity_rank(), p.created_at), reverse=True)
    return candidates[0]


def _side(author_type):
    if author_type == TicketReply.OPERATOR:
        return "operator"
    if author_type in (TicketReply.OWNER, TicketReply.CONTACT):
        return "client"
    return None


def _group_into_turns(replies):
    """Consecutive same-side messages merge into one turn (e.g. two client
    messages sent back-to-back before any reply count as a single burst,
    anchored on the last one). Replies with an unrecognized/blank author_type
    are skipped entirely rather than being allowed to split or hide a turn."""
    turns = []
    for reply in replies:
        side = _side(reply.author_type)
        if side is None:
            continue
        if turns and turns[-1][0] == side:
            turns[-1][1].append(reply)
        else:
            turns.append((side, [reply]))
    return turns


def _fmt(delta):
    total_minutes = int(delta.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def _snippet(message, limit=80):
    text = _TAG_RE.sub(" ", message or "").strip()
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def evaluate_reply_turnaround(ticket, replies, target_minutes, now):
    """Walk every client turn AFTER the first operator response and check it
    got answered within `target_minutes` of its LAST message. Returns
    (overall_ok, breach_details):
      overall_ok=None  -- no follow-up client turn has occurred yet, or the
                          only ones so far are still pending (not overdue)
      overall_ok=True  -- every follow-up turn seen so far was answered in time
      overall_ok=False -- at least one follow-up turn breached
    """
    if target_minutes is None or ticket.first_response_at is None:
        return None, []

    target = timedelta(minutes=target_minutes)
    effective_now = ticket.closed_at or now

    tail = [r for r in replies if r.posted_at > ticket.first_response_at]
    turns = _group_into_turns(tail)

    any_breach = False
    any_pending = False
    any_followup_turn = False
    breach_details = []

    for i, (side, msgs) in enumerate(turns):
        if side != "client":
            continue
        any_followup_turn = True
        anchor = msgs[-1]
        when = anchor.posted_at.strftime("%Y-%m-%d %H:%M")
        snippet = _snippet(anchor.message)
        next_turn = turns[i + 1] if i + 1 < len(turns) else None

        if next_turn is not None and next_turn[0] == "operator":
            gap = next_turn[1][0].posted_at - anchor.posted_at
            if gap > target:
                any_breach = True
                breach_details.append(
                    f'Reply #{anchor.whmcs_reply_id} ({when}, "{snippet}") took '
                    f"{_fmt(gap)} to get an operator reply (target {_fmt(target)})."
                )
        else:
            elapsed = effective_now - anchor.posted_at
            if elapsed > target:
                any_breach = True
                if ticket.closed_at is not None:
                    breach_details.append(
                        f'Reply #{anchor.whmcs_reply_id} ({when}, "{snippet}") was never '
                        f"answered -- the ticket closed {_fmt(elapsed)} later "
                        f"(target {_fmt(target)})."
                    )
                else:
                    breach_details.append(
                        f'Reply #{anchor.whmcs_reply_id} ({when}, "{snippet}") has had no '
                        f"operator reply for {_fmt(elapsed)} (target {_fmt(target)})."
                    )
            elif ticket.closed_at is None:
                any_pending = True
            # else: closed while still within target -- fine, no breach, no pending.

    if any_breach:
        return False, breach_details
    if not any_followup_turn or any_pending:
        return None, []
    return True, []


def explain_sla(ticket, replies=None, now=None):
    """Human-readable breach reasons, recomputed on every view from the
    permanent TicketReply rows -- never persisted as rendered text (would go
    stale). Uses the target minutes SNAPSHOTTED at last evaluation, not the
    live SLAPolicy, so an old ticket's explanation stays reproducible even if
    Praveen edits the policy's targets later."""
    now = now or timezone.now()

    if ticket.sla_policy_id is None:
        return ["No SLA policy applies to this ticket's department."]

    lines = []
    if ticket.first_response_met is False:
        if ticket.first_response_at:
            detail = f"the first operator reply came at {ticket.first_response_at:%Y-%m-%d %H:%M}"
        else:
            detail = "there has been no operator reply yet"
        lines.append(
            f"Initial response missed: target "
            f"{ticket.first_response_target_minutes_at_eval} min after the ticket opened "
            f"({ticket.opened_at:%Y-%m-%d %H:%M}); {detail}."
        )

    replies = replies if replies is not None else list(ticket.ticket_replies.all())
    _ok, followup_lines = evaluate_reply_turnaround(
        ticket, replies, ticket.follow_up_target_minutes_at_eval, now
    )
    lines.extend(followup_lines)

    if ticket.status == TicketSnapshot.ON_HOLD_STATUS:
        lines.insert(
            0,
            "Ticket is currently On Hold -- SLA verdict is suppressed. Notes below "
            "reflect the underlying data as if it weren't on hold.",
        )

    return lines or ["No SLA issues detected so far."]


def apply_sla_evaluation(ticket: TicketSnapshot, now=None):
    now = now or timezone.now()
    policy = resolve_policy(ticket.project_id, ticket.department_id)

    ticket.sla_policy = policy

    if policy is None:
        ticket.first_response_due_at = None
        ticket.first_response_met = None
        ticket.follow_up_met = None
        ticket.first_response_target_minutes_at_eval = None
        ticket.follow_up_target_minutes_at_eval = None
        ticket.sla_status = None
        ticket.save(update_fields=SLA_FIELDS)
        return ticket

    ticket.first_response_target_minutes_at_eval = policy.first_response_target_minutes
    ticket.follow_up_target_minutes_at_eval = policy.resolution_target_minutes

    ticket.first_response_due_at = ticket.opened_at + timedelta(
        minutes=policy.first_response_target_minutes
    )

    # First-response check is unchanged from Phase 1: always compared against
    # live `now`, never frozen at closure (monotonic -- once overdue it stays
    # overdue, so there's no drift risk the way a single resolution deadline had).
    if ticket.first_response_at is not None:
        ticket.first_response_met = ticket.first_response_at <= ticket.first_response_due_at
    else:
        ticket.first_response_met = None if now <= ticket.first_response_due_at else False

    replies = list(ticket.ticket_replies.all())
    ticket.follow_up_met, _details = evaluate_reply_turnaround(
        ticket, replies, policy.resolution_target_minutes, now
    )

    ticket.sla_status = _overall_status(ticket)
    ticket.save(update_fields=SLA_FIELDS)
    return ticket


def _overall_status(ticket):
    """On Hold suppresses the DISPLAYED verdict only -- first_response_met /
    follow_up_met above are still computed and stored normally. Met requires
    an actual on-time first response (not just "not False"), closing the gap
    where a ticket closed instantly with zero replies used to show Met."""
    if ticket.status == TicketSnapshot.ON_HOLD_STATUS:
        return None

    if ticket.first_response_met is False or ticket.follow_up_met is False:
        return TicketSnapshot.SLA_BREACHED

    if (
        ticket.closed_at is not None
        and ticket.first_response_met is True
        and ticket.follow_up_met is not False
    ):
        return TicketSnapshot.SLA_MET

    return None

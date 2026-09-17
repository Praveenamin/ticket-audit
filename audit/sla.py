"""SLA policy resolution + per-turn breach evaluation.

Follows the same compute-then-save(update_fields=...) shape as StackSense's
apply_service_health: resolve state in Python, write it once via update_fields.

WHMCS's status field is admin-discretionary for "In Progress" vs "Answered"
(the tech picks it when replying) but NOT for "Open"/"Customer-Reply" -- those
two are set automatically the instant a client sends a message, no admin
judgment involved. That means "status is Open or Customer-Reply" is exactly
equivalent to "we're in a client turn awaiting the operator's first touch" --
fully reconstructable from the reply thread alone, no matter how many times
the ticket has been reopened. So there is no single "initial response"
instant and no single "resolution" instant -- every client turn (the
original open AND every later reopen/Customer-Reply) gets its own
initial-response-style check, and every operator turn gets its own
resolution-style check:
  - Response check (first_response_target): every CLIENT turn must get its
    first operator reply within target of that turn's LAST message.
  - Resolution check (resolution_target): every OPERATOR turn's FIRST message
    (an ack, "In Progress"-style) must be followed, within that SAME
    uninterrupted turn, by a final message (the "Answered"-style solution)
    within target. If a turn never grows past one message, resolution is
    trivially met -- that one reply WAS the solution.
"""

import re
from datetime import timedelta

from django.utils import timezone

from .models import SLAPolicy, TicketReply, TicketSnapshot

SLA_FIELDS = [
    "sla_policy",
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
    """"Operator" is WHMCS's one and only staff-side requestor_type value --
    every other non-blank value it uses is some flavor of "the person on the
    client's side of this ticket" (Owner, Contact, and, confirmed against
    real production data, Authorized User/Guest/Registered User/Sub-account).
    Treating anything non-blank and non-Operator as client covers all of
    these, and any future WHMCS role label, without this needing to be
    extended by hand every time one more turns up.

    Real bug this fixes: the previous version only recognized Owner/Contact
    as client, an incomplete whitelist -- "Authorized User" (768 replies in
    production) wasn't on it, so a client reply using that role went
    unrecognized and was silently dropped by _group_into_turns (see its own
    docstring: unrecognized/blank author_type is skipped, not just treated as
    neither side). That let the operator turns on either side of the dropped
    message merge into one, inflating the measured gap between them into a
    false SLA breach (confirmed real case, ticket NKW-994803: two genuinely
    fast turnarounds, 50m and 25m, stitched into one false 21h 2m gap)."""
    if not author_type:
        return None
    return "operator" if author_type == TicketReply.OPERATOR else "client"


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


def _response_breach_events(ticket, replies, target_minutes, now):
    """Every CLIENT turn -- the original open and every later reopen/
    Customer-Reply alike -- must get its first operator reply within target.
    If it DOES get one, the clock anchors on the turn's LAST message (one
    reply covering a whole burst of client messages satisfies it). If it
    NEVER gets one, the clock anchors on the turn's FIRST message instead --
    that's when the silence actually started, and anchoring on the last
    message would let a long-unanswered burst hide behind its own final,
    recent-looking ping (confirmed against real data: a 9,890-message
    monitoring-alert burst spanning 2+ DAYS with zero replies closed in the
    same instant as its last message, and would show as "met" if anchored
    there instead of the burst's start). Returns (overall_ok, events); each
    event: {reply, raw (actual gap/elapsed timedelta), target (timedelta),
    never_answered, closed}. The overdue-over-target amount is `raw - target`."""
    if target_minutes is None:
        return None, []

    target = timedelta(minutes=target_minutes)
    effective_now = ticket.closed_at or now
    turns = _group_into_turns(replies)

    events = []
    any_pending = False
    any_client_turn = False

    for i, (side, msgs) in enumerate(turns):
        if side != "client":
            continue
        any_client_turn = True
        next_turn = turns[i + 1] if i + 1 < len(turns) else None

        if next_turn is not None and next_turn[0] == "operator":
            anchor = msgs[-1]
            gap = next_turn[1][0].posted_at - anchor.posted_at
            if gap > target:
                events.append({
                    "reply": anchor, "raw": gap, "target": target, "never_answered": False,
                })
        else:
            anchor = msgs[0]
            elapsed = effective_now - anchor.posted_at
            if elapsed > target:
                events.append({
                    "reply": anchor, "raw": elapsed, "target": target,
                    "never_answered": True, "closed": ticket.closed_at is not None,
                })
            elif ticket.closed_at is None:
                any_pending = True
            # else: closed while still within target -- fine, no breach, no pending.

    if events:
        return False, events
    if not any_client_turn or any_pending:
        return None, events
    return True, events


def _is_scheduled_ack(message):
    """Heuristic, not a real signal (confirmed against real ticket
    XYC-778415: client asked for an 11pm-scheduled action; operator's ack
    said "we will proceed with your request at the scheduled time";
    completed 13h19m later -- a legitimate client-requested delay, not a
    slow response). WHMCS doesn't retain ticket status-history and this
    project only stores the latest synced status, so there's no way to
    confirm a genuine "On Hold" period actually happened -- this is the
    best available proxy, and it WILL misfire in both directions (an
    operator padding an excuse with the word, or a genuinely scheduled ack
    that just doesn't happen to use it)."""
    return "schedule" in (message or "").lower()


def _resolution_breach_events(ticket, replies, target_minutes, now):
    """Every OPERATOR turn's FIRST message (an ack) must be followed, within
    that SAME uninterrupted turn, by a final message (the solution) within
    target. The instant that follow-up lands, the gap is fixed and resolved
    -- it does NOT keep ticking against a live clock just because the ticket
    hasn't been formally closed (confirmed against real data: an ack answered
    35 minutes later showed as breached-for-days because the ticket sat in
    "Answered" untouched afterward -- the client not replying isn't a
    resolution problem). Only a turn that's STILL just a single message (no
    follow-up sent yet at all) is genuinely unresolved, and only then, only if
    it's the trailing (last) turn overall, does it stay measured against the
    live clock -- any earlier operator turn is already sealed off by the
    client's next message, so a single-message one there was clearly
    sufficient. An ack whose own wording flags it as a scheduled/deferred
    action (_is_scheduled_ack) skips this check entirely, resolved outright
    regardless of gap. Returns (overall_ok, events) in the same shape as
    _response_breach_events."""
    if target_minutes is None:
        return None, []

    target = timedelta(minutes=target_minutes)
    effective_now = ticket.closed_at or now
    turns = _group_into_turns(replies)

    events = []
    any_pending = False
    any_operator_turn = False

    for turn_i, (side, msgs) in enumerate(turns):
        if side != "operator":
            continue
        any_operator_turn = True
        anchor = msgs[0]
        is_trailing = turn_i == len(turns) - 1

        if _is_scheduled_ack(anchor.message):
            # Treated as resolved outright, not measured against target at
            # all -- see _is_scheduled_ack's own caveats.
            continue

        if len(msgs) >= 2:
            gap = msgs[-1].posted_at - anchor.posted_at
            if gap > target:
                events.append({
                    "reply": anchor, "raw": gap, "target": target, "never_answered": False,
                })
            # else: resolved within target -- fixed and done, whether trailing or not.
        elif is_trailing and ticket.status != TicketSnapshot.ANSWERED_STATUS:
            # Still just a single message AND the ticket isn't marked Answered
            # -- genuinely an open ack awaiting a solution, so it keeps
            # ticking against the live clock. If status IS Answered, WHMCS's
            # own signal says this one message already WAS the solution
            # (confirmed against real data: a tech replying with a direct fix
            # -- no separate ack step -- leaves exactly one trailing operator
            # message, and the ticket then just sits Answered indefinitely
            # since the client doesn't need to reply again; without this
            # check that showed as breached-for-days).
            elapsed = effective_now - anchor.posted_at
            if elapsed > target:
                events.append({
                    "reply": anchor, "raw": elapsed, "target": target,
                    "never_answered": True, "closed": ticket.closed_at is not None,
                })
            elif ticket.closed_at is None:
                any_pending = True
            # else: closed while still within target -- resolved fine.
        # else: a single-message, non-trailing operator turn (the client's
        # next message means this ack WAS the sufficient solution), or a
        # trailing one where status=Answered already confirms the same.

    if events:
        return False, events
    if not any_operator_turn or any_pending:
        return None, events
    return True, events


def _format_response_events(events):
    lines = []
    for event in events:
        anchor = event["reply"]
        when = timezone.localtime(anchor.posted_at).strftime("%Y-%m-%d %H:%M")
        snippet = _snippet(anchor.message)
        if not event["never_answered"]:
            lines.append(
                f'Reply #{anchor.whmcs_reply_id} ({when}, "{snippet}") took '
                f"{_fmt(event['raw'])} to get an operator reply (target {_fmt(event['target'])})."
            )
        elif event["closed"]:
            lines.append(
                f'Reply #{anchor.whmcs_reply_id} ({when}, "{snippet}") was never '
                f"answered -- the ticket closed {_fmt(event['raw'])} later "
                f"(target {_fmt(event['target'])})."
            )
        else:
            lines.append(
                f'Reply #{anchor.whmcs_reply_id} ({when}, "{snippet}") has had no '
                f"operator reply for {_fmt(event['raw'])} (target {_fmt(event['target'])})."
            )
    return lines


def _format_resolution_events(events):
    lines = []
    for event in events:
        anchor = event["reply"]
        when = timezone.localtime(anchor.posted_at).strftime("%Y-%m-%d %H:%M")
        snippet = _snippet(anchor.message)
        if not event["never_answered"]:
            lines.append(
                f'Reply #{anchor.whmcs_reply_id} ({when}, "{snippet}") took '
                f"{_fmt(event['raw'])} from that acknowledgment to a final answer "
                f"(target {_fmt(event['target'])})."
            )
        elif event["closed"]:
            lines.append(
                f'Reply #{anchor.whmcs_reply_id} ({when}, "{snippet}") never got a '
                f"final answer -- the ticket closed {_fmt(event['raw'])} later "
                f"(target {_fmt(event['target'])})."
            )
        else:
            lines.append(
                f'Reply #{anchor.whmcs_reply_id} ({when}, "{snippet}") has had no '
                f"final answer for {_fmt(event['raw'])} (target {_fmt(event['target'])})."
            )
    return lines


def breach_summary(ticket, replies=None, now=None):
    """Compact one-line summary of WHICH check breached, stating the actual
    response/resolution time (not the overshoot past target) -- the terse
    list-column counterpart to explain_sla's full multi-line detail-page
    text. Blank ("-") for tickets that aren't breached."""
    now = now or timezone.now()
    replies = replies if replies is not None else list(ticket.ticket_replies.all())
    parts = []

    if ticket.first_response_met is False:
        _ok, events = _response_breach_events(
            ticket, replies, ticket.first_response_target_minutes_at_eval, now
        )
        if events:
            worst = max(events, key=lambda e: e["raw"])
            if worst["never_answered"]:
                parts.append(f"No operator reply yet ({_fmt(worst['raw'])})")
            else:
                parts.append(f"Operator responded after {_fmt(worst['raw'])}")

    if ticket.follow_up_met is False:
        _ok, events = _resolution_breach_events(
            ticket, replies, ticket.follow_up_target_minutes_at_eval, now
        )
        if events:
            worst = max(events, key=lambda e: e["raw"])
            if worst["never_answered"]:
                parts.append(f"No final answer yet ({_fmt(worst['raw'])})")
            else:
                parts.append(f"Operator resolved after {_fmt(worst['raw'])}")

    return "; ".join(parts) if parts else "-"


def explain_sla(ticket, replies=None, now=None):
    """Human-readable breach reasons, recomputed on every view from the
    permanent TicketReply rows -- never persisted as rendered text (would go
    stale). Uses the target minutes SNAPSHOTTED at last evaluation, not the
    live SLAPolicy, so an old ticket's explanation stays reproducible even if
    Praveen edits the policy's targets later."""
    now = now or timezone.now()

    if ticket.sla_policy_id is None:
        return ["No SLA policy applies to this ticket's department."]

    replies = replies if replies is not None else list(ticket.ticket_replies.all())

    lines = []
    _ok, response_events = _response_breach_events(
        ticket, replies, ticket.first_response_target_minutes_at_eval, now
    )
    lines.extend(_format_response_events(response_events))

    _ok, resolution_events = _resolution_breach_events(
        ticket, replies, ticket.follow_up_target_minutes_at_eval, now
    )
    lines.extend(_format_resolution_events(resolution_events))

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
        ticket.first_response_met = None
        ticket.follow_up_met = None
        ticket.first_response_target_minutes_at_eval = None
        ticket.follow_up_target_minutes_at_eval = None
        ticket.sla_status = None
        ticket.save(update_fields=SLA_FIELDS)
        return ticket

    ticket.first_response_target_minutes_at_eval = policy.first_response_target_minutes
    ticket.follow_up_target_minutes_at_eval = policy.resolution_target_minutes

    replies = list(ticket.ticket_replies.all())

    ticket.first_response_met, _events = _response_breach_events(
        ticket, replies, policy.first_response_target_minutes, now
    )
    ticket.follow_up_met, _events = _resolution_breach_events(
        ticket, replies, policy.resolution_target_minutes, now
    )

    ticket.sla_status = _overall_status(ticket)
    ticket.save(update_fields=SLA_FIELDS)
    return ticket


def _overall_status(ticket):
    """On Hold still suppresses the DISPLAYED verdict only -- first_response_met/
    follow_up_met are still computed and stored normally underneath, unrelated
    to the binary rule below.

    Otherwise binary by design -- every ticket resolves to exactly Breached
    or Met, never a third "pending" state: a still-open ticket that hasn't
    missed anything YET reads as "currently meeting SLA" (Met), not
    undetermined. The one case that must NOT get that same benefit of the
    doubt: a ticket that's already CLOSED but never actually got a confirmed
    on-time verdict for one of the two checks (in practice, a ticket closed
    with zero client or zero operator turns ever) -- there's no evidence it
    was met, so that's Breached, not silently upgraded to Met just because
    nothing technically failed (closing the gap where a ticket closed
    instantly with zero replies used to show Met)."""
    if ticket.status == TicketSnapshot.ON_HOLD_STATUS:
        return None

    if ticket.first_response_met is False or ticket.follow_up_met is False:
        return TicketSnapshot.SLA_BREACHED

    if ticket.closed_at is not None and (
        ticket.first_response_met is None or ticket.follow_up_met is None
    ):
        return TicketSnapshot.SLA_BREACHED

    return TicketSnapshot.SLA_MET

    return None

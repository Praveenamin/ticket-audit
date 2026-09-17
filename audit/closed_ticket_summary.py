"""Build an LLM prompt from one Closed ticket's reply thread, call Ollama for
a structured JSON draft (problem source / type / root cause / how it was
fixed), validate it, and store the result on a ClosedTicketSummary row for a
human to review and edit.

Mirrors llm_audit.py's shape exactly (same three-way needs_review/failed/
success outcome, same management-command/scheduler wiring) and reuses its
Ollama-calling and transcript-building helpers directly rather than
duplicating a second copy of the HTTP-calling code -- see _build_transcript's
own docstring in llm_audit.py.
"""

import json
import logging
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone

from .llm_audit import MAX_REPLIES, TicketTooLargeError, _build_transcript, _call_ollama
from .models import ClosedTicketSummary, TicketSnapshot

logger = logging.getLogger("audit")

ALLOWED_PROBLEM_TYPES = {choice[0] for choice in ClosedTicketSummary.PROBLEM_TYPE_CHOICES}

PROMPT_TEMPLATE = """You are auditing a CLOSED customer support ticket for a hosting \
company's operations manager, to summarize what the underlying problem was and how it \
was fixed. Read the full conversation below and respond with ONLY a single JSON object \
(no markdown, no commentary) in exactly this shape:

{{
  "problem_source": "a short (1-4 word) label naming the subsystem/technical area this ticket concerns, e.g. 'Storage', 'Cloudstack', 'Billing', 'DNS', 'Networking' -- your own best label based on the conversation, not from a fixed list",
  "problem_type": one of "bug", "feature", "update", "security_fix", "scaling", "new_config", "ops_action",
  "problem_root_cause": "1-3 sentences on the underlying root cause, based on the conversation and internal notes",
  "fixed_on": "1-2 sentences on how it was actually resolved and what was communicated back to the client"
}}

Ticket subject: {subject}

Below, "Agent" and "Client" lines are the visible conversation. "Internal Note,
staff-only" lines were never sent to the client -- staff added them for other
staff's reference (e.g. recording how something was actually fixed).

Conversation (chronological):
{transcript}

Respond with only the JSON object described above."""


def build_prompt(ticket, replies=None, notes=None):
    return PROMPT_TEMPLATE.format(
        subject=ticket.subject or "(no subject)",
        transcript=_build_transcript(ticket, replies, notes),
    )


def _parse_and_validate(raw_text):
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("Top-level response was not a JSON object.")

    problem_type = data.get("problem_type")
    if problem_type not in ALLOWED_PROBLEM_TYPES:
        raise ValueError(f"problem_type {problem_type!r} not one of {sorted(ALLOWED_PROBLEM_TYPES)}.")

    strings = {}
    for key in ("problem_source", "problem_root_cause", "fixed_on"):
        value = data.get(key)
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string.")
        strings[key] = value

    return {"problem_type": problem_type, **strings}


def _mark_needs_review(summary, raw_text, error_message):
    summary.status = ClosedTicketSummary.STATUS_NEEDS_REVIEW
    summary.raw_response = raw_text
    summary.error_message = error_message[:2000]
    summary.finished_at = timezone.now()
    summary.save(update_fields=["status", "raw_response", "error_message", "finished_at"])


def run_closed_ticket_summary(summary_id):
    """Full pipeline for one ClosedTicketSummary: build prompt -> call Ollama
    -> validate -> store. Same three-way outcome as run_ticket_audit, never a
    fabricated result:
      - ticket too large, timeout, or unparseable/invalid JSON -> needs_review
        (expected, recoverable outcome; no re-raise)
      - any other error (connection refused, Ollama down, HTTP 5xx) -> failed,
        re-raised (the scheduler's own try/except around call_command(...)
        swallows/logs it)
      - success -> drafted, all fields populated, awaiting human review
    """
    summary = ClosedTicketSummary.objects.select_related("ticket").get(id=summary_id)
    summary.status = ClosedTicketSummary.STATUS_PROCESSING
    summary.started_at = timezone.now()
    summary.model_used = settings.OLLAMA_MODEL
    summary.save(update_fields=["status", "started_at", "model_used"])

    try:
        prompt = build_prompt(summary.ticket)
    except TicketTooLargeError as exc:
        _mark_needs_review(summary, "", str(exc))
        return summary

    try:
        raw_text = _call_ollama(prompt)
    except requests.exceptions.Timeout as exc:
        _mark_needs_review(summary, "", f"Ollama request timed out: {exc}")
        return summary
    except Exception as exc:
        logger.exception("Ollama call failed for ClosedTicketSummary %s", summary_id)
        summary.status = ClosedTicketSummary.STATUS_FAILED
        summary.error_message = str(exc)[:2000]
        summary.finished_at = timezone.now()
        summary.save(update_fields=["status", "error_message", "finished_at"])
        raise

    try:
        payload = _parse_and_validate(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        _mark_needs_review(summary, raw_text, f"Could not parse/validate Ollama response: {exc}")
        return summary

    summary.status = ClosedTicketSummary.STATUS_DRAFTED
    summary.problem_source = payload["problem_source"]
    summary.problem_type = payload["problem_type"]
    summary.problem_root_cause = payload["problem_root_cause"]
    summary.fixed_on = payload["fixed_on"]
    summary.raw_response = raw_text
    summary.finished_at = timezone.now()
    summary.save(update_fields=[
        "status", "problem_source", "problem_type", "problem_root_cause", "fixed_on",
        "raw_response", "finished_at",
    ])
    return summary


def _queue_tickets(tickets):
    created = 0
    for ticket in tickets:
        _summary, was_created = ClosedTicketSummary.objects.get_or_create(ticket=ticket)
        if was_created:
            created += 1
    return created


def queue_recent_closed_tickets(window_days=None):
    """Auto-queue a ClosedTicketSummary for every Closed ticket, closed
    within the last `window_days` (default settings.
    CLOSED_TICKET_SUMMARY_QUEUE_WINDOW_DAYS, read live -- same pattern as
    _call_ollama's settings.OLLAMA_MODEL access -- not cached at import
    time, so overriding the setting takes effect immediately, no restart
    needed), that doesn't have one yet. get_or_create rather than a bare
    create -- cheap insurance against a double-tick race even though the
    scheduler is single-threaded; the closed_summary__isnull filter already
    makes this idempotent in the common case. Returns the number of rows
    created."""
    if window_days is None:
        window_days = settings.CLOSED_TICKET_SUMMARY_QUEUE_WINDOW_DAYS
    tickets = TicketSnapshot.objects.filter(
        status="Closed",
        closed_at__gte=timezone.now() - timedelta(days=window_days),
        closed_summary__isnull=True,
    )
    return _queue_tickets(tickets)


def queue_closed_tickets_for_range(project_id, date_from, date_to):
    """Manual, project-scoped counterpart to queue_recent_closed_tickets --
    backs the "Queue for AI Summary" button on the Closed Tickets Summary
    report, which filters by an explicit project + calendar date range
    (matching reports.closed_tickets_summary's own bounds) rather than a
    rolling "last N days from now" window. Same idempotent get_or_create
    shape, so clicking it again (e.g. after widening the date range) never
    double-queues a ticket that already has a summary in any status."""
    tickets = TicketSnapshot.objects.filter(
        project_id=project_id, status="Closed",
        closed_at__gte=date_from, closed_at__lt=date_to,
        closed_summary__isnull=True,
    )
    return _queue_tickets(tickets)


def average_processing_seconds(default=90):
    """Rough per-ticket ETA for the queue-confirm page: the average
    wall-clock time of every ClosedTicketSummary run so far that actually
    reached Ollama (has both started_at and finished_at), so a big backlog
    gets an honest time estimate instead of just a bare ticket count. Falls
    back to `default` seconds if nothing has completed yet to average from.
    Plain Python, not a DB aggregate -- this table is small (one row per
    closed ticket ever summarized), not worth an ORM duration-expression."""
    durations = [
        (cs.finished_at - cs.started_at).total_seconds()
        for cs in ClosedTicketSummary.objects.exclude(started_at=None).exclude(finished_at=None)
        .only("started_at", "finished_at")
    ]
    return (sum(durations) / len(durations)) if durations else default

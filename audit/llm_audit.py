"""Build an LLM prompt from one ticket's reply thread, call Ollama for a
structured JSON verdict (sentiment / missed queries / positives / negatives),
validate it, and store the result on a TicketAudit row.

Mirrors dump_import.py's process_dump_upload(upload_id) shape: one
entry-point function, called by a thin management command, called by the
scheduler's guard-block.
"""

import json
import logging

import requests
from django.conf import settings
from django.utils import timezone

from .models import TicketAudit
from .sla import _TAG_RE, _side

logger = logging.getLogger("audit")

ALLOWED_SENTIMENTS = {
    TicketAudit.SENTIMENT_POSITIVE, TicketAudit.SENTIMENT_NEUTRAL,
    TicketAudit.SENTIMENT_NEGATIVE, TicketAudit.SENTIMENT_MIXED,
}

# Guards against the pathological case sla.py already documents (a real
# ~9,890-message client burst) blowing up the prompt -- see build_prompt.
MAX_REPLIES = 300
MAX_MESSAGE_CHARS = 3000

# Ollama defaults num_ctx to 2048 tokens when a request doesn't specify one --
# well under a real multi-reply thread, so it would silently truncate the
# transcript (no error) rather than tell us. Size the context window to the
# actual prompt every call instead. Floor matches Ollama's own default (so
# small tickets keep its normal footprint/speed); cap keeps a pathological
# thread (bounded by MAX_REPLIES/MAX_MESSAGE_CHARS above) from demanding an
# unbounded amount of RAM/time on CPU-only inference.
MIN_NUM_CTX = 2048
MAX_NUM_CTX = 32768

PROMPT_TEMPLATE = """You are auditing a customer support ticket for a hosting \
company's support-quality manager. Read the full conversation below and \
respond with ONLY a single JSON object (no markdown, no commentary) in \
exactly this shape:

{{
  "sentiment_label": one of "positive", "neutral", "negative", "mixed",
  "sentiment_summary": "1-2 sentences on the customer's overall sentiment across the thread",
  "missed_queries": ["short description of each customer question/request that was NEVER addressed by the agent -- empty array if none"],
  "positives": ["short description of something the agent did well"],
  "negatives": ["short description of something the agent could have done better"]
}}

Ticket subject: {subject}

Below, "Agent" and "Client" lines are the visible conversation. "Internal Note,
staff-only" lines were never sent to the client -- staff added them for other
staff's reference (e.g. recording how something was actually fixed).

Conversation (chronological):
{transcript}

Respond with only the JSON object described above."""


def _strip_html(text):
    return " ".join(_TAG_RE.sub(" ", text or "").split())


class TicketTooLargeError(Exception):
    """Raised by build_prompt when a ticket has more messages (replies +
    internal notes) than MAX_REPLIES -- caught by run_ticket_audit and
    turned into a needs_review row rather than silently truncating (which
    risks quietly dropping the exact missed query the audit exists to
    catch)."""


def _build_transcript(ticket, replies=None, notes=None):
    """Shared by every LLM-audit-style module in this app (currently
    llm_audit.build_prompt and closed_ticket_summary.build_prompt) so there's
    one place that walks replies+notes into a chronological, Agent/Client/
    Internal-Note-labeled transcript -- not a second parallel copy."""
    replies = replies if replies is not None else list(ticket.ticket_replies.all())
    notes = notes if notes is not None else list(ticket.ticket_notes.all())
    if len(replies) + len(notes) > MAX_REPLIES:
        raise TicketTooLargeError(
            f"ticket has {len(replies) + len(notes)} messages, too large to auto-audit "
            f"(limit {MAX_REPLIES})"
        )

    entries = []
    for reply in replies:
        side = _side(reply.author_type)
        if side is None:
            continue
        who = "Agent" if side == "operator" else "Client"
        name = (reply.admin_name if side == "operator" else reply.author_name) or "unknown"
        message = _strip_html(reply.message)[:MAX_MESSAGE_CHARS]
        entries.append((reply.posted_at, f"{who} ({name})", message))
    for note in notes:
        name = note.admin_name or "unknown"
        message = _strip_html(note.message)[:MAX_MESSAGE_CHARS]
        entries.append((note.posted_at, f"Internal Note, staff-only ({name})", message))
    entries.sort(key=lambda entry: entry[0])

    lines = [
        f"[{timezone.localtime(when).strftime('%Y-%m-%d %H:%M')}] {label}: {message}"
        for when, label, message in entries
    ]
    return "\n\n".join(lines) or "(no messages)"


def build_prompt(ticket, replies=None, notes=None):
    return PROMPT_TEMPLATE.format(
        subject=ticket.subject or "(no subject)",
        transcript=_build_transcript(ticket, replies, notes),
    )


def _num_ctx_for_prompt(prompt):
    """~3 chars/token estimate (English text, Llama tokenizer) plus headroom
    for the JSON response, clamped to [MIN_NUM_CTX, MAX_NUM_CTX]."""
    estimated_tokens = len(prompt) // 3 + 512
    return max(MIN_NUM_CTX, min(MAX_NUM_CTX, estimated_tokens))


def _call_ollama(prompt):
    response = requests.post(
        f"{settings.OLLAMA_API_URL}/api/generate",
        json={
            "model": settings.OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"num_ctx": _num_ctx_for_prompt(prompt)},
        },
        timeout=settings.OLLAMA_TIMEOUT,
    )
    response.raise_for_status()
    return response.json().get("response", "")


def _parse_and_validate(raw_text):
    data = json.loads(raw_text)
    if not isinstance(data, dict):
        raise ValueError("Top-level response was not a JSON object.")

    sentiment_label = data.get("sentiment_label")
    if sentiment_label not in ALLOWED_SENTIMENTS:
        raise ValueError(f"sentiment_label {sentiment_label!r} not one of {sorted(ALLOWED_SENTIMENTS)}.")

    sentiment_summary = data.get("sentiment_summary")
    if not isinstance(sentiment_summary, str):
        raise ValueError("sentiment_summary must be a string.")

    lists = {}
    for key in ("missed_queries", "positives", "negatives"):
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"{key} must be a list of strings.")
        lists[key] = value

    return {"sentiment_label": sentiment_label, "sentiment_summary": sentiment_summary, **lists}


def _mark_needs_review(audit, raw_text, error_message):
    audit.status = TicketAudit.STATUS_NEEDS_REVIEW
    audit.raw_response = raw_text
    audit.error_message = error_message[:2000]
    audit.finished_at = timezone.now()
    audit.save(update_fields=["status", "raw_response", "error_message", "finished_at"])


def run_ticket_audit(audit_id):
    """Full pipeline for one TicketAudit: build prompt -> call Ollama ->
    validate -> store. Three-way outcome, never a fabricated result:
      - ticket too large, timeout, or unparseable/invalid JSON -> needs_review
        (expected, recoverable outcome; no re-raise)
      - any other error (connection refused, Ollama down, HTTP 5xx) -> failed,
        re-raised (mirrors process_dump_upload's convention -- the
        scheduler's own try/except around call_command(...) swallows/logs it)
      - success -> done, all fields populated
    """
    audit = TicketAudit.objects.select_related("ticket").get(id=audit_id)
    audit.status = TicketAudit.STATUS_PROCESSING
    audit.started_at = timezone.now()
    audit.model_used = settings.OLLAMA_MODEL
    audit.save(update_fields=["status", "started_at", "model_used"])

    try:
        prompt = build_prompt(audit.ticket)
    except TicketTooLargeError as exc:
        _mark_needs_review(audit, "", str(exc))
        return audit

    try:
        raw_text = _call_ollama(prompt)
    except requests.exceptions.Timeout as exc:
        _mark_needs_review(audit, "", f"Ollama request timed out: {exc}")
        return audit
    except Exception as exc:
        logger.exception("Ollama call failed for TicketAudit %s", audit_id)
        audit.status = TicketAudit.STATUS_FAILED
        audit.error_message = str(exc)[:2000]
        audit.finished_at = timezone.now()
        audit.save(update_fields=["status", "error_message", "finished_at"])
        raise

    try:
        payload = _parse_and_validate(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        _mark_needs_review(audit, raw_text, f"Could not parse/validate Ollama response: {exc}")
        return audit

    audit.status = TicketAudit.STATUS_DONE
    audit.sentiment_label = payload["sentiment_label"]
    audit.sentiment_summary = payload["sentiment_summary"]
    audit.missed_queries = payload["missed_queries"]
    audit.positives = payload["positives"]
    audit.negatives = payload["negatives"]
    audit.raw_response = raw_text
    audit.finished_at = timezone.now()
    audit.save(update_fields=[
        "status", "sentiment_label", "sentiment_summary", "missed_queries",
        "positives", "negatives", "raw_response", "finished_at",
    ])
    return audit

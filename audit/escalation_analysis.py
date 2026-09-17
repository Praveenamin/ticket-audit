"""Build an LLM prompt from one ticket's reply thread, call Ollama for a structured
JSON escalation-risk assessment (sentiment category / risk score / frustration driver /
justification), validate it, and store the result on a TicketEscalationAnalysis row.

Distinct from llm_audit.py's TicketAudit: that's a manual, on-demand audit of the
AGENT's performance; this is an automatic, ongoing CUSTOMER-risk radar, re-run every
time a ticket's reply thread changes (see sync.py's _upsert_replies hook -- there is no
queue_recent_*-style periodic scan here, unlike closed_ticket_summary.py, because being
inside that hook at all already means the thread just changed).

Mirrors llm_audit.py/closed_ticket_summary.py's shape exactly (same three-way
needs_review/failed/success outcome, same management-command/scheduler wiring) and
reuses their Ollama-calling and transcript-building helpers directly rather than
duplicating a second copy of the HTTP-calling code.
"""

import json
import logging

import requests
from django.conf import settings
from django.utils import timezone

from .llm_audit import TicketTooLargeError, _build_transcript, _call_ollama
from .models import TicketEscalationAnalysis

logger = logging.getLogger("audit")

ALLOWED_SENTIMENTS = {choice[0] for choice in TicketEscalationAnalysis.SENTIMENT_CHOICES}
ALLOWED_DRIVERS = {choice[0] for choice in TicketEscalationAnalysis.DRIVER_CHOICES}

PROMPT_TEMPLATE = """You are a customer-support risk analyst for a cloud hosting/Linux \
infrastructure company. Read the full conversation below and assess the client's \
current emotional state AND the underlying technical severity, then respond with ONLY \
a single JSON object (no markdown, no commentary) in exactly this shape:

{{
  "sentiment_category": one of "critical", "frustrated", "healthy",
  "escalation_risk_score": integer 0-100 (0 = no risk of escalation, 100 = about to escalate/churn),
  "frustration_driver": one of "downtime", "slow_response", "unresolved_technical_loop", "billing_issue", "repeated_escalation", "miscommunication", "other", or "" (blank ONLY if sentiment_category is "healthy"),
  "justification": "exactly one sentence explaining the score, quoting or closely paraphrasing the SPECIFIC line(s) below that support it -- never a generic description of a pattern that isn't actually shown"
}}

Accuracy rules -- do not invent or embellish what happened:
- Only call something "repeated"/"recurring"/"again" if the SAME underlying request or
  problem genuinely appears more than once in the conversation, separated by at least
  one reply that should have addressed it. A client sending two messages back-to-back
  to add a missed detail (e.g. resending credentials to include a port they forgot) is
  ONE request, not a repeat -- never describe that as repetition.
- Do not use "unresolved_technical_loop" or "repeated_escalation" unless you can point
  to at least two SEPARATE instances of the same problem recurring after it was
  supposedly addressed. A single ask-then-answer exchange (e.g. the agent requests
  credentials once, the client provides them once, access is confirmed) is normal
  support process, not a loop, even if it spans several back-and-forth messages.
- Base the score and driver only on what the conversation actually shows. If you are
  not sure something happened more than once, do not claim that it did.
- Do not claim that a fix was announced, that an issue was declared "resolved", or that
  the client confirmed satisfaction, unless the agent or client LITERALLY said so. If
  the agent's last message says something like "we will investigate further" or "we
  will keep you updated," that means the underlying problem is still OPEN and ongoing
  -- describe it that way, not as a resolution that then contradicts itself.
- "downtime" means the client's OWN site/service/server is itself inaccessible or
  returning server errors. A related-but-distinct problem -- email being rejected by a
  third party (e.g. Gmail), DNS, billing, licensing -- is NOT downtime even if it's
  serious; pick "other" (or the more specific matching driver) instead.

Technical guardrails -- weigh these as heavily as tone, sometimes MORE heavily:
- A client can be calm, polite, even apologetic while describing a SEVERE technical
  situation (e.g. "sorry to bother you again, but the production site is still down").
  Calm language does NOT mean low risk -- score the underlying technical severity, not
  just word choice or politeness.
- Signals that push toward "critical" and a high score even with polite wording:
  production downtime, a site/service completely inaccessible, 5xx/404 errors on a
  LIVE/production environment, data loss, a security breach or suspected compromise, a
  client explicitly mentioning revenue/business impact, or the SAME unresolved
  technical issue being reported again after it was supposedly fixed (this must be a
  real, separately-occurring repeat per the accuracy rules above, not assumed).
- A routine question, a minor config request, or a non-production/staging issue is NOT
  critical by default, even if the client's tone is terse.
- "unresolved_technical_loop" means the same root problem keeps recurring despite
  repeated agent replies -- distinct from "slow_response" (the problem may be fine, but
  replies are too slow) and "downtime" (an active outage, recurring or not).

Ticket subject: {subject}

Below, "Agent" and "Client" lines are the visible conversation. "Internal Note,
staff-only" lines were never sent to the client.

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

    sentiment_category = data.get("sentiment_category")
    if sentiment_category not in ALLOWED_SENTIMENTS:
        raise ValueError(f"sentiment_category {sentiment_category!r} not one of {sorted(ALLOWED_SENTIMENTS)}.")

    score = data.get("escalation_risk_score")
    # isinstance(True, int) is True in Python -- explicitly excluding bool so a
    # malformed "true"/"false" response can't slip through as 1/0.
    if isinstance(score, bool) or not isinstance(score, int) or not (0 <= score <= 100):
        raise ValueError(f"escalation_risk_score must be an int in [0, 100], got {score!r}.")

    driver = data.get("frustration_driver") or ""
    if driver == "":
        if sentiment_category != TicketEscalationAnalysis.SENTIMENT_HEALTHY:
            raise ValueError("frustration_driver may only be blank when sentiment_category is 'healthy'.")
    elif driver not in ALLOWED_DRIVERS:
        raise ValueError(f"frustration_driver {driver!r} not one of {sorted(ALLOWED_DRIVERS)}.")

    justification = data.get("justification")
    if not isinstance(justification, str) or not justification.strip():
        raise ValueError("justification must be a non-empty string.")

    return {
        "sentiment_category": sentiment_category, "escalation_risk_score": score,
        "frustration_driver": driver, "justification": justification.strip(),
    }


def _mark_needs_review(analysis, raw_text, error_message):
    analysis.status = TicketEscalationAnalysis.STATUS_NEEDS_REVIEW
    analysis.raw_response = raw_text
    analysis.error_message = error_message[:2000]
    analysis.finished_at = timezone.now()
    analysis.save(update_fields=["status", "raw_response", "error_message", "finished_at"])


def run_escalation_analysis(analysis_id):
    """Full pipeline for one TicketEscalationAnalysis: build prompt -> call Ollama ->
    validate -> store. Same three-way outcome as run_ticket_audit/
    run_closed_ticket_summary, never a fabricated result:
      - ticket too large, timeout, or unparseable/invalid JSON -> needs_review
        (expected, recoverable outcome; no re-raise)
      - any other error (connection refused, Ollama down, HTTP 5xx) -> failed,
        re-raised (the scheduler's own try/except around call_command(...)
        swallows/logs it)
      - success -> done, all 4 fields populated
    """
    analysis = TicketEscalationAnalysis.objects.select_related("ticket").get(id=analysis_id)
    analysis.status = TicketEscalationAnalysis.STATUS_PROCESSING
    analysis.started_at = timezone.now()
    analysis.model_used = settings.OLLAMA_MODEL
    analysis.save(update_fields=["status", "started_at", "model_used"])

    try:
        prompt = build_prompt(analysis.ticket)
    except TicketTooLargeError as exc:
        _mark_needs_review(analysis, "", str(exc))
        return analysis

    try:
        raw_text = _call_ollama(prompt)
    except requests.exceptions.Timeout as exc:
        _mark_needs_review(analysis, "", f"Ollama request timed out: {exc}")
        return analysis
    except Exception as exc:
        logger.exception("Ollama call failed for TicketEscalationAnalysis %s", analysis_id)
        analysis.status = TicketEscalationAnalysis.STATUS_FAILED
        analysis.error_message = str(exc)[:2000]
        analysis.finished_at = timezone.now()
        analysis.save(update_fields=["status", "error_message", "finished_at"])
        raise

    try:
        payload = _parse_and_validate(raw_text)
    except (json.JSONDecodeError, ValueError) as exc:
        _mark_needs_review(analysis, raw_text, f"Could not parse/validate Ollama response: {exc}")
        return analysis

    analysis.status = TicketEscalationAnalysis.STATUS_DONE
    analysis.sentiment_category = payload["sentiment_category"]
    analysis.escalation_risk_score = payload["escalation_risk_score"]
    analysis.frustration_driver = payload["frustration_driver"]
    analysis.justification = payload["justification"]
    analysis.raw_response = raw_text
    analysis.finished_at = timezone.now()
    analysis.save(update_fields=[
        "status", "sentiment_category", "escalation_risk_score", "frustration_driver",
        "justification", "raw_response", "finished_at",
    ])
    return analysis

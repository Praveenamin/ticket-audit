"""Closed-set tests for the escalation-analysis pipeline. Mirrors
test_closed_ticket_summary.py's structure and local-fixture convention.

BuildPromptTests here only smoke-tests that this module's build_prompt wires
correctly into llm_audit._build_transcript (subject/transcript appear, an
oversized ticket still raises TicketTooLargeError through this module) -- it
does NOT re-test chronological ordering/HTML-stripping/note-labeling, which
are _build_transcript's own job and already exhaustively covered in
test_llm_audit.py. Queuing itself has no tests here -- unlike
closed_ticket_summary.py, this module has no queue_recent_*-style function;
the auto-queue hook lives in sync.py and is tested in test_sync.py's
EscalationAnalysisHookTests.
"""

import json
from unittest import mock

import requests
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .escalation_analysis import (
    ALLOWED_DRIVERS, ALLOWED_SENTIMENTS, _parse_and_validate, build_prompt,
    run_escalation_analysis,
)
from .llm_audit import MAX_REPLIES, TicketTooLargeError
from .models import Project, TicketEscalationAnalysis, TicketReply, TicketSnapshot


def make_project(name="Test Project", **overrides):
    defaults = {"source_type": Project.SOURCE_API}
    defaults.update(overrides)
    project, _ = Project.objects.get_or_create(name=name, defaults=defaults)
    return project


def make_ticket(**overrides):
    defaults = {
        "project": make_project(),
        "whmcs_ticket_id": 1,
        "tid": "T-1",
        "subject": "Production site is down",
        "status": "Open",
        "priority": "High",
        "opened_at": timezone.now(),
    }
    defaults.update(overrides)
    return TicketSnapshot.objects.create(**defaults)


def make_reply(ticket, author_type, posted_at, reply_id, message="msg", author_name="", admin_name=""):
    return TicketReply.objects.create(
        ticket=ticket, whmcs_reply_id=reply_id, author_name=author_name,
        author_type=author_type, admin_name=admin_name, message=message, posted_at=posted_at,
    )


VALID_PAYLOAD = {
    "sentiment_category": "critical",
    "escalation_risk_score": 85,
    "frustration_driver": "downtime",
    "justification": "The client reports the production site is completely down.",
}


class BuildPromptTests(TestCase):
    def test_subject_and_transcript_present(self):
        ticket = make_ticket()
        make_reply(ticket, TicketReply.OWNER, timezone.now(), "0", message="My site is down")
        prompt = build_prompt(ticket)
        self.assertIn("Production site is down", prompt)
        self.assertIn("My site is down", prompt)
        self.assertIn("escalation_risk_score", prompt)
        self.assertIn("sentiment_category", prompt)

    def test_prompt_warns_against_fabricating_repetition(self):
        # Real bug this guards against, confirmed against a live ticket
        # (PQI-644623): the model invented a "repeatedly asked for credentials"
        # narrative for a thread where credentials were asked and provided
        # exactly once. The prompt must explicitly rule this out.
        ticket = make_ticket()
        prompt = build_prompt(ticket)
        self.assertIn("do not invent or embellish", prompt.lower())
        self.assertIn("not a repeat", prompt.lower())
        self.assertIn("at least two SEPARATE instances", prompt)

    def test_prompt_warns_against_fabricating_a_resolution(self):
        # Second real bug on the SAME live ticket, surfaced by re-running after the
        # first fix above: with the repetition claim gone, the model instead invented
        # "the issue has been 'resolved'" when neither party ever said that -- the
        # agent's actual last message was "we will investigate further" (still open).
        ticket = make_ticket()
        prompt = build_prompt(ticket)
        self.assertIn("declared \"resolved\"", prompt)
        self.assertIn("still open and ongoing", prompt.lower())

    def test_prompt_clarifies_downtime_excludes_third_party_rejection(self):
        # Third real bug, same ticket: a Gmail-rejects-our-mail issue got labeled
        # "downtime", which technically means the client's OWN site/server is down.
        ticket = make_ticket()
        prompt = build_prompt(ticket)
        self.assertIn("is NOT downtime", prompt)

    def test_oversized_ticket_raises_through_this_module(self):
        ticket = make_ticket()
        replies = [
            TicketReply(ticket=ticket, whmcs_reply_id=str(i), author_type=TicketReply.OWNER,
                        message="hi", posted_at=timezone.now())
            for i in range(MAX_REPLIES + 1)
        ]
        with self.assertRaises(TicketTooLargeError):
            build_prompt(ticket, replies=replies)


class ParseAndValidateTests(SimpleTestCase):
    def test_valid_payload_parses(self):
        result = _parse_and_validate(json.dumps(VALID_PAYLOAD))
        self.assertEqual(result["sentiment_category"], "critical")
        self.assertEqual(result["escalation_risk_score"], 85)
        self.assertEqual(result["frustration_driver"], "downtime")

    def test_each_allowed_sentiment_accepted(self):
        for sentiment in ALLOWED_SENTIMENTS:
            driver = "" if sentiment == TicketEscalationAnalysis.SENTIMENT_HEALTHY else "billing_issue"
            payload = {**VALID_PAYLOAD, "sentiment_category": sentiment, "frustration_driver": driver}
            result = _parse_and_validate(json.dumps(payload))
            self.assertEqual(result["sentiment_category"], sentiment)

    def test_invalid_sentiment_raises(self):
        payload = {**VALID_PAYLOAD, "sentiment_category": "angry"}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_each_allowed_driver_accepted(self):
        for driver in ALLOWED_DRIVERS:
            payload = {**VALID_PAYLOAD, "frustration_driver": driver}
            result = _parse_and_validate(json.dumps(payload))
            self.assertEqual(result["frustration_driver"], driver)

    def test_invalid_driver_raises(self):
        payload = {**VALID_PAYLOAD, "frustration_driver": "vibes"}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_blank_driver_accepted_when_healthy(self):
        payload = {
            "sentiment_category": "healthy", "escalation_risk_score": 5,
            "frustration_driver": "", "justification": "Client is satisfied.",
        }
        result = _parse_and_validate(json.dumps(payload))
        self.assertEqual(result["frustration_driver"], "")

    def test_blank_driver_rejected_when_not_healthy(self):
        payload = {**VALID_PAYLOAD, "frustration_driver": ""}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_score_boundaries_0_and_100_accepted(self):
        for score in (0, 100):
            payload = {**VALID_PAYLOAD, "escalation_risk_score": score}
            result = _parse_and_validate(json.dumps(payload))
            self.assertEqual(result["escalation_risk_score"], score)

    def test_score_below_0_rejected(self):
        payload = {**VALID_PAYLOAD, "escalation_risk_score": -1}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_score_above_100_rejected(self):
        payload = {**VALID_PAYLOAD, "escalation_risk_score": 101}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_non_int_score_rejected(self):
        payload = {**VALID_PAYLOAD, "escalation_risk_score": "85"}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_bool_score_rejected_despite_being_an_int_subclass(self):
        payload = {**VALID_PAYLOAD, "escalation_risk_score": True}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_blank_justification_rejected(self):
        payload = {**VALID_PAYLOAD, "justification": "   "}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_malformed_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_and_validate("not json at all")

    def test_non_dict_top_level_raises(self):
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(["just", "a", "list"]))


class RunEscalationAnalysisTests(TestCase):
    def _make_analysis_with_thread(self):
        ticket = make_ticket()
        make_reply(ticket, TicketReply.OWNER, timezone.now(), "0", message="The site is down!")
        make_reply(ticket, TicketReply.OPERATOR, timezone.now(), "1", message="Looking into it", admin_name="Bob")
        return TicketEscalationAnalysis.objects.create(ticket=ticket)

    @mock.patch("audit.escalation_analysis._call_ollama")
    def test_success_marks_done_and_populates_fields(self, mock_call):
        mock_call.return_value = json.dumps(VALID_PAYLOAD)
        analysis = self._make_analysis_with_thread()

        run_escalation_analysis(analysis.id)
        analysis.refresh_from_db()

        self.assertEqual(analysis.status, TicketEscalationAnalysis.STATUS_DONE)
        self.assertEqual(analysis.sentiment_category, "critical")
        self.assertEqual(analysis.escalation_risk_score, 85)
        self.assertEqual(analysis.frustration_driver, "downtime")
        self.assertTrue(analysis.justification)
        self.assertTrue(analysis.model_used)
        self.assertIsNotNone(analysis.finished_at)

    @mock.patch("audit.escalation_analysis._call_ollama")
    def test_timeout_marks_needs_review_without_reraising(self, mock_call):
        mock_call.side_effect = requests.exceptions.Timeout("too slow")
        analysis = self._make_analysis_with_thread()

        run_escalation_analysis(analysis.id)  # must not raise
        analysis.refresh_from_db()

        self.assertEqual(analysis.status, TicketEscalationAnalysis.STATUS_NEEDS_REVIEW)
        self.assertIn("timed out", analysis.error_message)

    @mock.patch("audit.escalation_analysis._call_ollama")
    def test_malformed_json_marks_needs_review_and_keeps_raw_response(self, mock_call):
        mock_call.return_value = "not valid json"
        analysis = self._make_analysis_with_thread()

        run_escalation_analysis(analysis.id)
        analysis.refresh_from_db()

        self.assertEqual(analysis.status, TicketEscalationAnalysis.STATUS_NEEDS_REVIEW)
        self.assertEqual(analysis.raw_response, "not valid json")

    @mock.patch("audit.escalation_analysis._call_ollama")
    def test_connection_error_marks_failed_and_reraises(self, mock_call):
        mock_call.side_effect = requests.exceptions.ConnectionError("refused")
        analysis = self._make_analysis_with_thread()

        with self.assertRaises(requests.exceptions.ConnectionError):
            run_escalation_analysis(analysis.id)

        analysis.refresh_from_db()
        self.assertEqual(analysis.status, TicketEscalationAnalysis.STATUS_FAILED)
        self.assertIn("refused", analysis.error_message)

    @mock.patch("audit.escalation_analysis._call_ollama")
    def test_oversized_ticket_skips_ollama_and_marks_needs_review(self, mock_call):
        ticket = make_ticket()
        for i in range(MAX_REPLIES + 1):
            make_reply(ticket, TicketReply.OWNER, timezone.now(), str(i), message="hi")
        analysis = TicketEscalationAnalysis.objects.create(ticket=ticket)

        run_escalation_analysis(analysis.id)
        analysis.refresh_from_db()

        self.assertEqual(analysis.status, TicketEscalationAnalysis.STATUS_NEEDS_REVIEW)
        self.assertIn("too large", analysis.error_message)
        mock_call.assert_not_called()

    @mock.patch("audit.escalation_analysis._call_ollama")
    def test_re_running_an_existing_row_updates_it_in_place(self, mock_call):
        # The "stays current as the conversation evolves" behavior: re-running
        # overwrites the SAME row (OneToOneField), never creates a second one.
        analysis = self._make_analysis_with_thread()
        mock_call.return_value = json.dumps({**VALID_PAYLOAD, "sentiment_category": "critical"})
        run_escalation_analysis(analysis.id)

        mock_call.return_value = json.dumps({
            "sentiment_category": "healthy", "escalation_risk_score": 5,
            "frustration_driver": "", "justification": "Issue resolved, client is happy.",
        })
        run_escalation_analysis(analysis.id)
        analysis.refresh_from_db()

        self.assertEqual(TicketEscalationAnalysis.objects.filter(ticket=analysis.ticket).count(), 1)
        self.assertEqual(analysis.sentiment_category, "healthy")
        self.assertEqual(analysis.escalation_risk_score, 5)

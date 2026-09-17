"""Closed-set tests for the closed-ticket-summary pipeline. Mirrors
test_llm_audit.py's structure and local-fixture convention -- fixtures are
local to this file rather than shared via a factories module.

BuildPromptTests here only smoke-tests that this module's build_prompt wires
correctly into llm_audit._build_transcript (subject/transcript appear, an
oversized ticket still raises TicketTooLargeError through this module) -- it
does NOT re-test chronological ordering/HTML-stripping/note-labeling, which
are _build_transcript's own job and already exhaustively covered in
test_llm_audit.py.
"""

import json
from datetime import timedelta
from unittest import mock

import requests
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .closed_ticket_summary import (
    ALLOWED_PROBLEM_TYPES, _parse_and_validate, average_processing_seconds, build_prompt,
    queue_closed_tickets_for_range, queue_recent_closed_tickets, run_closed_ticket_summary,
)
from .llm_audit import MAX_REPLIES, TicketTooLargeError
from .models import ClosedTicketSummary, Project, TicketReply, TicketSnapshot


def make_project(name="Test Project", **overrides):
    defaults = {"source_type": Project.SOURCE_DUMP}
    defaults.update(overrides)
    project, _ = Project.objects.get_or_create(name=name, defaults=defaults)
    return project


def make_ticket(**overrides):
    defaults = {
        "project": make_project(),
        "whmcs_ticket_id": 1,
        "tid": "T-1",
        "subject": "Server is down",
        "status": "Closed",
        "priority": "Medium",
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
    "problem_source": "Storage",
    "problem_type": "ops_action",
    "problem_root_cause": "VM deployed from a template experiences a delay from the initial copy process.",
    "fixed_on": "Shared the root cause with the client; client agreed.",
}


class BuildPromptTests(TestCase):
    def test_subject_and_transcript_present(self):
        ticket = make_ticket()
        make_reply(ticket, TicketReply.OWNER, timezone.now(), "0", message="My site is down")
        prompt = build_prompt(ticket)
        self.assertIn("Server is down", prompt)
        self.assertIn("My site is down", prompt)
        self.assertIn("problem_source", prompt)
        self.assertIn("problem_type", prompt)

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
        self.assertEqual(result["problem_type"], "ops_action")
        self.assertEqual(result["problem_source"], "Storage")

    def test_each_allowed_problem_type_accepted(self):
        for problem_type in ALLOWED_PROBLEM_TYPES:
            payload = {**VALID_PAYLOAD, "problem_type": problem_type}
            result = _parse_and_validate(json.dumps(payload))
            self.assertEqual(result["problem_type"], problem_type)

    def test_invalid_problem_type_raises(self):
        payload = {**VALID_PAYLOAD, "problem_type": "urgent"}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_malformed_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_and_validate("not json at all")

    def test_non_dict_top_level_raises(self):
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(["just", "a", "list"]))

    def test_missing_problem_type_raises(self):
        payload = {**VALID_PAYLOAD}
        del payload["problem_type"]
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_problem_source_wrong_type_raises(self):
        payload = {**VALID_PAYLOAD, "problem_source": 123}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_problem_root_cause_wrong_type_raises(self):
        payload = {**VALID_PAYLOAD, "problem_root_cause": ["not", "a", "string"]}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_fixed_on_wrong_type_raises(self):
        payload = {**VALID_PAYLOAD, "fixed_on": None}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))


class RunClosedTicketSummaryTests(TestCase):
    def _make_summary_with_thread(self):
        ticket = make_ticket()
        make_reply(ticket, TicketReply.OWNER, timezone.now(), "0", message="Please help")
        make_reply(ticket, TicketReply.OPERATOR, timezone.now(), "1", message="Fixed it", admin_name="Bob")
        return ClosedTicketSummary.objects.create(ticket=ticket)

    @mock.patch("audit.closed_ticket_summary._call_ollama")
    def test_success_marks_drafted_and_populates_fields(self, mock_call):
        mock_call.return_value = json.dumps(VALID_PAYLOAD)
        summary = self._make_summary_with_thread()

        run_closed_ticket_summary(summary.id)
        summary.refresh_from_db()

        self.assertEqual(summary.status, ClosedTicketSummary.STATUS_DRAFTED)
        self.assertEqual(summary.problem_type, "ops_action")
        self.assertEqual(summary.problem_source, "Storage")
        self.assertTrue(summary.model_used)
        self.assertIsNotNone(summary.finished_at)
        self.assertIsNone(summary.reviewed_at)  # drafted, not yet reviewed by a human

    @mock.patch("audit.closed_ticket_summary._call_ollama")
    def test_timeout_marks_needs_review_without_reraising(self, mock_call):
        mock_call.side_effect = requests.exceptions.Timeout("too slow")
        summary = self._make_summary_with_thread()

        run_closed_ticket_summary(summary.id)  # must not raise
        summary.refresh_from_db()

        self.assertEqual(summary.status, ClosedTicketSummary.STATUS_NEEDS_REVIEW)
        self.assertIn("timed out", summary.error_message)

    @mock.patch("audit.closed_ticket_summary._call_ollama")
    def test_malformed_json_marks_needs_review_and_keeps_raw_response(self, mock_call):
        mock_call.return_value = "not valid json"
        summary = self._make_summary_with_thread()

        run_closed_ticket_summary(summary.id)
        summary.refresh_from_db()

        self.assertEqual(summary.status, ClosedTicketSummary.STATUS_NEEDS_REVIEW)
        self.assertEqual(summary.raw_response, "not valid json")

    @mock.patch("audit.closed_ticket_summary._call_ollama")
    def test_connection_error_marks_failed_and_reraises(self, mock_call):
        mock_call.side_effect = requests.exceptions.ConnectionError("refused")
        summary = self._make_summary_with_thread()

        with self.assertRaises(requests.exceptions.ConnectionError):
            run_closed_ticket_summary(summary.id)

        summary.refresh_from_db()
        self.assertEqual(summary.status, ClosedTicketSummary.STATUS_FAILED)
        self.assertIn("refused", summary.error_message)

    @mock.patch("audit.closed_ticket_summary._call_ollama")
    def test_oversized_ticket_skips_ollama_and_marks_needs_review(self, mock_call):
        ticket = make_ticket()
        for i in range(MAX_REPLIES + 1):
            make_reply(ticket, TicketReply.OWNER, timezone.now(), str(i), message="hi")
        summary = ClosedTicketSummary.objects.create(ticket=ticket)

        run_closed_ticket_summary(summary.id)
        summary.refresh_from_db()

        self.assertEqual(summary.status, ClosedTicketSummary.STATUS_NEEDS_REVIEW)
        self.assertIn("too large", summary.error_message)
        mock_call.assert_not_called()


class QueueRecentClosedTicketsTests(TestCase):
    def test_queues_in_window_closed_ticket_with_no_summary(self):
        make_ticket(closed_at=timezone.now() - timedelta(days=1))
        created = queue_recent_closed_tickets(window_days=3)
        self.assertEqual(created, 1)
        self.assertEqual(ClosedTicketSummary.objects.count(), 1)

    def test_does_not_double_queue_a_ticket_that_already_has_a_summary(self):
        ticket = make_ticket(closed_at=timezone.now() - timedelta(days=1))
        ClosedTicketSummary.objects.create(ticket=ticket)

        created = queue_recent_closed_tickets(window_days=3)

        self.assertEqual(created, 0)
        self.assertEqual(ClosedTicketSummary.objects.filter(ticket=ticket).count(), 1)

    def test_ignores_ticket_closed_outside_the_window(self):
        make_ticket(closed_at=timezone.now() - timedelta(days=10))
        created = queue_recent_closed_tickets(window_days=3)
        self.assertEqual(created, 0)

    def test_ignores_non_closed_ticket(self):
        make_ticket(status="Open", closed_at=None)
        created = queue_recent_closed_tickets(window_days=3)
        self.assertEqual(created, 0)

    def test_explicit_window_days_overrides_default(self):
        make_ticket(closed_at=timezone.now() - timedelta(days=5))
        self.assertEqual(queue_recent_closed_tickets(window_days=3), 0)
        self.assertEqual(queue_recent_closed_tickets(window_days=7), 1)

    @override_settings(CLOSED_TICKET_SUMMARY_QUEUE_WINDOW_DAYS=10)
    def test_default_window_reads_the_setting_live(self):
        # window_days=None (the default) must read settings.
        # CLOSED_TICKET_SUMMARY_QUEUE_WINDOW_DAYS at call time, not a value
        # cached when the module was first imported -- otherwise
        # override_settings here would have no effect and this ticket
        # (8 days old) would wrongly fall outside a stale 3-day default.
        make_ticket(closed_at=timezone.now() - timedelta(days=8))
        created = queue_recent_closed_tickets()
        self.assertEqual(created, 1)


class QueueClosedTicketsForRangeTests(TestCase):
    """The manual, project + calendar-date-range scoped counterpart behind
    the report's "Queue for AI Summary" button -- unlike
    queue_recent_closed_tickets, this must never touch another project's
    tickets even if they fall in the same date range."""

    def setUp(self):
        self.project = make_project("P1")
        self.other_project = make_project("P2")
        self.now = timezone.now()

    def test_queues_ticket_in_range_with_no_summary(self):
        ticket = make_ticket(
            project=self.project, whmcs_ticket_id=1, closed_at=self.now - timedelta(days=5),
        )
        created = queue_closed_tickets_for_range(
            self.project.id, self.now - timedelta(days=10), self.now,
        )
        self.assertEqual(created, 1)
        self.assertTrue(ClosedTicketSummary.objects.filter(ticket=ticket).exists())

    def test_does_not_double_queue_an_already_summarized_ticket(self):
        ticket = make_ticket(
            project=self.project, whmcs_ticket_id=1, closed_at=self.now - timedelta(days=5),
        )
        ClosedTicketSummary.objects.create(ticket=ticket)

        created = queue_closed_tickets_for_range(
            self.project.id, self.now - timedelta(days=10), self.now,
        )

        self.assertEqual(created, 0)
        self.assertEqual(ClosedTicketSummary.objects.filter(ticket=ticket).count(), 1)

    def test_excludes_ticket_outside_the_explicit_range(self):
        make_ticket(project=self.project, whmcs_ticket_id=1, closed_at=self.now - timedelta(days=20))
        created = queue_closed_tickets_for_range(
            self.project.id, self.now - timedelta(days=10), self.now,
        )
        self.assertEqual(created, 0)

    def test_excludes_a_different_project_even_in_the_same_range(self):
        make_ticket(
            project=self.other_project, whmcs_ticket_id=1, closed_at=self.now - timedelta(days=5),
        )
        created = queue_closed_tickets_for_range(
            self.project.id, self.now - timedelta(days=10), self.now,
        )
        self.assertEqual(created, 0)

    def test_excludes_non_closed_ticket(self):
        make_ticket(
            project=self.project, whmcs_ticket_id=1, status="Open", closed_at=None,
        )
        created = queue_closed_tickets_for_range(
            self.project.id, self.now - timedelta(days=10), self.now,
        )
        self.assertEqual(created, 0)


class AverageProcessingSecondsTests(TestCase):
    def test_falls_back_to_default_when_nothing_has_completed(self):
        self.assertEqual(average_processing_seconds(default=42), 42)

    def test_averages_completed_runs_only(self):
        ticket_a = make_ticket(whmcs_ticket_id=1)
        ticket_b = make_ticket(whmcs_ticket_id=2)
        now = timezone.now()
        ClosedTicketSummary.objects.create(
            ticket=ticket_a, started_at=now, finished_at=now + timedelta(seconds=30),
        )
        ClosedTicketSummary.objects.create(
            ticket=ticket_b, started_at=now, finished_at=now + timedelta(seconds=90),
        )
        # A still-queued row (no started_at/finished_at) must not skew the average.
        make_ticket(whmcs_ticket_id=3)
        ClosedTicketSummary.objects.create(ticket=TicketSnapshot.objects.get(whmcs_ticket_id=3))

        self.assertEqual(average_processing_seconds(), 60)

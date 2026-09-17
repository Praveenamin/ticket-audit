"""Closed-set tests for the LLM ticket-audit pipeline. Per this codebase's
convention (test_sla.py/test_admin.py), fixtures are local to this file
rather than shared via a factories module.
"""

import json
from unittest import mock

import requests
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .llm_audit import (
    MAX_NUM_CTX, MAX_REPLIES, MIN_NUM_CTX, TicketTooLargeError, _call_ollama,
    _num_ctx_for_prompt, _parse_and_validate, build_prompt, run_ticket_audit,
)
from .models import Project, TicketAudit, TicketNote, TicketReply, TicketSnapshot


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
        "status": "Open",
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


def make_note(ticket, posted_at, note_id, message="note", admin_name="Bob"):
    return TicketNote.objects.create(
        ticket=ticket, whmcs_note_id=note_id, admin_name=admin_name,
        message=message, posted_at=posted_at,
    )


VALID_PAYLOAD = {
    "sentiment_label": "positive",
    "sentiment_summary": "Customer was satisfied with the quick fix.",
    "missed_queries": [],
    "positives": ["Responded quickly"],
    "negatives": [],
}


class BuildPromptTests(TestCase):
    def test_chronological_order_and_labels(self):
        ticket = make_ticket()
        make_reply(
            ticket, TicketReply.OWNER, timezone.now(), "0",
            message="My site is down", author_name="Alice",
        )
        make_reply(
            ticket, TicketReply.OPERATOR, timezone.now(), "1",
            message="Looking into it", admin_name="Bob",
        )
        prompt = build_prompt(ticket)
        self.assertIn("Server is down", prompt)
        down_idx = prompt.index("My site is down")
        looking_idx = prompt.index("Looking into it")
        self.assertLess(down_idx, looking_idx)
        self.assertIn("Client (Alice)", prompt)
        self.assertIn("Agent (Bob)", prompt)

    def test_html_stripped(self):
        ticket = make_ticket()
        make_reply(ticket, TicketReply.OWNER, timezone.now(), "0", message="<p>Hello <b>there</b></p>")
        prompt = build_prompt(ticket)
        self.assertNotIn("<p>", prompt)
        self.assertNotIn("<b>", prompt)
        self.assertIn("Hello there", prompt)

    def test_unrecognized_author_type_skipped(self):
        ticket = make_ticket()
        make_reply(ticket, "", timezone.now(), "0", message="a stray system row")
        prompt = build_prompt(ticket)
        self.assertNotIn("a stray system row", prompt)
        self.assertIn("(no messages)", prompt)

    def test_no_messages_placeholder(self):
        ticket = make_ticket()
        prompt = build_prompt(ticket, replies=[])
        self.assertIn("(no messages)", prompt)

    def test_note_labeled_as_internal_and_interleaved_chronologically(self):
        ticket = make_ticket()
        make_reply(
            ticket, TicketReply.OWNER, timezone.now(), "0",
            message="Site is down", author_name="Alice",
        )
        note = make_note(
            ticket, timezone.now(), "9001",
            message="Root cause was a bad cron job", admin_name="Bob",
        )
        make_reply(
            ticket, TicketReply.OPERATOR, timezone.now(), "1",
            message="All fixed now", admin_name="Bob",
        )
        prompt = build_prompt(ticket)
        self.assertIn("Internal Note, staff-only (Bob)", prompt)
        self.assertIn("Root cause was a bad cron job", prompt)
        # Chronological across BOTH replies and notes, not replies-then-notes.
        down_idx = prompt.index("Site is down")
        note_idx = prompt.index("Root cause was a bad cron job")
        fixed_idx = prompt.index("All fixed now")
        self.assertLess(down_idx, note_idx)
        self.assertLess(note_idx, fixed_idx)

    def test_notes_count_toward_too_large_guard(self):
        ticket = make_ticket()
        replies = [
            TicketReply(ticket=ticket, whmcs_reply_id="0", author_type=TicketReply.OWNER,
                        message="hi", posted_at=timezone.now())
        ]
        notes = [
            TicketNote(ticket=ticket, whmcs_note_id=str(i), message="n", posted_at=timezone.now())
            for i in range(MAX_REPLIES)
        ]
        with self.assertRaises(TicketTooLargeError):
            build_prompt(ticket, replies=replies, notes=notes)

    def test_over_max_replies_raises(self):
        ticket = make_ticket()
        replies = [
            TicketReply(ticket=ticket, whmcs_reply_id=str(i), author_type=TicketReply.OWNER,
                        message="hi", posted_at=timezone.now())
            for i in range(MAX_REPLIES + 1)
        ]
        with self.assertRaises(TicketTooLargeError) as ctx:
            build_prompt(ticket, replies=replies)
        self.assertIn(str(MAX_REPLIES + 1), str(ctx.exception))


class ParseAndValidateTests(SimpleTestCase):
    def test_valid_payload_parses(self):
        result = _parse_and_validate(json.dumps(VALID_PAYLOAD))
        self.assertEqual(result["sentiment_label"], "positive")
        self.assertEqual(result["positives"], ["Responded quickly"])

    def test_malformed_json_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            _parse_and_validate("not json at all")

    def test_non_dict_top_level_raises(self):
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(["just", "a", "list"]))

    def test_missing_sentiment_label_raises(self):
        payload = {**VALID_PAYLOAD}
        del payload["sentiment_label"]
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_invalid_sentiment_label_raises(self):
        payload = {**VALID_PAYLOAD, "sentiment_label": "ecstatic"}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_sentiment_summary_wrong_type_raises(self):
        payload = {**VALID_PAYLOAD, "sentiment_summary": 123}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_missed_queries_not_a_list_raises(self):
        payload = {**VALID_PAYLOAD, "missed_queries": "none"}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))

    def test_positives_with_non_string_item_raises(self):
        payload = {**VALID_PAYLOAD, "positives": [1, 2]}
        with self.assertRaises(ValueError):
            _parse_and_validate(json.dumps(payload))


class CallOllamaTests(SimpleTestCase):
    @mock.patch("audit.llm_audit.requests.post")
    def test_sends_expected_payload_and_returns_response_field(self, mock_post):
        mock_post.return_value = mock.Mock(
            status_code=200, json=lambda: {"response": json.dumps(VALID_PAYLOAD)},
        )
        result = _call_ollama("some prompt")
        self.assertEqual(result, json.dumps(VALID_PAYLOAD))

        args, kwargs = mock_post.call_args
        self.assertTrue(args[0].endswith("/api/generate"))
        self.assertEqual(kwargs["json"]["prompt"], "some prompt")
        self.assertEqual(kwargs["json"]["stream"], False)
        self.assertEqual(kwargs["json"]["format"], "json")
        self.assertEqual(kwargs["json"]["options"]["num_ctx"], MIN_NUM_CTX)
        self.assertIn("timeout", kwargs)

    @mock.patch("audit.llm_audit.requests.post")
    def test_raises_on_non_2xx(self, mock_post):
        response = requests.Response()
        response.status_code = 500
        mock_post.return_value = response
        with self.assertRaises(requests.exceptions.HTTPError):
            _call_ollama("some prompt")


class NumCtxForPromptTests(SimpleTestCase):
    """Ollama defaults num_ctx to 2048 tokens when unset -- silently
    truncating anything longer with no error. Context must be sized to the
    real prompt every call, clamped to a sane, CPU-feasible range."""

    def test_short_prompt_floors_at_min(self):
        self.assertEqual(_num_ctx_for_prompt("hi"), MIN_NUM_CTX)

    def test_long_prompt_scales_up(self):
        prompt = "x" * 60_000  # ~13k tokens, like a real 133-message ticket
        result = _num_ctx_for_prompt(prompt)
        self.assertGreater(result, MIN_NUM_CTX)
        self.assertLessEqual(result, MAX_NUM_CTX)

    def test_pathological_prompt_caps_at_max(self):
        prompt = "x" * 1_000_000
        self.assertEqual(_num_ctx_for_prompt(prompt), MAX_NUM_CTX)


class RunTicketAuditTests(TestCase):
    def _make_audit_with_thread(self):
        ticket = make_ticket()
        make_reply(ticket, TicketReply.OWNER, timezone.now(), "0", message="Help, it's down")
        make_reply(ticket, TicketReply.OPERATOR, timezone.now(), "1", message="Fixed it", admin_name="Bob")
        return TicketAudit.objects.create(ticket=ticket)

    @mock.patch("audit.llm_audit._call_ollama")
    def test_success_marks_done_and_populates_fields(self, mock_call):
        mock_call.return_value = json.dumps(VALID_PAYLOAD)
        audit = self._make_audit_with_thread()

        run_ticket_audit(audit.id)
        audit.refresh_from_db()

        self.assertEqual(audit.status, TicketAudit.STATUS_DONE)
        self.assertEqual(audit.sentiment_label, "positive")
        self.assertEqual(audit.positives, ["Responded quickly"])
        self.assertTrue(audit.model_used)
        self.assertIsNotNone(audit.finished_at)

    @mock.patch("audit.llm_audit._call_ollama")
    def test_timeout_marks_needs_review_without_reraising(self, mock_call):
        mock_call.side_effect = requests.exceptions.Timeout("too slow")
        audit = self._make_audit_with_thread()

        run_ticket_audit(audit.id)  # must not raise
        audit.refresh_from_db()

        self.assertEqual(audit.status, TicketAudit.STATUS_NEEDS_REVIEW)
        self.assertIn("timed out", audit.error_message)

    @mock.patch("audit.llm_audit._call_ollama")
    def test_malformed_json_marks_needs_review_and_keeps_raw_response(self, mock_call):
        mock_call.return_value = "not valid json"
        audit = self._make_audit_with_thread()

        run_ticket_audit(audit.id)
        audit.refresh_from_db()

        self.assertEqual(audit.status, TicketAudit.STATUS_NEEDS_REVIEW)
        self.assertEqual(audit.raw_response, "not valid json")

    @mock.patch("audit.llm_audit._call_ollama")
    def test_connection_error_marks_failed_and_reraises(self, mock_call):
        mock_call.side_effect = requests.exceptions.ConnectionError("refused")
        audit = self._make_audit_with_thread()

        with self.assertRaises(requests.exceptions.ConnectionError):
            run_ticket_audit(audit.id)

        audit.refresh_from_db()
        self.assertEqual(audit.status, TicketAudit.STATUS_FAILED)
        self.assertIn("refused", audit.error_message)

    @mock.patch("audit.llm_audit._call_ollama")
    def test_oversized_ticket_skips_ollama_and_marks_needs_review(self, mock_call):
        ticket = make_ticket()
        for i in range(MAX_REPLIES + 1):
            make_reply(ticket, TicketReply.OWNER, timezone.now(), str(i), message="hi")
        audit = TicketAudit.objects.create(ticket=ticket)

        run_ticket_audit(audit.id)
        audit.refresh_from_db()

        self.assertEqual(audit.status, TicketAudit.STATUS_NEEDS_REVIEW)
        self.assertIn("too large", audit.error_message)
        mock_call.assert_not_called()

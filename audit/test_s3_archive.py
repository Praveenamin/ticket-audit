"""Closed-set tests for s3_archive.py -- no real network calls, boto3.client is
mocked throughout, matching test_whmcs_client.py's module-qualified-patch
convention (@patch("audit.s3_archive.boto3.client"))."""

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from .models import Project, TicketReply, TicketSnapshot
from .s3_archive import (
    _message_to_markdown, _render_reply_markdown, _reply_key, archive_reply, parse_reply_markdown,
    retry_unarchived_replies,
)


def make_project(name="Test Project", **overrides):
    defaults = {
        "source_type": Project.SOURCE_API, "s3_archive_enabled": True,
        "s3_bucket_name": "my-bucket", "s3_access_key_id": "AKIA...", "s3_secret_access_key": "secret",
        "s3_region": "ap-south-1",
    }
    defaults.update(overrides)
    project, _ = Project.objects.get_or_create(name=name, defaults=defaults)
    return project


def make_ticket(project, whmcs_ticket_id=1, tid="453611", eligible=True, opened_at=None):
    return TicketSnapshot.objects.create(
        project=project, whmcs_ticket_id=whmcs_ticket_id, tid=tid, status="Open",
        opened_at=opened_at or timezone.now(), s3_archive_eligible=eligible,
    )


def make_reply(ticket, whmcs_reply_id="8231", **overrides):
    defaults = {
        "author_name": "Bob", "author_type": "Operator", "admin_name": "Bob",
        "message": "hello", "posted_at": timezone.now(),
    }
    defaults.update(overrides)
    return TicketReply.objects.create(ticket=ticket, whmcs_reply_id=whmcs_reply_id, **defaults)


class ReplyKeyTests(TestCase):
    def test_exact_key_for_the_confirmed_real_example(self):
        project = make_project("Stackbill-Whmcs")
        opened_at = datetime(2026, 8, 12, 9, 46, 55, tzinfo=dt_timezone.utc)
        self.assertEqual(
            _reply_key(project, "453611", opened_at, opened_at, "0"),
            "stackbill-whmcs/2026/08/T453611/20260812-1516-0.md",
        )

    def test_key_uses_the_tickets_own_opening_month_not_the_replys_own_month(self):
        # The core scenario from the user's own worked example: a ticket opened
        # Jan 10 whose reply happens Feb 5 still archives under /2026/01/, not
        # /2026/02/ -- the ticket's opening month governs the folder for its whole
        # lifecycle, not each reply's own date.
        project = make_project("Stackbill-Whmcs")
        opened_at = datetime(2026, 1, 10, 9, 30, tzinfo=dt_timezone.utc)
        posted_at = datetime(2026, 2, 5, 11, 0, tzinfo=dt_timezone.utc)

        key = _reply_key(project, "11213", opened_at, posted_at, "39247")

        self.assertIn("/2026/01/", key)
        self.assertNotIn("/2026/02/", key)


class RenderAndParseReplyMarkdownTests(TestCase):
    def test_parse_is_the_exact_inverse_of_render(self):
        frontmatter = {"whmcs_reply_id": "8231", "tid": "453611", "message_html": "hi"}
        content = _render_reply_markdown(frontmatter, "Hello there")

        parsed_frontmatter, parsed_body = parse_reply_markdown(content)

        self.assertEqual(parsed_frontmatter, frontmatter)
        self.assertEqual(parsed_body, "Hello there")

    def test_parse_handles_a_body_that_itself_contains_a_literal_dashes_line(self):
        # Proves the closing-fence search is genuinely unambiguous, not just
        # "works on the happy path" -- yaml.dump() never emits a bare, unindented
        # "---" line of its own (see _yaml_multiline_str_presenter), so the first
        # "\n---\n" after the opening fence really is the frontmatter's own close.
        frontmatter = {"tid": "453611"}
        body = "Section one\n---\nSection two"
        content = _render_reply_markdown(frontmatter, body)

        parsed_frontmatter, parsed_body = parse_reply_markdown(content)

        self.assertEqual(parsed_frontmatter, frontmatter)
        self.assertEqual(parsed_body, body)

    def test_parse_accepts_bytes_as_well_as_str(self):
        content = _render_reply_markdown({"tid": "1"}, "body text")
        self.assertIsInstance(content, bytes)

        frontmatter, body = parse_reply_markdown(content)

        self.assertEqual(frontmatter, {"tid": "1"})
        self.assertEqual(body, "body text")

    def test_missing_opening_fence_raises(self):
        with self.assertRaises(ValueError):
            parse_reply_markdown("no frontmatter here")

    def test_frontmatter_is_rendered_as_yaml_not_json(self):
        content = _render_reply_markdown({"tid": "453611", "whmcs_ticket_id": 143966}, "body").decode()
        self.assertIn("tid: '453611'", content)
        self.assertIn("whmcs_ticket_id: 143966", content)
        self.assertNotIn("{", content)
        self.assertNotIn("}", content)

    def test_multiline_value_renders_as_a_readable_yaml_block_literal(self):
        frontmatter = {"tid": "1", "message_html": "Para one.\n\nPara two."}
        content = _render_reply_markdown(frontmatter, "body").decode()

        self.assertIn("message_html: |", content)

        parsed_frontmatter, _ = parse_reply_markdown(content)
        self.assertEqual(parsed_frontmatter, frontmatter)


class MessageToMarkdownTests(TestCase):
    def test_decodes_numeric_html_entity(self):
        self.assertEqual(_message_to_markdown("platform&#039;s Help"), "platform's Help")

    def test_decodes_named_html_entity(self):
        self.assertEqual(_message_to_markdown("Terms &amp; Conditions"), "Terms & Conditions")

    def test_preserves_crlf_paragraph_breaks(self):
        self.assertEqual(
            _message_to_markdown("Para one.\r\n\r\nPara two."), "Para one.\n\nPara two.",
        )

    def test_br_tag_becomes_a_line_break(self):
        self.assertEqual(_message_to_markdown("Line one<br>Line two"), "Line one\nLine two")

    def test_p_tags_become_paragraph_breaks(self):
        self.assertEqual(_message_to_markdown("<p>First</p><p>Second</p>"), "First\n\nSecond")

    def test_other_tags_are_stripped_without_adding_breaks(self):
        self.assertEqual(_message_to_markdown("<b>Hi</b> <i>there</i>"), "Hi there")

    def test_intra_line_whitespace_is_normalized(self):
        self.assertEqual(_message_to_markdown("Too    many   spaces"), "Too many spaces")

    def test_none_message_returns_empty_string(self):
        self.assertEqual(_message_to_markdown(None), "")

    def test_leading_and_trailing_whitespace_stripped(self):
        self.assertEqual(_message_to_markdown("\r\n\r\n  Hello  \r\n\r\n"), "Hello")


class ArchiveReplyTests(TestCase):
    def setUp(self):
        self.project = make_project()
        self.ticket = make_ticket(self.project, opened_at=timezone.now())
        self.reply = make_reply(self.ticket)

    @patch("audit.s3_archive.boto3.client")
    def test_success_stamps_archived_to_s3_at_and_returns_true(self, mock_boto_client):
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3

        result = archive_reply(self.reply, self.project, "453611")

        self.assertTrue(result)
        self.reply.refresh_from_db()
        self.assertIsNotNone(self.reply.archived_to_s3_at)
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args.kwargs
        self.assertEqual(call_kwargs["Bucket"], "my-bucket")
        self.assertEqual(call_kwargs["ContentType"], "text/markdown")
        self.assertTrue(call_kwargs["Key"].startswith("test-project/"))
        self.assertTrue(call_kwargs["Key"].endswith("-8231.md"))

    @patch("audit.s3_archive.boto3.client")
    def test_client_error_leaves_field_none_returns_false_does_not_raise(self, mock_boto_client):
        from botocore.exceptions import ClientError
        mock_s3 = Mock()
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutObject",
        )
        mock_boto_client.return_value = mock_s3

        result = archive_reply(self.reply, self.project, "453611")

        self.assertFalse(result)
        self.reply.refresh_from_db()
        self.assertIsNone(self.reply.archived_to_s3_at)

    @patch("audit.s3_archive.boto3.client")
    def test_boto_core_error_leaves_field_none_returns_false_does_not_raise(self, mock_boto_client):
        from botocore.exceptions import BotoCoreError
        mock_s3 = Mock()
        mock_s3.put_object.side_effect = BotoCoreError()
        mock_boto_client.return_value = mock_s3

        result = archive_reply(self.reply, self.project, "453611")

        self.assertFalse(result)
        self.reply.refresh_from_db()
        self.assertIsNone(self.reply.archived_to_s3_at)

    @patch("audit.s3_archive.boto3.client")
    def test_blank_bucket_name_skips_the_boto3_call_entirely(self, mock_boto_client):
        self.project.s3_bucket_name = ""
        self.project.save(update_fields=["s3_bucket_name"])

        result = archive_reply(self.reply, self.project, "453611")

        self.assertFalse(result)
        mock_boto_client.assert_not_called()

    @patch("audit.s3_archive.boto3.client")
    def test_uploaded_content_frontmatter_and_body_match_the_reply(self, mock_boto_client):
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        self.reply.message = "<p>Hi Team,</p><p>Please help.</p>"
        self.reply.save(update_fields=["message"])

        archive_reply(self.reply, self.project, "453611")

        content = mock_s3.put_object.call_args.kwargs["Body"]
        frontmatter, body = parse_reply_markdown(content)
        self.assertEqual(frontmatter["whmcs_reply_id"], "8231")
        self.assertEqual(frontmatter["tid"], "453611")
        self.assertEqual(frontmatter["message_html"], "<p>Hi Team,</p><p>Please help.</p>")
        self.assertEqual(body, "Hi Team,\n\nPlease help.")

    @patch("audit.s3_archive.boto3.client")
    def test_uploaded_body_decodes_html_entities(self, mock_boto_client):
        # Real bug, reported against a real already-archived reply: WHMCS stores
        # some punctuation as numeric HTML entities even in a plain-text message
        # (e.g. "platform&#039;s"), and the old _strip_html-based rendering never
        # decoded them, so the archived body showed the literal entity text.
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        self.reply.message = "the platform&#039;s Help section"
        self.reply.save(update_fields=["message"])

        archive_reply(self.reply, self.project, "453611")

        content = mock_s3.put_object.call_args.kwargs["Body"]
        _frontmatter, body = parse_reply_markdown(content)
        self.assertEqual(body, "the platform's Help section")

    @patch("audit.s3_archive.boto3.client")
    def test_uploaded_body_preserves_the_users_real_multi_paragraph_example(self, mock_boto_client):
        # The exact real message text the user pasted as a bug report: a WHMCS
        # message with \r\n\r\n-separated paragraphs must stay multi-paragraph in
        # the archived Markdown body, not collapse into one run-on line.
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        self.reply.message = (
            "Good morning, how are you?\r\n\r\n"
            "We need the platform&#039;s Help section to also be available in "
            "Portuguese and Spanish.\r\n\r\n"
            "Initially, I thought that when selecting the language, a Google "
            "Translate plugin would be used to translate the page. However, when "
            "selecting these options, the page appears blank.\r\n\r\n"
            "Besides English, would it be possible to make the content available "
            "in Portuguese and Spanish as well?"
        )
        self.reply.save(update_fields=["message"])

        archive_reply(self.reply, self.project, "453611")

        content = mock_s3.put_object.call_args.kwargs["Body"]
        _frontmatter, body = parse_reply_markdown(content)
        self.assertEqual(
            body,
            "Good morning, how are you?\n\n"
            "We need the platform's Help section to also be available in "
            "Portuguese and Spanish.\n\n"
            "Initially, I thought that when selecting the language, a Google "
            "Translate plugin would be used to translate the page. However, when "
            "selecting these options, the page appears blank.\n\n"
            "Besides English, would it be possible to make the content available "
            "in Portuguese and Spanish as well?",
        )

    @patch("audit.s3_archive.boto3.client")
    def test_uploaded_body_collapses_more_than_one_consecutive_blank_line(self, mock_boto_client):
        mock_s3 = Mock()
        mock_boto_client.return_value = mock_s3
        self.reply.message = "First paragraph.\r\n\r\n\r\n\r\nSecond paragraph."
        self.reply.save(update_fields=["message"])

        archive_reply(self.reply, self.project, "453611")

        content = mock_s3.put_object.call_args.kwargs["Body"]
        _frontmatter, body = parse_reply_markdown(content)
        self.assertEqual(body, "First paragraph.\n\nSecond paragraph.")


class RetryUnarchivedRepliesTests(TestCase):
    def setUp(self):
        self.project = make_project()
        self.ticket = make_ticket(self.project)

    @patch("audit.s3_archive.boto3.client")
    def test_picks_up_and_archives_an_unarchived_reply_on_an_eligible_project_ticket(self, mock_boto_client):
        mock_boto_client.return_value = Mock()
        reply = make_reply(self.ticket, "1", archived_to_s3_at=None)

        count = retry_unarchived_replies()

        self.assertEqual(count, 1)
        reply.refresh_from_db()
        self.assertIsNotNone(reply.archived_to_s3_at)

    def test_ignores_a_reply_whose_ticket_is_not_eligible(self):
        ineligible_ticket = make_ticket(self.project, whmcs_ticket_id=2, tid="T-2", eligible=False)
        make_reply(ineligible_ticket, "1", archived_to_s3_at=None)

        count = retry_unarchived_replies()

        self.assertEqual(count, 0)

    def test_ignores_a_reply_whose_project_has_the_toggle_off(self):
        other_project = make_project("Off Project", s3_archive_enabled=False)
        other_ticket = make_ticket(other_project, whmcs_ticket_id=3, tid="T-3")
        make_reply(other_ticket, "1", archived_to_s3_at=None)

        count = retry_unarchived_replies()

        self.assertEqual(count, 0)

    def test_ignores_an_already_archived_reply(self):
        make_reply(self.ticket, "1", archived_to_s3_at=timezone.now())

        count = retry_unarchived_replies()

        self.assertEqual(count, 0)

    def test_respects_limit(self):
        for i in range(3):
            make_reply(self.ticket, str(i), archived_to_s3_at=None, posted_at=timezone.now())

        with patch("audit.s3_archive.boto3.client", return_value=Mock()):
            count = retry_unarchived_replies(limit=2)

        self.assertEqual(count, 2)

    def test_one_replys_unexpected_exception_does_not_stop_the_rest_of_the_batch(self):
        good = make_reply(self.ticket, "good", archived_to_s3_at=None)
        bad = make_reply(self.ticket, "bad", archived_to_s3_at=None, posted_at=timezone.now() - timedelta(minutes=1))

        def side_effect(reply, project, tid):
            if reply.whmcs_reply_id == "bad":
                raise RuntimeError("boom")
            reply.archived_to_s3_at = timezone.now()
            reply.save(update_fields=["archived_to_s3_at"])
            return True

        with patch("audit.s3_archive.archive_reply", side_effect=side_effect):
            count = retry_unarchived_replies()

        self.assertEqual(count, 1)
        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertIsNotNone(good.archived_to_s3_at)
        self.assertIsNone(bad.archived_to_s3_at)

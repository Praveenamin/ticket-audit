"""Regression tests for sync.py's WHMCS-JSON-to-model field mapping.

Motivated by a real production bug: WHMCS returns some optional ticket/reply
fields as an explicit JSON `null` (not just an absent key) on older records.
`dict.get(key, "")` only falls back to "" when the key is *missing* -- a
present key with value None passes straight through, and every string field
here is a NOT NULL column, so that surfaced as a repeating
psycopg2.errors.NotNullViolation on every sync cycle for the affected
tickets. These pin the `or ""` fix down as a closed set: every affected
field, individually, as an explicit None -- not just a happy-path smoke test.
"""

from datetime import timedelta, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from .models import Project, TicketEscalationAnalysis, TicketReply, TicketSnapshot
from .sync import _sync_one_ticket, _upsert_replies, refresh_ticket_replies, sync_tickets


def make_project(name="Test Project", **overrides):
    defaults = {"source_type": Project.SOURCE_API}
    defaults.update(overrides)
    project, _ = Project.objects.get_or_create(name=name, defaults=defaults)
    return project


def make_summary(**overrides):
    defaults = {
        "id": 1, "tid": "T-1", "subject": "Server is down", "status": "Open",
        "priority": "Medium", "requestor_name": "Alice", "requestor_email": "alice@example.com",
        "date": "2026-01-01 10:00:00", "lastreply": "2026-01-01 10:00:00",
        "deptid": None, "deptname": None,
    }
    defaults.update(overrides)
    return defaults


def mock_client(get_ticket_result=None):
    client = Mock()
    client.get_ticket.return_value = get_ticket_result or {"result": "success", "replies": {"reply": []}}
    return client


class SyncOneTicketNullFieldTests(TestCase):
    def setUp(self):
        self.project = make_project()

    def test_null_requestor_name_saved_as_empty_string(self):
        ticket = _sync_one_ticket(self.project, mock_client(), make_summary(requestor_name=None))
        self.assertEqual(ticket.requestor_name, "")

    def test_null_requestor_email_saved_as_empty_string(self):
        ticket = _sync_one_ticket(self.project, mock_client(), make_summary(requestor_email=None))
        self.assertEqual(ticket.requestor_email, "")

    def test_null_subject_saved_as_empty_string(self):
        ticket = _sync_one_ticket(self.project, mock_client(), make_summary(subject=None))
        self.assertEqual(ticket.subject, "")

    def test_null_status_saved_as_empty_string(self):
        ticket = _sync_one_ticket(self.project, mock_client(), make_summary(status=None))
        self.assertEqual(ticket.status, "")

    def test_null_priority_saved_as_empty_string(self):
        ticket = _sync_one_ticket(self.project, mock_client(), make_summary(priority=None))
        self.assertEqual(ticket.priority, "")

    def test_normal_values_pass_through_unaffected(self):
        ticket = _sync_one_ticket(self.project, mock_client(), make_summary())
        self.assertEqual(ticket.requestor_name, "Alice")
        self.assertEqual(ticket.subject, "Server is down")


class UpsertRepliesNullFieldTests(TestCase):
    def setUp(self):
        self.project = make_project()
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, status="Open", opened_at=timezone.now(),
        )

    def _reply(self, **overrides):
        defaults = {
            "replyid": 1, "date": "2026-01-01 10:00:00", "requestor_type": "Operator",
            "name": "Bob", "admin": "Bob", "message": "hello",
        }
        defaults.update(overrides)
        return defaults

    def test_null_name_saved_as_empty_string(self):
        _upsert_replies(self.project, self.ticket, {"reply": [self._reply(name=None)]})
        self.assertEqual(TicketReply.objects.get(ticket=self.ticket).author_name, "")

    def test_null_admin_saved_as_empty_string(self):
        _upsert_replies(self.project, self.ticket, {"reply": [self._reply(admin=None)]})
        self.assertEqual(TicketReply.objects.get(ticket=self.ticket).admin_name, "")

    def test_null_message_saved_as_empty_string(self):
        _upsert_replies(self.project, self.ticket, {"reply": [self._reply(message=None)]})
        self.assertEqual(TicketReply.objects.get(ticket=self.ticket).message, "")

    def test_null_requestor_type_saved_as_empty_string(self):
        _upsert_replies(self.project, self.ticket, {"reply": [self._reply(requestor_type=None)]})
        self.assertEqual(TicketReply.objects.get(ticket=self.ticket).author_type, "")

    def test_normal_values_pass_through_unaffected(self):
        _upsert_replies(self.project, self.ticket, {"reply": [self._reply()]})
        reply = TicketReply.objects.get(ticket=self.ticket)
        self.assertEqual(reply.author_name, "Bob")
        self.assertEqual(reply.author_type, "Operator")


class S3ArchiveEligibilityTests(TestCase):
    """s3_archive_eligible is decided exactly once, the first time a ticket is
    ever synced (existing is None) -- never re-evaluated afterward."""

    def setUp(self):
        self.project = make_project(s3_archive_enabled=True)

    def test_ticket_opened_today_becomes_eligible(self):
        ticket = _sync_one_ticket(
            self.project, mock_client(),
            make_summary(date=timezone.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.assertTrue(ticket.s3_archive_eligible)

    def test_ticket_opened_yesterday_never_becomes_eligible(self):
        yesterday = timezone.now() - timedelta(days=1)
        ticket = _sync_one_ticket(
            self.project, mock_client(),
            make_summary(date=yesterday.strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.assertFalse(ticket.s3_archive_eligible)

    def test_toggle_off_means_same_day_ticket_never_becomes_eligible(self):
        project = make_project("Toggle Off Project", s3_archive_enabled=False)
        ticket = _sync_one_ticket(
            project, mock_client(),
            make_summary(id=2, date=timezone.now().strftime("%Y-%m-%d %H:%M:%S")),
        )
        self.assertFalse(ticket.s3_archive_eligible)

    def test_eligibility_persists_across_a_later_sync_pass(self):
        summary = make_summary(date=timezone.now().strftime("%Y-%m-%d %H:%M:%S"))
        ticket = _sync_one_ticket(self.project, mock_client(), summary)
        self.assertTrue(ticket.s3_archive_eligible)

        # A later sync pass for the same ticket (existing is no longer None,
        # simulating a new reply arriving, possibly on a later calendar day) --
        # eligibility must never be recomputed/reset once already decided.
        later_summary = dict(summary, lastreply="2026-01-02 10:00:00")
        ticket = _sync_one_ticket(self.project, mock_client(), later_summary)
        self.assertTrue(ticket.s3_archive_eligible)


class S3ArchiveHookTests(TestCase):
    """_upsert_replies' archival hook -- mocks audit.sync.archive_reply (the
    name as imported into sync.py's own namespace) rather than touching boto3
    at all, matching test_sync.py's existing Mock-based style."""

    def setUp(self):
        self.project = make_project(s3_archive_enabled=True)

    def _eligible_ticket(self, whmcs_ticket_id=1, tid="T-1"):
        return TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=whmcs_ticket_id, tid=tid, status="Open",
            opened_at=timezone.now(), s3_archive_eligible=True,
        )

    def _reply_payload(self, replyid=1):
        return {"reply": [{
            "replyid": replyid, "date": "2026-01-01 10:00:00", "requestor_type": "Operator",
            "name": "Bob", "message": "hi",
        }]}

    @patch("audit.sync.archive_reply")
    def test_new_reply_on_eligible_ticket_gets_archived_once(self, mock_archive):
        ticket = self._eligible_ticket()
        _upsert_replies(self.project, ticket, self._reply_payload())

        mock_archive.assert_called_once()
        reply_arg, project_arg, tid_arg = mock_archive.call_args[0]
        self.assertEqual(reply_arg.whmcs_reply_id, "1")
        self.assertEqual(project_arg, self.project)
        self.assertEqual(tid_arg, "T-1")

    @patch("audit.sync.archive_reply")
    def test_already_archived_reply_is_never_re_passed(self, mock_archive):
        ticket = self._eligible_ticket()
        TicketReply.objects.create(
            ticket=ticket, whmcs_reply_id="1", posted_at=timezone.now(),
            archived_to_s3_at=timezone.now(),
        )

        _upsert_replies(self.project, ticket, self._reply_payload())

        mock_archive.assert_not_called()

    @patch("audit.sync.archive_reply")
    def test_a_genuinely_new_reply_alongside_an_archived_one_is_passed_exactly_once(self, mock_archive):
        ticket = self._eligible_ticket()
        TicketReply.objects.create(
            ticket=ticket, whmcs_reply_id="1", posted_at=timezone.now(),
            archived_to_s3_at=timezone.now(),
        )
        payload = {"reply": [
            {"replyid": 1, "date": "2026-01-01 10:00:00", "requestor_type": "Operator", "message": "old"},
            {"replyid": 2, "date": "2026-01-02 10:00:00", "requestor_type": "Owner", "message": "new"},
        ]}

        _upsert_replies(self.project, ticket, payload)

        mock_archive.assert_called_once()
        reply_arg = mock_archive.call_args[0][0]
        self.assertEqual(reply_arg.whmcs_reply_id, "2")

    @patch("audit.sync.archive_reply")
    def test_toggle_off_means_archive_reply_never_called_even_for_eligible_ticket(self, mock_archive):
        self.project.s3_archive_enabled = False
        self.project.save(update_fields=["s3_archive_enabled"])
        ticket = self._eligible_ticket()

        _upsert_replies(self.project, ticket, self._reply_payload())

        mock_archive.assert_not_called()

    @patch("audit.sync.archive_reply")
    def test_ineligible_ticket_never_gets_archive_reply_called(self, mock_archive):
        ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=2, tid="T-2", status="Open",
            opened_at=timezone.now(), s3_archive_eligible=False,
        )

        _upsert_replies(self.project, ticket, self._reply_payload())

        mock_archive.assert_not_called()

    def test_failed_upload_leaves_archived_to_s3_at_none(self):
        ticket = self._eligible_ticket()
        with patch("audit.sync.archive_reply", return_value=False):
            _upsert_replies(self.project, ticket, self._reply_payload())

        reply = TicketReply.objects.get(ticket=ticket, whmcs_reply_id="1")
        self.assertIsNone(reply.archived_to_s3_at)


class RefreshTicketRepliesTests(TestCase):
    """refresh_ticket_replies: the ratings backfill's re-fetch-regardless-of-
    last_reply_at path, distinct from _sync_one_ticket's needs_full_refresh gate."""

    def setUp(self):
        self.project = make_project()
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, tid="T-1", status="Open", opened_at=timezone.now(),
        )

    @patch("audit.sync.WHMCSClient")
    def test_successful_refetch_upserts_rating_and_returns_true(self, mock_client_cls):
        mock_client_cls.return_value = mock_client(get_ticket_result={
            "result": "success",
            "replies": {"reply": [{
                "replyid": 1, "date": "2026-01-01 10:00:00", "requestor_type": "Operator",
                "admin": "Bob", "message": "fixed it", "rating": 5,
            }]},
        })

        result = refresh_ticket_replies(self.project, self.ticket)

        self.assertTrue(result)
        reply = TicketReply.objects.get(ticket=self.ticket, whmcs_reply_id="1")
        self.assertEqual(reply.rating, 5)

    @patch("audit.sync.WHMCSClient")
    def test_already_synced_reply_picks_up_rating_on_refetch(self, mock_client_cls):
        # The exact scenario the backfill command exists for: a reply synced
        # before the `rating` field existed (still 0), re-fetched later once
        # the client has since rated it in WHMCS.
        TicketReply.objects.create(
            ticket=self.ticket, whmcs_reply_id="1", author_type=TicketReply.OPERATOR,
            admin_name="Bob", message="fixed it", posted_at=timezone.now(), rating=0,
        )
        mock_client_cls.return_value = mock_client(get_ticket_result={
            "result": "success",
            "replies": {"reply": [{
                "replyid": 1, "date": "2026-01-01 10:00:00", "requestor_type": "Operator",
                "admin": "Bob", "message": "fixed it", "rating": 5,
            }]},
        })

        refresh_ticket_replies(self.project, self.ticket)

        reply = TicketReply.objects.get(ticket=self.ticket, whmcs_reply_id="1")
        self.assertEqual(reply.rating, 5)

    @patch("audit.sync.WHMCSClient")
    def test_failed_get_ticket_returns_false(self, mock_client_cls):
        mock_client_cls.return_value = mock_client(
            get_ticket_result={"result": "error", "message": "Ticket ID Not Found"},
        )

        result = refresh_ticket_replies(self.project, self.ticket)

        self.assertFalse(result)


class EscalationAnalysisHookTests(TestCase):
    """_upsert_replies' escalation-analysis hook -- pure DB state, no mocking needed
    (unlike S3ArchiveHookTests/archive_reply, this hook never leaves Python)."""

    def setUp(self):
        self.project = make_project(escalation_analysis_enabled=True)

    def _ticket(self, whmcs_ticket_id=1, tid="T-1", status="Open"):
        return TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=whmcs_ticket_id, tid=tid, status=status,
            opened_at=timezone.now(),
        )

    def _reply_payload(self, replyid=1):
        return {"reply": [{
            "replyid": replyid, "date": "2026-01-01 10:00:00", "requestor_type": "Operator",
            "name": "Bob", "message": "hi",
        }]}

    def test_new_reply_on_toggled_on_open_ticket_queues_analysis(self):
        ticket = self._ticket()
        _upsert_replies(self.project, ticket, self._reply_payload())

        analysis = TicketEscalationAnalysis.objects.get(ticket=ticket)
        self.assertEqual(analysis.status, TicketEscalationAnalysis.STATUS_QUEUED)

    def test_toggle_off_means_never_queued(self):
        self.project.escalation_analysis_enabled = False
        self.project.save(update_fields=["escalation_analysis_enabled"])
        ticket = self._ticket()

        _upsert_replies(self.project, ticket, self._reply_payload())

        self.assertFalse(TicketEscalationAnalysis.objects.filter(ticket=ticket).exists())

    def test_closed_ticket_is_never_queued(self):
        # Including the specific case of the reply that JUST closed it -- by the
        # time _upsert_replies runs, ticket.status already reflects the fresh
        # sync's value (set by _sync_one_ticket before calling this).
        ticket = self._ticket(status="Closed")
        _upsert_replies(self.project, ticket, self._reply_payload())
        self.assertFalse(TicketEscalationAnalysis.objects.filter(ticket=ticket).exists())

    def test_existing_done_row_flips_back_to_queued_on_a_new_reply(self):
        # The "stays current as the conversation evolves" behavior: a ticket that
        # was already analyzed gets re-queued, not left stale, the next time its
        # thread genuinely changes.
        ticket = self._ticket()
        TicketEscalationAnalysis.objects.create(
            ticket=ticket, status=TicketEscalationAnalysis.STATUS_DONE,
            sentiment_category=TicketEscalationAnalysis.SENTIMENT_HEALTHY, escalation_risk_score=5,
        )

        _upsert_replies(self.project, ticket, self._reply_payload())

        analysis = TicketEscalationAnalysis.objects.get(ticket=ticket)
        self.assertEqual(analysis.status, TicketEscalationAnalysis.STATUS_QUEUED)
        self.assertEqual(TicketEscalationAnalysis.objects.filter(ticket=ticket).count(), 1)

    def test_a_different_toggled_off_projects_ticket_is_untouched(self):
        other_project = make_project("Off Project", escalation_analysis_enabled=False)
        other_ticket = TicketSnapshot.objects.create(
            project=other_project, whmcs_ticket_id=2, tid="T-2", status="Open", opened_at=timezone.now(),
        )

        _upsert_replies(other_project, other_ticket, self._reply_payload())

        self.assertFalse(TicketEscalationAnalysis.objects.filter(ticket=other_ticket).exists())


class SyncTicketsWatermarkTests(TestCase):
    """sync_tickets' incremental watermark (Addendum 10) -- derived from
    MAX(TicketSnapshot.last_reply_at), not a separately-persisted field. Mocks
    WHMCSClient entirely (sync_tickets constructs its own instance internally, same
    as RefreshTicketRepliesTests above), asserting only what iter_tickets was called
    with -- the rest of sync_tickets' per-ticket behavior is already covered
    elsewhere in this file."""

    def setUp(self):
        self.project = make_project()

    @patch("audit.sync.WHMCSClient")
    def test_no_existing_tickets_means_no_watermark(self, mock_client_cls):
        mock_instance = Mock()
        mock_instance.iter_tickets.return_value = iter([])
        mock_client_cls.return_value = mock_instance

        sync_tickets(self.project)

        mock_instance.iter_tickets.assert_called_once_with(stop_at_lastreply=None)

    @patch("audit.sync.WHMCSClient")
    def test_watermark_is_the_latest_stored_last_reply_at_ist_formatted(self, mock_client_cls):
        older = timezone.datetime(2026, 1, 1, 10, 0, 0, tzinfo=dt_timezone.utc)
        newer = timezone.datetime(2026, 1, 5, 10, 0, 0, tzinfo=dt_timezone.utc)
        TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, tid="T-1", status="Open",
            opened_at=older, last_reply_at=older,
        )
        TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=2, tid="T-2", status="Open",
            opened_at=newer, last_reply_at=newer,
        )
        mock_instance = Mock()
        mock_instance.iter_tickets.return_value = iter([])
        mock_client_cls.return_value = mock_instance

        sync_tickets(self.project)

        # 2026-01-05 10:00:00 UTC -> 2026-01-05 15:30:00 IST (UTC+5:30)
        mock_instance.iter_tickets.assert_called_once_with(stop_at_lastreply="2026-01-05 15:30:00")

    @patch("audit.sync.WHMCSClient")
    def test_watermark_ignores_a_different_projects_tickets(self, mock_client_cls):
        other_project = make_project("Other Project")
        TicketSnapshot.objects.create(
            project=other_project, whmcs_ticket_id=1, tid="T-1", status="Open",
            opened_at=timezone.now(), last_reply_at=timezone.now(),
        )
        mock_instance = Mock()
        mock_instance.iter_tickets.return_value = iter([])
        mock_client_cls.return_value = mock_instance

        sync_tickets(self.project)

        mock_instance.iter_tickets.assert_called_once_with(stop_at_lastreply=None)

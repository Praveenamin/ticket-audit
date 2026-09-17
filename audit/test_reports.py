"""Closed-set tests for the aggregate report queries in reports.py."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from .models import Client, ClosedTicketSummary, Project, TicketReply, TicketSnapshot
from .reports import (
    client_monthly_report, closed_tickets_summary, rated_replies_report, tech_monthly_report,
    top_tickets_by_replies,
)


def make_project(name="Test Project", **overrides):
    defaults = {"source_type": Project.SOURCE_DUMP}
    defaults.update(overrides)
    project, _ = Project.objects.get_or_create(name=name, defaults=defaults)
    return project


def make_ticket(project, whmcs_ticket_id, opened_at, **overrides):
    defaults = {
        "project": project, "whmcs_ticket_id": whmcs_ticket_id, "tid": f"T-{whmcs_ticket_id}",
        "subject": "test", "status": "Open", "priority": "Medium", "opened_at": opened_at,
    }
    defaults.update(overrides)
    return TicketSnapshot.objects.create(**defaults)


def make_reply(ticket, author_type, posted_at, reply_id, admin_name="", rating=0):
    return TicketReply.objects.create(
        ticket=ticket, whmcs_reply_id=reply_id, author_name="Someone",
        author_type=author_type, admin_name=admin_name, message="msg", posted_at=posted_at,
        rating=rating,
    )


class ClientMonthlyReportTests(TestCase):
    def setUp(self):
        self.project = make_project("Project A")
        self.other_project = make_project("Project B")

    def test_counts_tickets_opened_in_the_target_month(self):
        june = timezone.datetime(2026, 6, 15, tzinfo=timezone.get_current_timezone())
        make_ticket(self.project, 1, june, requestor_name="Alice")
        make_ticket(self.project, 2, june, requestor_name="Alice")
        make_ticket(self.project, 3, june, requestor_name="Bob")

        rows = client_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [{"label": "Alice", "count": 2}, {"label": "Bob", "count": 1}])

    def test_excludes_tickets_opened_in_a_different_month(self):
        june = timezone.datetime(2026, 6, 15, tzinfo=timezone.get_current_timezone())
        july = timezone.datetime(2026, 7, 1, tzinfo=timezone.get_current_timezone())
        make_ticket(self.project, 1, june, requestor_name="Alice")
        make_ticket(self.project, 2, july, requestor_name="Alice")

        rows = client_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [{"label": "Alice", "count": 1}])

    def test_excludes_tickets_from_a_different_project(self):
        june = timezone.datetime(2026, 6, 15, tzinfo=timezone.get_current_timezone())
        make_ticket(self.project, 1, june, requestor_name="Alice")
        make_ticket(self.other_project, 2, june, requestor_name="Alice")

        rows = client_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [{"label": "Alice", "count": 1}])

    def test_blank_requestor_name_groups_as_unknown(self):
        june = timezone.datetime(2026, 6, 15, tzinfo=timezone.get_current_timezone())
        make_ticket(self.project, 1, june, requestor_name="")
        make_ticket(self.project, 2, june, requestor_name="")

        rows = client_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [{"label": "(unknown)", "count": 2}])

    def test_linked_client_account_name_wins_over_blank_requestor_name(self):
        june = timezone.datetime(2026, 6, 15, tzinfo=timezone.get_current_timezone())
        client = Client.objects.create(
            project=self.project, whmcs_client_id=55, name="Acme Hosting Ltd",
        )
        make_ticket(self.project, 1, june, requestor_name="", client=client)

        rows = client_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [{"label": "Acme Hosting Ltd", "count": 1}])

    def test_linked_client_account_name_wins_over_populated_requestor_name(self):
        """The per-ticket requestor_name is just whatever was typed into that
        one submission -- the linked account is the more reliable identity,
        so it takes priority even when requestor_name also happens to be set."""
        june = timezone.datetime(2026, 6, 15, tzinfo=timezone.get_current_timezone())
        client = Client.objects.create(
            project=self.project, whmcs_client_id=55, name="Acme Hosting Ltd",
        )
        make_ticket(self.project, 1, june, requestor_name="Alice", client=client)

        rows = client_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [{"label": "Acme Hosting Ltd", "count": 1}])


class TechMonthlyReportTests(TestCase):
    def setUp(self):
        self.project = make_project("Project A")
        self.other_project = make_project("Project B")
        self.ticket = make_ticket(self.project, 1, timezone.now())

    def test_counts_distinct_tickets_replied_to_in_the_target_month(self):
        june = timezone.datetime(2026, 6, 10, tzinfo=timezone.get_current_timezone())
        ticket2 = make_ticket(self.project, 2, june)
        make_reply(self.ticket, TicketReply.OPERATOR, june, "1", admin_name="Suriya")
        make_reply(ticket2, TicketReply.OPERATOR, june, "2", admin_name="Suriya")

        rows = tech_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [{"label": "Suriya", "count": 2}])

    def test_multiple_replies_on_the_same_ticket_count_once(self):
        june = timezone.datetime(2026, 6, 10, tzinfo=timezone.get_current_timezone())
        make_reply(self.ticket, TicketReply.OPERATOR, june, "1", admin_name="Suriya")
        make_reply(self.ticket, TicketReply.OPERATOR, june + timedelta(hours=1), "2", admin_name="Suriya")

        rows = tech_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [{"label": "Suriya", "count": 1}])

    def test_excludes_replies_outside_the_target_month(self):
        june = timezone.datetime(2026, 6, 10, tzinfo=timezone.get_current_timezone())
        july = timezone.datetime(2026, 7, 1, tzinfo=timezone.get_current_timezone())
        make_reply(self.ticket, TicketReply.OPERATOR, july, "1", admin_name="Suriya")

        rows = tech_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [])

    def test_excludes_client_side_replies(self):
        june = timezone.datetime(2026, 6, 10, tzinfo=timezone.get_current_timezone())
        make_reply(self.ticket, TicketReply.CONTACT, june, "1", admin_name="")

        rows = tech_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [])

    def test_excludes_replies_from_a_different_project(self):
        june = timezone.datetime(2026, 6, 10, tzinfo=timezone.get_current_timezone())
        other_ticket = make_ticket(self.other_project, 5, june)
        make_reply(other_ticket, TicketReply.OPERATOR, june, "1", admin_name="Suriya")

        rows = tech_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [])

    def test_blank_admin_name_excluded(self):
        june = timezone.datetime(2026, 6, 10, tzinfo=timezone.get_current_timezone())
        make_reply(self.ticket, TicketReply.OPERATOR, june, "1", admin_name="")

        rows = tech_monthly_report(self.project.id, 2026, 6)
        self.assertEqual(rows, [])


class TopTicketsByRepliesTests(TestCase):
    def setUp(self):
        self.project = make_project("Project A")
        self.other_project = make_project("Project B")

    def test_ranks_tickets_by_reply_count_within_the_window(self):
        now = timezone.now()
        busy = make_ticket(self.project, 1, now - timedelta(days=3))
        quiet = make_ticket(self.project, 2, now - timedelta(days=3))
        for i in range(5):
            make_reply(busy, TicketReply.OPERATOR, now - timedelta(days=1), f"b{i}")
        make_reply(quiet, TicketReply.OPERATOR, now - timedelta(days=1), "q0")

        rows = top_tickets_by_replies(self.project.id, days=7)
        self.assertEqual(rows[0]["ticket"].id, busy.id)
        self.assertEqual(rows[0]["reply_count"], 5)
        self.assertEqual(rows[1]["ticket"].id, quiet.id)
        self.assertEqual(rows[1]["reply_count"], 1)

    def test_excludes_replies_older_than_the_window(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=40))
        make_reply(ticket, TicketReply.OPERATOR, now - timedelta(days=40), "old")

        rows = top_tickets_by_replies(self.project.id, days=7)
        self.assertEqual(rows, [])

    def test_excludes_replies_from_a_different_project(self):
        now = timezone.now()
        other_ticket = make_ticket(self.other_project, 9, now)
        make_reply(other_ticket, TicketReply.OPERATOR, now, "1")

        rows = top_tickets_by_replies(self.project.id, days=7)
        self.assertEqual(rows, [])

    def test_respects_limit(self):
        now = timezone.now()
        for i in range(3):
            ticket = make_ticket(self.project, i + 1, now)
            make_reply(ticket, TicketReply.OPERATOR, now, f"r{i}")

        rows = top_tickets_by_replies(self.project.id, days=7, limit=2)
        self.assertEqual(len(rows), 2)


class ClosedTicketsSummaryReportTests(TestCase):
    def setUp(self):
        self.project = make_project("Project A")
        self.other_project = make_project("Project B")

    def test_includes_in_window_closed_ticket_with_no_summary_yet(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=5), status="Closed", closed_at=now - timedelta(days=1))

        rows = closed_tickets_summary(self.project.id, date_from=now - timedelta(days=7))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticket"].id, ticket.id)
        self.assertIsNone(rows[0]["summary"])

    def test_includes_ticket_with_a_drafted_summary(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=5), status="Closed", closed_at=now - timedelta(days=1))
        ClosedTicketSummary.objects.create(
            ticket=ticket, status=ClosedTicketSummary.STATUS_DRAFTED,
            problem_source="Storage", problem_type=ClosedTicketSummary.PROBLEM_TYPE_OPS_ACTION,
        )

        rows = closed_tickets_summary(self.project.id, date_from=now - timedelta(days=7))

        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["summary"])
        self.assertEqual(rows[0]["summary"].problem_source, "Storage")

    def test_excludes_ticket_closed_before_date_from(self):
        now = timezone.now()
        make_ticket(self.project, 1, now - timedelta(days=40), status="Closed", closed_at=now - timedelta(days=40))

        rows = closed_tickets_summary(self.project.id, date_from=now - timedelta(days=7))
        self.assertEqual(rows, [])

    def test_excludes_ticket_closed_after_date_to(self):
        now = timezone.now()
        make_ticket(self.project, 1, now - timedelta(days=1), status="Closed", closed_at=now - timedelta(days=1))

        rows = closed_tickets_summary(self.project.id, date_to=now - timedelta(days=10))
        self.assertEqual(rows, [])

    def test_excludes_non_closed_ticket(self):
        now = timezone.now()
        make_ticket(self.project, 1, now - timedelta(days=1), status="Open", closed_at=None)

        rows = closed_tickets_summary(self.project.id, date_from=now - timedelta(days=7))
        self.assertEqual(rows, [])

    def test_excludes_ticket_from_a_different_project(self):
        now = timezone.now()
        make_ticket(
            self.other_project, 9, now - timedelta(days=1), status="Closed", closed_at=now - timedelta(days=1),
        )

        rows = closed_tickets_summary(self.project.id, date_from=now - timedelta(days=7))
        self.assertEqual(rows, [])

    def test_orders_most_recently_closed_first(self):
        now = timezone.now()
        older = make_ticket(self.project, 1, now - timedelta(days=5), status="Closed", closed_at=now - timedelta(days=3))
        newer = make_ticket(self.project, 2, now - timedelta(days=5), status="Closed", closed_at=now - timedelta(days=1))

        rows = closed_tickets_summary(self.project.id, date_from=now - timedelta(days=7))

        self.assertEqual([row["ticket"].id for row in rows], [newer.id, older.id])

    def test_unbounded_range_includes_a_ticket_a_finite_window_would_exclude(self):
        now = timezone.now()
        ticket = make_ticket(
            self.project, 1, now - timedelta(days=100), status="Closed", closed_at=now - timedelta(days=100),
        )

        self.assertEqual(closed_tickets_summary(self.project.id, date_from=now - timedelta(days=7)), [])
        rows = closed_tickets_summary(self.project.id)
        self.assertEqual([row["ticket"].id for row in rows], [ticket.id])

    def test_client_label_prefers_linked_client_over_requestor_name(self):
        now = timezone.now()
        client = Client.objects.create(project=self.project, whmcs_client_id=55, name="Acme Hosting Ltd")
        make_ticket(
            self.project, 1, now - timedelta(days=5), status="Closed", closed_at=now - timedelta(days=1),
            requestor_name="Alice", client=client,
        )

        rows = closed_tickets_summary(self.project.id, date_from=now - timedelta(days=7))
        self.assertEqual(rows[0]["client_label"], "Acme Hosting Ltd")


class ClosedTicketsSummaryStatusFilterTests(TestCase):
    """The report's 4-bucket status filter: not_queued / queued /
    processing / completed. "completed" deliberately covers drafted,
    needs_review, AND failed -- all three mean the pipeline finished
    running, just with different outcomes; a user thinking "is this done"
    shouldn't need to know those three exist separately."""

    def setUp(self):
        self.project = make_project()
        self.now = timezone.now()

    def _ticket(self, whmcs_ticket_id, summary_status=None, **summary_kwargs):
        ticket = make_ticket(
            self.project, whmcs_ticket_id, self.now - timedelta(days=5),
            status="Closed", closed_at=self.now - timedelta(days=1),
        )
        if summary_status is not None:
            ClosedTicketSummary.objects.create(ticket=ticket, status=summary_status, **summary_kwargs)
        return ticket

    def test_not_queued_includes_only_tickets_with_no_summary(self):
        no_summary = self._ticket(1)
        self._ticket(2, ClosedTicketSummary.STATUS_QUEUED)

        rows = closed_tickets_summary(self.project.id, status_filter="not_queued")

        self.assertEqual([row["ticket"].id for row in rows], [no_summary.id])

    def test_queued_includes_only_queued_status(self):
        queued = self._ticket(1, ClosedTicketSummary.STATUS_QUEUED)
        self._ticket(2, ClosedTicketSummary.STATUS_PROCESSING)
        self._ticket(3)

        rows = closed_tickets_summary(self.project.id, status_filter="queued")

        self.assertEqual([row["ticket"].id for row in rows], [queued.id])

    def test_processing_includes_only_processing_status(self):
        processing = self._ticket(1, ClosedTicketSummary.STATUS_PROCESSING)
        self._ticket(2, ClosedTicketSummary.STATUS_QUEUED)

        rows = closed_tickets_summary(self.project.id, status_filter="processing")

        self.assertEqual([row["ticket"].id for row in rows], [processing.id])

    def test_completed_includes_drafted_needs_review_and_failed(self):
        drafted = self._ticket(1, ClosedTicketSummary.STATUS_DRAFTED)
        needs_review = self._ticket(2, ClosedTicketSummary.STATUS_NEEDS_REVIEW)
        failed = self._ticket(3, ClosedTicketSummary.STATUS_FAILED)
        self._ticket(4, ClosedTicketSummary.STATUS_QUEUED)
        self._ticket(5)

        rows = closed_tickets_summary(self.project.id, status_filter="completed")

        self.assertEqual(
            {row["ticket"].id for row in rows}, {drafted.id, needs_review.id, failed.id},
        )

    def test_no_filter_includes_every_status(self):
        self._ticket(1, ClosedTicketSummary.STATUS_QUEUED)
        self._ticket(2, ClosedTicketSummary.STATUS_DRAFTED)
        self._ticket(3)

        rows = closed_tickets_summary(self.project.id)

        self.assertEqual(len(rows), 3)


class RatedRepliesReportTests(TestCase):
    def setUp(self):
        self.project = make_project("Project A")
        self.other_project = make_project("Project B")

    def test_excludes_unrated_replies(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=1))
        make_reply(ticket, TicketReply.OPERATOR, now, "r1", admin_name="Bob", rating=0)

        self.assertEqual(rated_replies_report(self.project.id), [])

    def test_includes_replies_rated_one_through_five(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=1))
        for i, stars in enumerate([1, 2, 3, 4, 5], start=1):
            make_reply(ticket, TicketReply.OPERATOR, now, f"r{i}", admin_name="Bob", rating=stars)

        rows = rated_replies_report(self.project.id)

        self.assertEqual(sorted(row["rating"] for row in rows), [1, 2, 3, 4, 5])

    def test_excludes_reply_posted_before_date_from(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=40))
        make_reply(ticket, TicketReply.OPERATOR, now - timedelta(days=40), "r1", admin_name="Bob", rating=5)

        rows = rated_replies_report(self.project.id, date_from=now - timedelta(days=7))
        self.assertEqual(rows, [])

    def test_excludes_reply_posted_after_date_to(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=1))
        make_reply(ticket, TicketReply.OPERATOR, now, "r1", admin_name="Bob", rating=5)

        rows = rated_replies_report(self.project.id, date_to=now - timedelta(days=10))
        self.assertEqual(rows, [])

    def test_excludes_reply_from_a_different_project(self):
        now = timezone.now()
        ticket = make_ticket(self.other_project, 9, now - timedelta(days=1))
        make_reply(ticket, TicketReply.OPERATOR, now, "r1", admin_name="Bob", rating=5)

        rows = rated_replies_report(self.project.id, date_from=now - timedelta(days=7))
        self.assertEqual(rows, [])

    def test_tech_is_the_rated_replys_own_admin_name(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=1))
        make_reply(ticket, TicketReply.OPERATOR, now, "r1", admin_name="Midhun Jose", rating=4)

        rows = rated_replies_report(self.project.id)
        self.assertEqual(rows[0]["tech"], "Midhun Jose")

    def test_blank_admin_name_falls_back_to_unknown(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=1))
        make_reply(ticket, TicketReply.OPERATOR, now, "r1", admin_name="", rating=3)

        rows = rated_replies_report(self.project.id)
        self.assertEqual(rows[0]["tech"], "(unknown)")

    def test_client_label_prefers_linked_client_over_requestor_name(self):
        now = timezone.now()
        client = Client.objects.create(project=self.project, whmcs_client_id=55, name="Acme Hosting Ltd")
        ticket = make_ticket(self.project, 1, now - timedelta(days=1), requestor_name="Alice", client=client)
        make_reply(ticket, TicketReply.OPERATOR, now, "r1", admin_name="Bob", rating=5)

        rows = rated_replies_report(self.project.id)
        self.assertEqual(rows[0]["client_label"], "Acme Hosting Ltd")

    def test_unbounded_range_includes_a_reply_a_finite_window_would_exclude(self):
        now = timezone.now()
        ticket = make_ticket(self.project, 1, now - timedelta(days=100))
        reply = make_reply(ticket, TicketReply.OPERATOR, now - timedelta(days=100), "r1", admin_name="Bob", rating=5)

        self.assertEqual(rated_replies_report(self.project.id, date_from=now - timedelta(days=7)), [])
        rows = rated_replies_report(self.project.id)
        self.assertEqual([row["ticket"].id for row in rows], [ticket.id])
        self.assertEqual(rows[0]["rating"], reply.rating)

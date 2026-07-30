"""Closed-set boundary tests for SLA policy resolution + per-exchange breach
evaluation.

apply_sla_evaluation / evaluate_reply_turnaround accept an explicit `now` so
these are deterministic without needing a time-freezing dependency.
"""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from .models import Department, Project, SLAPolicy, TicketReply, TicketSnapshot
from .sla import apply_sla_evaluation, evaluate_reply_turnaround, explain_sla, resolve_policy


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
        "subject": "test",
        "status": "Open",
        "priority": "Medium",
        "opened_at": timezone.now() - timedelta(hours=1),
    }
    defaults.update(overrides)
    ticket = TicketSnapshot.objects.create(**defaults)
    return ticket


def make_reply(ticket, author_type, posted_at, reply_id, message="msg"):
    return TicketReply.objects.create(
        ticket=ticket,
        whmcs_reply_id=reply_id,
        author_name="Someone",
        author_type=author_type,
        admin_name="Agent" if author_type == TicketReply.OPERATOR else "",
        message=message,
        posted_at=posted_at,
    )


class PolicyPrecedenceTests(TestCase):
    """resolve_policy: department-specific beats the global (department-null)
    default, scoped per project."""

    def setUp(self):
        self.project = make_project("Project A")
        self.other_project = make_project("Project B")
        self.cpanel = Department.objects.create(project=self.project, whmcs_deptid=1, name="cPanel")
        self.linux = Department.objects.create(project=self.project, whmcs_deptid=2, name="Linux")

    def test_no_policies_returns_none(self):
        self.assertIsNone(resolve_policy(self.project.id, self.cpanel.id))

    def test_global_default_used_when_nothing_more_specific(self):
        global_policy = SLAPolicy.objects.create(
            project=self.project, name="Global",
            first_response_target_minutes=60, resolution_target_minutes=1440,
        )
        self.assertEqual(resolve_policy(self.project.id, self.cpanel.id), global_policy)

    def test_department_specific_beats_global_default(self):
        SLAPolicy.objects.create(
            project=self.project, name="Global",
            first_response_target_minutes=60, resolution_target_minutes=1440,
        )
        dept_only = SLAPolicy.objects.create(
            project=self.project, name="cPanel dept", department=self.cpanel,
            first_response_target_minutes=20, resolution_target_minutes=480,
        )
        self.assertEqual(resolve_policy(self.project.id, self.cpanel.id), dept_only)

    def test_wrong_department_does_not_match(self):
        SLAPolicy.objects.create(
            project=self.project, name="cPanel dept", department=self.cpanel,
            first_response_target_minutes=20, resolution_target_minutes=480,
        )
        self.assertIsNone(resolve_policy(self.project.id, self.linux.id))

    def test_tie_broken_by_most_recently_created(self):
        older = SLAPolicy.objects.create(
            project=self.project, name="Global A",
            first_response_target_minutes=60, resolution_target_minutes=1440,
        )
        newer = SLAPolicy.objects.create(
            project=self.project, name="Global B",
            first_response_target_minutes=45, resolution_target_minutes=900,
        )
        self.assertEqual(resolve_policy(self.project.id, self.cpanel.id), newer)
        self.assertNotEqual(resolve_policy(self.project.id, self.cpanel.id), older)

    def test_global_policy_in_one_project_does_not_leak_into_another(self):
        SLAPolicy.objects.create(
            project=self.project, name="Global (Project A)",
            first_response_target_minutes=60, resolution_target_minutes=1440,
        )
        other_dept = Department.objects.create(
            project=self.other_project, whmcs_deptid=1, name="Support",
        )
        self.assertIsNone(resolve_policy(self.other_project.id, other_dept.id))

    def test_same_whmcs_deptid_in_different_projects_does_not_collide(self):
        # cpanel already has whmcs_deptid=1 under self.project.
        other_dept = Department.objects.create(
            project=self.other_project, whmcs_deptid=1, name="Same ID, different project",
        )
        self.assertNotEqual(other_dept.project_id, self.cpanel.project_id)
        self.assertEqual(other_dept.whmcs_deptid, self.cpanel.whmcs_deptid)


class SLAEvaluationTests(TestCase):
    """First-response logic is unchanged from Phase 1 -- these guard against
    regressions while the follow-up (resolution) side is reworked."""

    def setUp(self):
        self.policy = SLAPolicy.objects.create(
            project=make_project(),
            name="Standard",
            first_response_target_minutes=60,
            resolution_target_minutes=240,
        )

    def test_no_matching_policy_leaves_sla_fields_null(self):
        self.policy.delete()
        ticket = make_ticket()
        apply_sla_evaluation(ticket)
        self.assertIsNone(ticket.sla_policy)
        self.assertIsNone(ticket.sla_status)

    def test_no_status_label_when_comfortably_within_window(self):
        opened = timezone.now() - timedelta(minutes=10)
        ticket = make_ticket(opened_at=opened)
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=10))
        self.assertIsNone(ticket.first_response_met)
        self.assertIsNone(ticket.follow_up_met)
        self.assertIsNone(ticket.sla_status)

    def test_first_response_met_exactly_at_target_boundary(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=60))
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=60))
        self.assertTrue(ticket.first_response_met)

    def test_first_response_breached_one_minute_past_target(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=61))
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=61))
        self.assertFalse(ticket.first_response_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_BREACHED)

    def test_first_response_pending_when_not_yet_due_and_no_reply_yet(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=30))
        self.assertIsNone(ticket.first_response_met)

    def test_first_response_breached_when_due_passed_with_no_reply(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=61))
        self.assertFalse(ticket.first_response_met)

    def test_breached_overall_when_first_response_missed_even_if_followup_fine(self):
        """Late first response still marks the ticket Breached overall, even
        though the one follow-up exchange after it was handled promptly."""
        opened = timezone.now()
        ticket = make_ticket(
            opened_at=opened,
            first_response_at=opened + timedelta(minutes=90),  # missed 60-min target
            status="Closed",
            closed_at=opened + timedelta(minutes=200),
        )
        make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=100), "r1")
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=150), "r2")
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=200))
        self.assertFalse(ticket.first_response_met)
        self.assertTrue(ticket.follow_up_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_BREACHED)

    def test_met_when_first_response_and_every_followup_are_on_time(self):
        opened = timezone.now()
        ticket = make_ticket(
            opened_at=opened,
            first_response_at=opened + timedelta(minutes=10),
            status="Closed",
            closed_at=opened + timedelta(minutes=200),
        )
        make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=20), "r1")
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=100), "r2")
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=200))
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_MET)

    def test_closed_with_zero_replies_before_first_response_due_is_not_met(self):
        """Confirmed bug fix: a ticket closed within seconds of opening, with
        NO operator reply ever, must not show Met just because it technically
        closed before the first-response deadline passed."""
        opened = timezone.now()
        ticket = make_ticket(
            opened_at=opened, status="Closed", closed_at=opened + timedelta(minutes=2),
        )
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=2))
        self.assertIsNone(ticket.first_response_met)
        self.assertIsNone(ticket.sla_status)

    def test_on_hold_suppresses_sla_status_but_not_underlying_fields(self):
        opened = timezone.now()
        ticket = make_ticket(
            opened_at=opened, status=TicketSnapshot.ON_HOLD_STATUS,
            first_response_at=opened + timedelta(minutes=90),  # missed 60-min target
        )
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=90))
        self.assertIsNone(ticket.sla_status)
        self.assertFalse(ticket.first_response_met)
        explanation = explain_sla(ticket, replies=[], now=opened + timedelta(minutes=90))
        self.assertTrue(any("On Hold" in line for line in explanation))
        self.assertTrue(any("Initial response missed" in line for line in explanation))


class ReplyTurnaroundTests(TestCase):
    """Unit tests against evaluate_reply_turnaround directly -- a target of
    240 minutes (4h) throughout, matching the real policy Praveen described."""

    TARGET = 240

    def test_no_followup_turns_yet_is_not_applicable(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=10))
        ok, details = evaluate_reply_turnaround(
            ticket, [], self.TARGET, now=opened + timedelta(minutes=20)
        )
        self.assertIsNone(ok)
        self.assertEqual(details, [])

    def test_followup_met_exactly_at_target_boundary(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=10))
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=30), "r1")
        operator_msg = make_reply(
            ticket, TicketReply.OPERATOR,
            client_msg.posted_at + timedelta(minutes=self.TARGET), "r2",
        )
        ok, details = evaluate_reply_turnaround(
            ticket, [client_msg, operator_msg], self.TARGET, now=operator_msg.posted_at,
        )
        self.assertTrue(ok)
        self.assertEqual(details, [])

    def test_followup_breached_one_minute_past_target(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=10))
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=30), "r1")
        operator_msg = make_reply(
            ticket, TicketReply.OPERATOR,
            client_msg.posted_at + timedelta(minutes=self.TARGET + 1), "r2",
        )
        ok, details = evaluate_reply_turnaround(
            ticket, [client_msg, operator_msg], self.TARGET, now=operator_msg.posted_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(details), 1)
        self.assertIn("r1", details[0])

    def test_followup_pending_within_window_still_open(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=10))
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=30), "r1")
        ok, details = evaluate_reply_turnaround(
            ticket, [client_msg], self.TARGET,
            now=client_msg.posted_at + timedelta(minutes=self.TARGET - 1),
        )
        self.assertIsNone(ok)
        self.assertEqual(details, [])

    def test_followup_breached_overdue_no_reply_still_open(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=10))
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=30), "r1")
        ok, details = evaluate_reply_turnaround(
            ticket, [client_msg], self.TARGET,
            now=client_msg.posted_at + timedelta(minutes=self.TARGET + 1),
        )
        self.assertFalse(ok)
        self.assertEqual(len(details), 1)
        self.assertIn("no operator reply", details[0])

    def test_consecutive_client_messages_anchor_on_last_message(self):
        """Client sends two messages before any reply -- the clock anchors on
        the LAST one, not the first, matching "within 4h of last reply"."""
        opened = timezone.now()
        first_response_at = opened + timedelta(minutes=10)
        ticket = make_ticket(opened_at=opened, first_response_at=first_response_at)
        msg_a = make_reply(ticket, TicketReply.CONTACT, first_response_at + timedelta(minutes=10), "a")
        msg_b = make_reply(ticket, TicketReply.CONTACT, msg_a.posted_at + timedelta(minutes=30), "b")
        # 265 min after A (would breach if anchored on A), 235 min after B (within target).
        operator_msg = make_reply(
            ticket, TicketReply.OPERATOR, msg_b.posted_at + timedelta(minutes=235), "op",
        )
        ok, details = evaluate_reply_turnaround(
            ticket, [msg_a, msg_b, operator_msg], self.TARGET, now=operator_msg.posted_at,
        )
        self.assertTrue(ok)
        self.assertEqual(details, [])

    def test_consecutive_operator_replies_dont_create_extra_windows_or_fix_a_breach(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=10))
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=30), "r1")
        late_reply = make_reply(
            ticket, TicketReply.OPERATOR,
            client_msg.posted_at + timedelta(minutes=300), "op1",
        )
        immediate_followup = make_reply(
            ticket, TicketReply.OPERATOR, late_reply.posted_at + timedelta(minutes=1), "op2",
        )
        ok, details = evaluate_reply_turnaround(
            ticket, [client_msg, late_reply, immediate_followup], self.TARGET,
            now=immediate_followup.posted_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(details), 1)
        self.assertIn("r1", details[0])

    def test_multiple_followups_only_the_late_one_is_named(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=10))
        c1 = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=20), "c1")
        o1 = make_reply(ticket, TicketReply.OPERATOR, c1.posted_at + timedelta(minutes=30), "o1")
        c2 = make_reply(ticket, TicketReply.CONTACT, o1.posted_at + timedelta(minutes=20), "c2")
        o2 = make_reply(ticket, TicketReply.OPERATOR, c2.posted_at + timedelta(minutes=300), "o2")  # late
        c3 = make_reply(ticket, TicketReply.CONTACT, o2.posted_at + timedelta(minutes=20), "c3")
        o3 = make_reply(ticket, TicketReply.OPERATOR, c3.posted_at + timedelta(minutes=30), "o3")
        ok, details = evaluate_reply_turnaround(
            ticket, [c1, o1, c2, o2, c3, o3], self.TARGET, now=o3.posted_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(details), 1)
        self.assertIn("c2", details[0])

    def test_closed_ticket_freezes_at_closed_at_not_live_now(self):
        opened = timezone.now()
        ticket = make_ticket(
            opened_at=opened, first_response_at=opened + timedelta(minutes=10), status="Closed",
        )
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=30), "r1")
        ticket.closed_at = client_msg.posted_at + timedelta(minutes=100)  # within target
        ok, details = evaluate_reply_turnaround(
            ticket, [client_msg], self.TARGET,
            now=client_msg.posted_at + timedelta(days=10),  # would look breached if not frozen
        )
        self.assertTrue(ok)
        self.assertEqual(details, [])

    def test_closed_ticket_with_unanswered_message_past_target_before_closure_is_breach(self):
        opened = timezone.now()
        ticket = make_ticket(
            opened_at=opened, first_response_at=opened + timedelta(minutes=10), status="Closed",
        )
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=30), "r1")
        ticket.closed_at = client_msg.posted_at + timedelta(minutes=self.TARGET + 60)
        ok, details = evaluate_reply_turnaround(
            ticket, [client_msg], self.TARGET, now=ticket.closed_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(details), 1)
        self.assertIn("never", details[0])

    def test_unrecognized_author_type_ignored_in_grouping(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, first_response_at=opened + timedelta(minutes=10))
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=30), "r1")
        unknown = make_reply(ticket, "", client_msg.posted_at + timedelta(minutes=5), "r-bot")
        operator_msg = make_reply(
            ticket, TicketReply.OPERATOR, unknown.posted_at + timedelta(minutes=20), "r2",
        )
        ok, details = evaluate_reply_turnaround(
            ticket, [client_msg, unknown, operator_msg], self.TARGET, now=operator_msg.posted_at,
        )
        self.assertTrue(ok)
        self.assertEqual(details, [])

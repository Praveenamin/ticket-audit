"""Closed-set boundary tests for SLA policy resolution + per-turn breach
evaluation.

apply_sla_evaluation / _response_breach_events / _resolution_breach_events
accept an explicit `now` so these are deterministic without needing a
time-freezing dependency.
"""

from datetime import timedelta

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .models import Department, Project, SLAPolicy, TicketReply, TicketSnapshot
from .sla import (
    _resolution_breach_events, _response_breach_events, _side,
    apply_sla_evaluation, breach_summary, explain_sla, resolve_policy,
)


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


def make_opening_reply(ticket, when=None, message="Hello"):
    return make_reply(ticket, TicketReply.OWNER, when or ticket.opened_at, "0", message=message)


class SideTests(SimpleTestCase):
    """_side(): "Operator" is the one and only staff-side value -- every
    other non-blank value is client, whitelist or not. Closed-set boundary
    per real production author_type values (see the DB-wide distribution
    check that motivated this fix: Owner/Operator/Contact/Authorized User/
    Guest/Registered User/Sub-account)."""

    def test_operator_is_operator_side(self):
        self.assertEqual(_side(TicketReply.OPERATOR), "operator")

    def test_owner_is_client_side(self):
        self.assertEqual(_side(TicketReply.OWNER), "client")

    def test_contact_is_client_side(self):
        self.assertEqual(_side(TicketReply.CONTACT), "client")

    def test_authorized_user_is_client_side(self):
        # Regression: this exact value was missing from the old Owner/Contact
        # whitelist, causing ticket NKW-994803's real false-breach bug.
        self.assertEqual(_side("Authorized User"), "client")

    def test_any_other_non_blank_unknown_value_is_client_side(self):
        # Proves the fix is a genuine "operator vs everything else" rule, not
        # just a longer hardcoded whitelist -- a role WHMCS hasn't even used
        # yet must still resolve to client, not silently vanish again.
        self.assertEqual(_side("Some Brand New WHMCS Role"), "client")

    def test_blank_string_is_unrecognized(self):
        self.assertIsNone(_side(""))

    def test_none_is_unrecognized(self):
        self.assertIsNone(_side(None))


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


class ShouldBumpClosedAtTests(SimpleTestCase):
    """TicketSnapshot.should_bump_closed_at: closed_at must update on a
    genuine re-closure (new activity since the last one recorded), not stay
    frozen at the first closure forever -- otherwise a reopened-then-reclosed
    ticket's later exchanges silently vanish from the breach walk."""

    def test_none_current_is_always_a_bump(self):
        now = timezone.now()
        self.assertTrue(TicketSnapshot.should_bump_closed_at(None, now))

    def test_none_current_and_no_last_reply_is_still_a_bump(self):
        self.assertTrue(TicketSnapshot.should_bump_closed_at(None, None))

    def test_later_last_reply_is_a_bump(self):
        base = timezone.now()
        self.assertTrue(TicketSnapshot.should_bump_closed_at(base, base + timedelta(minutes=1)))

    def test_same_or_earlier_last_reply_is_not_a_bump(self):
        base = timezone.now()
        self.assertFalse(TicketSnapshot.should_bump_closed_at(base, base))
        self.assertFalse(TicketSnapshot.should_bump_closed_at(base, base - timedelta(minutes=1)))

    def test_no_new_last_reply_with_existing_closed_at_is_not_a_bump(self):
        base = timezone.now()
        self.assertFalse(TicketSnapshot.should_bump_closed_at(base, None))


class SLAEvaluationTests(TestCase):
    """apply_sla_evaluation: end-to-end boundary tests against real
    TicketReply rows -- both checks are derived purely from the reply
    thread now, not from a stored first_response_at/due_at pair. Every
    client turn (the original open AND every later reopen/Customer-Reply)
    gets the fast response target; every operator turn's ack-to-solution
    gap gets the resolution target."""

    def setUp(self):
        self.policy = SLAPolicy.objects.create(
            project=make_project(), name="Standard",
            first_response_target_minutes=60, resolution_target_minutes=240,
        )

    def test_no_matching_policy_leaves_sla_fields_null(self):
        self.policy.delete()
        ticket = make_ticket()
        apply_sla_evaluation(ticket)
        self.assertIsNone(ticket.sla_policy)
        self.assertIsNone(ticket.sla_status)

    def test_shows_met_when_still_open_and_comfortably_within_window(self):
        """Binary by design: a still-open ticket that hasn't missed anything
        YET shows Met (currently meeting SLA), not a third "pending" state --
        the per-check fields stay None (genuinely not yet due), but the
        overall verdict must be one of exactly two values."""
        opened = timezone.now() - timedelta(minutes=10)
        ticket = make_ticket(opened_at=opened)
        make_opening_reply(ticket, opened)
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=10))
        self.assertIsNone(ticket.first_response_met)
        self.assertIsNone(ticket.follow_up_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_MET)

    def test_first_response_met_exactly_at_target_boundary(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=60), "1")
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=60))
        self.assertTrue(ticket.first_response_met)

    def test_first_response_breached_one_minute_past_target(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=61), "1")
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=61))
        self.assertFalse(ticket.first_response_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_BREACHED)

    def test_first_response_pending_when_not_yet_due_and_no_reply_yet(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        make_opening_reply(ticket, opened)
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=30))
        self.assertIsNone(ticket.first_response_met)

    def test_first_response_breached_when_due_passed_with_no_reply(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        make_opening_reply(ticket, opened)
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=61))
        self.assertFalse(ticket.first_response_met)

    def test_every_reopen_gets_its_own_fast_response_check(self):
        """A client turn well into the thread is checked against the SAME
        fast target as the original open -- not a slower target -- matching
        WHMCS's automatic Open/Customer-Reply status (never admin-
        discretionary, unlike In Progress/Answered)."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "1")  # turn 1 OK
        reopen_at = opened + timedelta(minutes=1000)
        make_reply(ticket, TicketReply.CONTACT, reopen_at, "2")  # reopen (Customer-Reply)
        # 61 min later -- breaches the SAME 60-min target as a fresh ticket would.
        make_reply(ticket, TicketReply.OPERATOR, reopen_at + timedelta(minutes=61), "3")
        ticket.closed_at = reopen_at + timedelta(minutes=61)
        apply_sla_evaluation(ticket, now=ticket.closed_at)
        self.assertFalse(ticket.first_response_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_BREACHED)

    def test_breached_overall_when_first_response_missed_even_if_resolution_fine(self):
        """Late first response still marks the ticket Breached overall, even
        though the resolution side (ack-to-solution gaps) was handled
        promptly."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=90), "1")  # missed 60-min target
        make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=100), "2")
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=120), "3")  # 20 min later, fine
        ticket.closed_at = opened + timedelta(minutes=150)
        apply_sla_evaluation(ticket, now=ticket.closed_at)
        self.assertFalse(ticket.first_response_met)
        self.assertTrue(ticket.follow_up_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_BREACHED)

    def test_met_when_response_and_resolution_are_both_on_time(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "1")  # turn 1 OK
        make_reply(ticket, TicketReply.CONTACT, opened + timedelta(minutes=20), "2")  # reopen
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=40), "3")  # turn 2 OK (20 min)
        ticket.closed_at = opened + timedelta(minutes=200)
        apply_sla_evaluation(ticket, now=ticket.closed_at)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_MET)

    def test_resolution_breach_when_ack_to_solution_gap_exceeds_target(self):
        """Tech acks (In Progress) then takes too long to send the actual
        solution, with no client message in between -- a distinct breach
        from the (fast, on-time) initial response."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "1")  # ack, on time
        # solution arrives 260 min after the ack -- breaches the 240-min resolution target.
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=270), "2")
        ticket.closed_at = opened + timedelta(minutes=270)
        apply_sla_evaluation(ticket, now=ticket.closed_at)
        self.assertTrue(ticket.first_response_met)
        self.assertFalse(ticket.follow_up_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_BREACHED)

    def test_scheduled_ack_exempts_a_long_ack_to_solution_gap(self):
        """Real ticket XYC-778415: client asked for an 11pm-scheduled action;
        operator's ack said "we will proceed with your request at the
        scheduled time"; completion landed 13h19m later -- far past the
        240-min target, but a client-requested delay, not a slow response."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        make_opening_reply(ticket, opened, message="Please upgrade at 11 PM today.")
        make_reply(
            ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "1",
            message="We will proceed with your request at the scheduled time.",
        )
        make_reply(
            ticket, TicketReply.OPERATOR, opened + timedelta(minutes=800), "2",
            message="As requested, we have completed the update.",
        )
        ticket.closed_at = opened + timedelta(minutes=800)
        apply_sla_evaluation(ticket, now=ticket.closed_at)
        self.assertTrue(ticket.follow_up_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_MET)

    def test_scheduled_ack_exempts_a_still_open_trailing_turn_too(self):
        """Same exemption applies before the completion reply even arrives --
        a scheduled ack shouldn't tick against the live clock either."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="In Progress")
        make_opening_reply(ticket, opened)
        make_reply(
            ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "1",
            message="We will proceed with your request at the scheduled time.",
        )
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=800))
        self.assertTrue(ticket.follow_up_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_MET)

    def test_resolution_instantly_met_when_first_reply_is_already_the_solution(self):
        """Confirmed edge case: if the tech's very first reply is already the
        full solution (no separate ack step), resolution is trivially met."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "1")
        ticket.closed_at = opened + timedelta(minutes=10)
        apply_sla_evaluation(ticket, now=ticket.closed_at)
        self.assertTrue(ticket.follow_up_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_MET)

    def test_closed_with_zero_replies_before_first_response_due_is_breached(self):
        """Confirmed bug fix (now stricter, matching the binary Met/Breached
        rule): a ticket closed within seconds of opening, with NO reply ever
        recorded (not even the opening message), must not show Met just
        because it technically closed before any deadline passed -- there's
        no evidence either check was actually satisfied, so it's Breached,
        not a third "pending" state."""
        opened = timezone.now()
        ticket = make_ticket(
            opened_at=opened, status="Closed", closed_at=opened + timedelta(minutes=2),
        )
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=2))
        self.assertIsNone(ticket.first_response_met)
        self.assertEqual(ticket.sla_status, TicketSnapshot.SLA_BREACHED)

    def test_on_hold_suppresses_sla_status_but_not_underlying_fields(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status=TicketSnapshot.ON_HOLD_STATUS)
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=90), "1")  # missed 60-min target
        apply_sla_evaluation(ticket, now=opened + timedelta(minutes=90))
        self.assertIsNone(ticket.sla_status)
        self.assertFalse(ticket.first_response_met)
        explanation = explain_sla(ticket, now=opened + timedelta(minutes=90))
        self.assertTrue(any("On Hold" in line for line in explanation))
        self.assertTrue(any("to get an operator reply" in line for line in explanation))


class ResponseBreachEventsTests(TestCase):
    """_response_breach_events: a target of 30 minutes throughout -- EVERY
    client turn (including the ticket's opening message) is checked against
    this same fast target, since WHMCS sets Open/Customer-Reply
    automatically with no admin discretion involved (unlike In
    Progress/Answered)."""

    TARGET = 30

    def test_no_client_turns_at_all_is_not_applicable(self):
        ticket = make_ticket()
        ok, events = _response_breach_events(ticket, [], self.TARGET, now=timezone.now())
        self.assertIsNone(ok)
        self.assertEqual(events, [])

    def test_met_exactly_at_target_boundary(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened, "r1")
        operator_msg = make_reply(
            ticket, TicketReply.OPERATOR, opened + timedelta(minutes=self.TARGET), "r2",
        )
        ok, events = _response_breach_events(
            ticket, [client_msg, operator_msg], self.TARGET, now=operator_msg.posted_at,
        )
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_breached_one_minute_past_target(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened, "r1")
        operator_msg = make_reply(
            ticket, TicketReply.OPERATOR, opened + timedelta(minutes=self.TARGET + 1), "r2",
        )
        ok, events = _response_breach_events(
            ticket, [client_msg, operator_msg], self.TARGET, now=operator_msg.posted_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reply"].whmcs_reply_id, "r1")
        self.assertFalse(events[0]["never_answered"])

    def test_pending_within_window_still_open(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened, "r1")
        ok, events = _response_breach_events(
            ticket, [client_msg], self.TARGET, now=opened + timedelta(minutes=self.TARGET - 1),
        )
        self.assertIsNone(ok)
        self.assertEqual(events, [])

    def test_breached_overdue_no_reply_still_open(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened, "r1")
        ok, events = _response_breach_events(
            ticket, [client_msg], self.TARGET, now=opened + timedelta(minutes=self.TARGET + 1),
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["never_answered"])
        self.assertFalse(events[0]["closed"])

    def test_consecutive_client_messages_anchor_on_last_message(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        msg_a = make_reply(ticket, TicketReply.CONTACT, opened, "a")
        msg_b = make_reply(ticket, TicketReply.CONTACT, msg_a.posted_at + timedelta(minutes=10), "b")
        # 40 min after A (would breach if anchored on A), exactly at target after B.
        operator_msg = make_reply(
            ticket, TicketReply.OPERATOR, msg_b.posted_at + timedelta(minutes=self.TARGET), "op",
        )
        ok, events = _response_breach_events(
            ticket, [msg_a, msg_b, operator_msg], self.TARGET, now=operator_msg.posted_at,
        )
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_consecutive_operator_replies_dont_create_extra_windows_or_fix_a_breach(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened, "r1")
        late_reply = make_reply(
            ticket, TicketReply.OPERATOR,
            client_msg.posted_at + timedelta(minutes=self.TARGET + 30), "op1",
        )
        immediate_followup = make_reply(
            ticket, TicketReply.OPERATOR, late_reply.posted_at + timedelta(minutes=1), "op2",
        )
        ok, events = _response_breach_events(
            ticket, [client_msg, late_reply, immediate_followup], self.TARGET,
            now=immediate_followup.posted_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reply"].whmcs_reply_id, "r1")

    def test_multiple_client_turns_only_the_late_one_breaches(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        o1 = make_reply(ticket, TicketReply.OPERATOR, c1.posted_at + timedelta(minutes=10), "o1")
        c2 = make_reply(ticket, TicketReply.CONTACT, o1.posted_at + timedelta(minutes=20), "c2")
        o2 = make_reply(
            ticket, TicketReply.OPERATOR, c2.posted_at + timedelta(minutes=self.TARGET + 10), "o2",
        )  # late
        c3 = make_reply(ticket, TicketReply.CONTACT, o2.posted_at + timedelta(minutes=20), "c3")
        o3 = make_reply(ticket, TicketReply.OPERATOR, c3.posted_at + timedelta(minutes=10), "o3")
        ok, events = _response_breach_events(
            ticket, [c1, o1, c2, o2, c3, o3], self.TARGET, now=o3.posted_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reply"].whmcs_reply_id, "c2")

    def test_closed_ticket_freezes_at_closed_at_not_live_now(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened, "r1")
        ticket.closed_at = client_msg.posted_at + timedelta(minutes=self.TARGET - 5)  # within target
        ok, events = _response_breach_events(
            ticket, [client_msg], self.TARGET,
            now=client_msg.posted_at + timedelta(days=10),  # would breach if not frozen
        )
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_closed_ticket_with_unanswered_message_past_target_before_closure_is_breach(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened, "r1")
        ticket.closed_at = client_msg.posted_at + timedelta(minutes=self.TARGET + 60)
        ok, events = _response_breach_events(
            ticket, [client_msg], self.TARGET, now=ticket.closed_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertTrue(events[0]["never_answered"])
        self.assertTrue(events[0]["closed"])

    def test_never_answered_burst_anchors_on_its_first_message_not_its_last(self):
        """Confirmed against real production data: a monitoring-alert burst
        of thousands of messages over 2+ days, never once replied to, closed
        in the same instant as its own last message -- anchoring on the last
        message (as the "got answered" branch correctly does) would show
        this as instantly met, hiding a multi-day unanswered ticket. The
        clock has to start at the burst's FIRST message instead."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        first_alert = make_reply(ticket, TicketReply.CONTACT, opened, "a1")
        mid_alert = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(hours=1), "a2")
        last_alert = make_reply(ticket, TicketReply.CONTACT, opened + timedelta(days=2), "a3")
        ticket.closed_at = last_alert.posted_at  # closes in the SAME instant as the last alert
        ok, events = _response_breach_events(
            ticket, [first_alert, mid_alert, last_alert], self.TARGET, now=ticket.closed_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reply"].whmcs_reply_id, "a1")
        self.assertTrue(events[0]["never_answered"])

    def test_unrecognized_author_type_ignored_in_grouping(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        client_msg = make_reply(ticket, TicketReply.CONTACT, opened, "r1")
        unknown = make_reply(ticket, "", client_msg.posted_at + timedelta(minutes=5), "r-bot")
        operator_msg = make_reply(
            ticket, TicketReply.OPERATOR, unknown.posted_at + timedelta(minutes=10), "r2",
        )
        ok, events = _response_breach_events(
            ticket, [client_msg, unknown, operator_msg], self.TARGET, now=operator_msg.posted_at,
        )
        self.assertTrue(ok)
        self.assertEqual(events, [])


class ResolutionBreachEventsTests(TestCase):
    """_resolution_breach_events: a target of 240 minutes (4h) throughout --
    every OPERATOR turn's first message (the ack) must be followed, within
    that SAME uninterrupted turn, by a final message within target. Only the
    trailing (last) turn overall can still grow, so it's always measured
    against the live clock (now/closed_at), never the current last message."""

    TARGET = 240

    def test_no_operator_turns_at_all_is_not_applicable(self):
        ticket = make_ticket()
        ok, events = _resolution_breach_events(ticket, [], self.TARGET, now=timezone.now())
        self.assertIsNone(ok)
        self.assertEqual(events, [])

    def test_single_message_non_trailing_turn_is_trivially_met(self):
        """The ack was immediately followed by the client's next message --
        no separate solution step ever happened, so it counts as met."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        c2 = make_reply(ticket, TicketReply.CONTACT, ack.posted_at + timedelta(minutes=5), "c2")
        ok, events = _resolution_breach_events(ticket, [c1, ack, c2], self.TARGET, now=c2.posted_at)
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_single_message_trailing_turn_pending_within_window_still_open(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        ok, events = _resolution_breach_events(
            ticket, [c1, ack], self.TARGET, now=ack.posted_at + timedelta(minutes=self.TARGET - 1),
        )
        self.assertIsNone(ok)
        self.assertEqual(events, [])

    def test_single_message_trailing_turn_breached_overdue_still_open(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        ok, events = _resolution_breach_events(
            ticket, [c1, ack], self.TARGET, now=ack.posted_at + timedelta(minutes=self.TARGET + 1),
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reply"].whmcs_reply_id, "ack")
        self.assertTrue(events[0]["never_answered"])
        self.assertFalse(events[0]["closed"])

    def test_single_message_trailing_turn_met_when_closed_within_target(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        ticket.closed_at = ack.posted_at + timedelta(minutes=self.TARGET - 10)
        ok, events = _resolution_breach_events(ticket, [c1, ack], self.TARGET, now=ticket.closed_at)
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_single_message_trailing_turn_met_when_status_is_answered_however_long_later(self):
        """Confirmed against real data: a tech sometimes replies with the
        direct fix -- no separate ack step -- leaving exactly one trailing
        operator message. WHMCS's own status (Answered, not In Progress)
        confirms that one message WAS the solution, so it must resolve as
        met immediately, not keep ticking for however long the ticket then
        sits Answered with nobody needing to follow up."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status=TicketSnapshot.ANSWERED_STATUS)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        solution = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "sol")
        ok, events = _resolution_breach_events(
            ticket, [c1, solution], self.TARGET, now=solution.posted_at + timedelta(days=30),
        )
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_single_message_trailing_turn_in_progress_still_ticks_even_though_answered_does_not(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="In Progress")
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        ok, events = _resolution_breach_events(
            ticket, [c1, ack], self.TARGET, now=ack.posted_at + timedelta(minutes=self.TARGET + 1),
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)

    def test_multi_message_turn_met_when_gap_within_target(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        solution = make_reply(ticket, TicketReply.OPERATOR, ack.posted_at + timedelta(minutes=100), "sol")
        c2 = make_reply(ticket, TicketReply.CONTACT, solution.posted_at + timedelta(minutes=5), "c2")
        ok, events = _resolution_breach_events(
            ticket, [c1, ack, solution, c2], self.TARGET, now=c2.posted_at,
        )
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_multi_message_turn_breached_when_gap_exceeds_target(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        solution = make_reply(
            ticket, TicketReply.OPERATOR, ack.posted_at + timedelta(minutes=self.TARGET + 1), "sol",
        )
        c2 = make_reply(ticket, TicketReply.CONTACT, solution.posted_at + timedelta(minutes=5), "c2")
        ok, events = _resolution_breach_events(
            ticket, [c1, ack, solution, c2], self.TARGET, now=c2.posted_at,
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reply"].whmcs_reply_id, "ack")
        self.assertFalse(events[0]["never_answered"])

    def test_trailing_turns_second_message_resolves_it_even_though_ticket_stays_open(self):
        """Confirmed against real data: an ack followed 35 minutes later by
        the actual solution, on a ticket that then just sits in "Answered"
        (the client never needed to reply) -- must resolve as met, not keep
        accruing "elapsed" against however many days later someone happens
        to check, just because the ticket was never formally closed."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        solution = make_reply(ticket, TicketReply.OPERATOR, ack.posted_at + timedelta(minutes=5), "sol")
        ok, events = _resolution_breach_events(
            ticket, [c1, ack, solution], self.TARGET,
            now=solution.posted_at + timedelta(days=30),  # long after, ticket still just sitting Answered
        )
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_trailing_turns_second_message_still_checked_against_target(self):
        """The ack-to-solution gap on a trailing turn is still a real check --
        it just doesn't keep growing against a live clock once it's fixed."""
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        solution = make_reply(
            ticket, TicketReply.OPERATOR, ack.posted_at + timedelta(minutes=self.TARGET + 1), "sol",
        )
        ok, events = _resolution_breach_events(
            ticket, [c1, ack, solution], self.TARGET, now=solution.posted_at + timedelta(days=30),
        )
        self.assertFalse(ok)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reply"].whmcs_reply_id, "ack")
        self.assertFalse(events[0]["never_answered"])

    def test_closed_ticket_freezes_at_closed_at_not_live_now(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        ticket.closed_at = ack.posted_at + timedelta(minutes=100)  # within target
        ok, events = _resolution_breach_events(
            ticket, [c1, ack], self.TARGET,
            now=ack.posted_at + timedelta(days=10),  # would breach if not frozen
        )
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_unrecognized_author_type_ignored_in_grouping(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        c1 = make_reply(ticket, TicketReply.CONTACT, opened, "c1")
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        unknown = make_reply(ticket, "", ack.posted_at + timedelta(minutes=5), "bot")
        c2 = make_reply(ticket, TicketReply.CONTACT, unknown.posted_at + timedelta(minutes=5), "c2")
        ok, events = _resolution_breach_events(
            ticket, [c1, ack, unknown, c2], self.TARGET, now=c2.posted_at,
        )
        self.assertTrue(ok)
        self.assertEqual(events, [])

    def test_recognized_intervening_client_reply_splits_operator_turns_ticket_nkw_994803(self):
        """Regression, real ticket NKW-994803: a client reply using
        author_type "Authorized User" sits between two separate operator
        exchanges, 21h apart. Before the _side() fix, that role was
        unrecognized and silently dropped, merging both operator exchanges
        into one turn and measuring a false ~21h gap between the first ack
        and the second exchange's reply -- a breach, even though each real
        exchange was well within the 4h target (50m, then 25m)."""
        opened = timezone.now()
        # Matches the real ticket: WHMCS's own "Answered" status is set the
        # instant the trailing operator turn's message went out, which is
        # exactly what tells _resolution_breach_events that single trailing
        # message already WAS the solution rather than an open ack still
        # awaiting one (see _resolution_breach_events's own docstring).
        ticket = make_ticket(opened_at=opened, status=TicketSnapshot.ANSWERED_STATUS)
        c1 = make_reply(ticket, "Authorized User", opened, "c1")
        ack1 = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=3), "ack1")
        fix1 = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=53), "fix1")
        # A full day later: a second, unrelated exchange using the same
        # not-Owner/Contact client role.
        c2 = make_reply(ticket, "Authorized User", opened + timedelta(hours=21), "c2")
        fix2 = make_reply(
            ticket, TicketReply.OPERATOR, opened + timedelta(hours=21, minutes=25), "fix2",
        )

        ok, events = _resolution_breach_events(
            ticket, [c1, ack1, fix1, c2, fix2], self.TARGET, now=fix2.posted_at,
        )

        self.assertTrue(ok)
        self.assertEqual(events, [])


class BreachSummaryTests(TestCase):
    """breach_summary(): compact list-column text naming which check(s)
    breached, stating the actual response/resolution time -- not the
    overshoot past target."""

    def setUp(self):
        self.policy = SLAPolicy.objects.create(
            project=make_project(), name="Standard",
            first_response_target_minutes=60, resolution_target_minutes=240,
        )

    def test_not_breached_is_blank(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "1")
        now = opened + timedelta(minutes=10)
        apply_sla_evaluation(ticket, now=now)
        self.assertEqual(breach_summary(ticket, now=now), "-")

    def test_first_response_breach_shows_actual_response_time(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        make_opening_reply(ticket, opened)
        make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=90), "1")
        now = opened + timedelta(minutes=90)
        apply_sla_evaluation(ticket, now=now)
        self.assertEqual(breach_summary(ticket, now=now), "Operator responded after 1h 30m")

    def test_first_response_never_answered_shows_elapsed_so_far(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened)
        make_opening_reply(ticket, opened)
        now = opened + timedelta(minutes=90)
        apply_sla_evaluation(ticket, now=now)
        self.assertEqual(breach_summary(ticket, now=now), "No operator reply yet (1h 30m)")

    def test_resolution_breach_shows_actual_resolution_time(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        make_opening_reply(ticket, opened)
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack")
        # solution 260 min after the ack -- breaches the 240-min resolution target.
        make_reply(ticket, TicketReply.OPERATOR, ack.posted_at + timedelta(minutes=260), "sol")
        ticket.closed_at = ack.posted_at + timedelta(minutes=260)
        now = ticket.closed_at
        apply_sla_evaluation(ticket, now=now)
        self.assertEqual(breach_summary(ticket, now=now), "Operator resolved after 4h 20m")

    def test_resolution_breach_picks_the_longest_of_multiple_breaches(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        make_opening_reply(ticket, opened)
        ack1 = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=10), "ack1")
        sol1 = make_reply(ticket, TicketReply.OPERATOR, ack1.posted_at + timedelta(minutes=250), "sol1")
        make_reply(ticket, TicketReply.CONTACT, sol1.posted_at + timedelta(minutes=5), "c2")
        ack2 = make_reply(ticket, TicketReply.OPERATOR, sol1.posted_at + timedelta(minutes=20), "ack2")
        make_reply(ticket, TicketReply.OPERATOR, ack2.posted_at + timedelta(minutes=300), "sol2")  # longer gap
        ticket.closed_at = ack2.posted_at + timedelta(minutes=300)
        now = ticket.closed_at
        apply_sla_evaluation(ticket, now=now)
        self.assertEqual(breach_summary(ticket, now=now), "Operator resolved after 5h")

    def test_both_checks_breached_shows_both_parts(self):
        opened = timezone.now()
        ticket = make_ticket(opened_at=opened, status="Closed")
        make_opening_reply(ticket, opened)
        ack = make_reply(ticket, TicketReply.OPERATOR, opened + timedelta(minutes=90), "ack")
        make_reply(ticket, TicketReply.OPERATOR, ack.posted_at + timedelta(minutes=260), "sol")
        ticket.closed_at = ack.posted_at + timedelta(minutes=260)
        now = ticket.closed_at
        apply_sla_evaluation(ticket, now=now)
        self.assertEqual(
            breach_summary(ticket, now=now),
            "Operator responded after 1h 30m; Operator resolved after 4h 20m",
        )

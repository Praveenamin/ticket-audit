"""Closed-set tests for the recompute_sla management command's --open-only flag
(Addendum 10) -- the rest of its behavior (plain recompute, --project filter) has no
prior tests either, so those are pinned down here too rather than only the new flag.
"""

from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from .models import Project, TicketSnapshot


def make_project(name="Test Project", **overrides):
    defaults = {"source_type": Project.SOURCE_API}
    defaults.update(overrides)
    project, _ = Project.objects.get_or_create(name=name, defaults=defaults)
    return project


def make_ticket(project, whmcs_ticket_id, status="Open"):
    return TicketSnapshot.objects.create(
        project=project, whmcs_ticket_id=whmcs_ticket_id, tid=f"T-{whmcs_ticket_id}",
        status=status, opened_at=timezone.now(),
    )


class RecomputeSlaCommandTests(TestCase):
    def setUp(self):
        self.project = make_project()

    @patch("audit.management.commands.recompute_sla.apply_sla_evaluation")
    def test_unfiltered_recomputes_every_ticket_including_closed(self, mock_apply):
        open_ticket = make_ticket(self.project, 1, status="Open")
        closed_ticket = make_ticket(self.project, 2, status="Closed")

        call_command("recompute_sla")

        recomputed_ids = {call.args[0].id for call in mock_apply.call_args_list}
        self.assertEqual(recomputed_ids, {open_ticket.id, closed_ticket.id})

    @patch("audit.management.commands.recompute_sla.apply_sla_evaluation")
    def test_open_only_excludes_closed_tickets(self, mock_apply):
        open_ticket = make_ticket(self.project, 1, status="Open")
        make_ticket(self.project, 2, status="Closed")

        call_command("recompute_sla", open_only=True)

        recomputed_ids = {call.args[0].id for call in mock_apply.call_args_list}
        self.assertEqual(recomputed_ids, {open_ticket.id})

    @patch("audit.management.commands.recompute_sla.apply_sla_evaluation")
    def test_project_and_open_only_compose(self, mock_apply):
        other_project = make_project("Other Project")
        this_open = make_ticket(self.project, 1, status="Open")
        make_ticket(self.project, 2, status="Closed")
        make_ticket(other_project, 3, status="Open")

        call_command("recompute_sla", project=self.project.name, open_only=True)

        recomputed_ids = {call.args[0].id for call in mock_apply.call_args_list}
        self.assertEqual(recomputed_ids, {this_open.id})

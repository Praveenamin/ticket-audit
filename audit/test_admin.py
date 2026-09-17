"""Closed-set tests for the admin permission locks that make this app
read-only-from-WHMCS where it needs to be. These were only checked manually
(via the Django test client, ad hoc) when the read-only/locked-field/
immutable-log behaviors were first built -- captured here as regression
tests so a later admin.py change can't silently reopen them.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .admin import TicketEscalationAnalysisAdmin, TicketSnapshotAdmin
from .models import (
    Client as ClientModel, ClosedTicketSummary, Department, DumpUpload, Project,
    ProjectDeletionLog, SLAPolicy, TicketAudit, TicketEscalationAnalysis, TicketNote,
    TicketReply, TicketSnapshot,
)
from .whmcs_client import WHMCSAPIError


def make_superuser():
    User = get_user_model()
    return User.objects.create_superuser(username="auditor", password="pw", email="a@example.com")


def admin_client():
    client = Client(SERVER_NAME="localhost")
    client.force_login(make_superuser())
    return client


class DepartmentReadOnlyTests(TestCase):
    """ReadOnlyAdminMixin: mirrors WHMCS data, so add/change/delete must all
    be denied -- only GET (view) is allowed."""

    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.dept = Department.objects.create(project=self.project, whmcs_deptid=1, name="cPanel")

    def test_add_view_denied(self):
        response = self.client.get(reverse("admin:audit_department_add"))
        self.assertEqual(response.status_code, 403)

    def test_change_view_is_viewable_but_read_only(self):
        url = reverse("admin:audit_department_change", args=[self.dept.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_change_view_post_denied(self):
        url = reverse("admin:audit_department_change", args=[self.dept.id])
        response = self.client.post(url, {"name": "Renamed", "whmcs_deptid": 1, "project": self.project.id})
        self.assertEqual(response.status_code, 403)
        self.dept.refresh_from_db()
        self.assertEqual(self.dept.name, "cPanel")

    def test_delete_denied(self):
        url = reverse("admin:audit_department_delete", args=[self.dept.id])
        response = self.client.post(url, {"post": "yes"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Department.objects.filter(id=self.dept.id).exists())


class TicketSnapshotReadOnlyTests(TestCase):
    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, subject="test",
            status="Open", opened_at=timezone.now(),
        )

    def test_add_view_denied(self):
        response = self.client.get(reverse("admin:audit_ticketsnapshot_add"))
        self.assertEqual(response.status_code, 403)

    def test_change_view_post_denied(self):
        url = reverse("admin:audit_ticketsnapshot_change", args=[self.ticket.id])
        response = self.client.post(url, {"subject": "Hacked"})
        self.assertEqual(response.status_code, 403)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.subject, "test")


class PendingSlaLabelTests(TestCase):
    """sla_status is None for three distinct, legitimate reasons -- used to
    all render as a bare "-", indistinguishable from a data problem. Each
    should now say which one."""

    def setUp(self):
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)

    def test_on_hold_takes_precedence(self):
        ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, status=TicketSnapshot.ON_HOLD_STATUS,
            opened_at=timezone.now(),
        )
        self.assertEqual(TicketSnapshotAdmin._pending_sla_label(ticket), "On hold")

    def test_no_policy_resolved(self):
        ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=2, status="Open", opened_at=timezone.now(),
        )
        self.assertIsNone(ticket.sla_policy_id)
        self.assertEqual(TicketSnapshotAdmin._pending_sla_label(ticket), "No policy")

    def test_still_open_with_a_policy_is_pending(self):
        policy = SLAPolicy.objects.create(
            project=self.project, name="Standard",
            first_response_target_minutes=30, resolution_target_minutes=240,
        )
        ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=3, status="Open", opened_at=timezone.now(),
            sla_policy=policy,
        )
        self.assertEqual(TicketSnapshotAdmin._pending_sla_label(ticket), "Pending")

    def test_badge_renders_pending_label_when_status_is_none(self):
        ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=4, status="Open", opened_at=timezone.now(),
        )
        self.assertIsNone(ticket.sla_status)
        client = admin_client()
        response = client.get(reverse("admin:audit_ticketsnapshot_changelist"))
        self.assertContains(response, "No policy")


class SLAPolicyFieldLockTests(TestCase):
    """project/department are choosable at creation, then locked -- everything
    else (name, targets, active) stays editable after creation."""

    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.other_project = Project.objects.create(name="P2", source_type=Project.SOURCE_API)
        self.dept = Department.objects.create(project=self.project, whmcs_deptid=1, name="cPanel")
        self.other_dept = Department.objects.create(project=self.other_project, whmcs_deptid=1, name="Other")

    def test_add_view_allows_choosing_project_and_department(self):
        response = self.client.post(
            reverse("admin:audit_slapolicy_add"),
            {
                "project": self.project.id, "department": self.dept.id, "name": "Standard",
                "first_response_target_minutes": 30, "resolution_target_minutes": 240,
                "active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        policy = SLAPolicy.objects.get(name="Standard")
        self.assertEqual(policy.project_id, self.project.id)
        self.assertEqual(policy.department_id, self.dept.id)

    def test_change_view_cannot_move_policy_to_another_project_or_department(self):
        policy = SLAPolicy.objects.create(
            project=self.project, department=self.dept, name="Standard",
            first_response_target_minutes=30, resolution_target_minutes=240,
        )
        url = reverse("admin:audit_slapolicy_change", args=[policy.id])
        response = self.client.post(
            url,
            {
                "project": self.other_project.id, "department": self.other_dept.id,
                "name": "Renamed", "first_response_target_minutes": 45,
                "resolution_target_minutes": 300, "active": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        policy.refresh_from_db()
        # Locked fields unchanged...
        self.assertEqual(policy.project_id, self.project.id)
        self.assertEqual(policy.department_id, self.dept.id)
        # ...but everything else still editable.
        self.assertEqual(policy.name, "Renamed")
        self.assertEqual(policy.first_response_target_minutes, 45)


class DumpUploadImmutableTests(TestCase):
    """The upload IS the trigger; once created it's an immutable processing
    log -- add works, change is fully denied (view-only), delete stays open
    to the default admin permission (nothing in the plan required locking
    it), but no field can ever be edited post-creation."""

    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_DUMP)

    def test_add_view_only_shows_project_and_file(self):
        response = self.client.get(reverse("admin:audit_dumpupload_add"))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'name="status"')

    def test_add_creates_queued_upload(self):
        upload_file = SimpleUploadedFile("dump.sql", b"-- fake dump", content_type="text/plain")
        response = self.client.post(
            reverse("admin:audit_dumpupload_add"),
            {"project": self.project.id, "file": upload_file},
        )
        self.assertEqual(response.status_code, 302)
        upload = DumpUpload.objects.get(project=self.project)
        self.assertEqual(upload.status, DumpUpload.STATUS_QUEUED)

    def test_change_view_get_is_viewable_as_the_status_page(self):
        # has_change_permission=False still allows GET: a superuser's
        # implicit view permission renders the detail page read-only rather
        # than 403ing -- this IS the status page (status/counts/error).
        upload = DumpUpload.objects.create(project=self.project)
        url = reverse("admin:audit_dumpupload_change", args=[upload.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_change_view_post_denied(self):
        upload = DumpUpload.objects.create(project=self.project)
        url = reverse("admin:audit_dumpupload_change", args=[upload.id])
        response = self.client.post(url, {"status": DumpUpload.STATUS_DONE})
        self.assertEqual(response.status_code, 403)
        upload.refresh_from_db()
        self.assertEqual(upload.status, DumpUpload.STATUS_QUEUED)

    def test_add_form_preselects_project_from_query_string(self):
        # This is the actual "upload from the Project page" trigger --
        # ProjectAdmin.upload_dump_link just deep-links here with ?project=.
        response = self.client.get(reverse("admin:audit_dumpupload_add") + f"?project={self.project.id}")
        self.assertContains(response, f'<option value="{self.project.id}" selected>')


class ProjectDumpUploadManagementTests(TestCase):
    """Dump uploads are now managed from the Project page, not their own
    sidebar entry: a read-only history inline plus a link to the (still
    fully functional, just unlisted) DumpUpload add form."""

    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_DUMP)

    def test_upload_link_shown_on_existing_project_pointing_at_add_form(self):
        response = self.client.get(reverse("admin:audit_project_change", args=[self.project.id]))
        self.assertContains(
            response,
            f'{reverse("admin:audit_dumpupload_add")}?project={self.project.id}',
        )

    def test_upload_link_absent_on_add_form(self):
        response = self.client.get(reverse("admin:audit_project_add"))
        self.assertNotContains(response, "Upload new dump")

    def test_past_uploads_shown_read_only_on_project_page(self):
        DumpUpload.objects.create(project=self.project, status=DumpUpload.STATUS_DONE, tickets_imported=42)
        response = self.client.get(reverse("admin:audit_project_change", args=[self.project.id]))
        self.assertContains(response, "42")
        # Read-only: no "add another" row for the inline.
        self.assertContains(response, 'dump_uploads-TOTAL_FORMS" value="1"', html=False)


class ProjectTestConnectivityViewTests(TestCase):
    """A plain GET+redirect (not a queued job) that calls WHMCS's GetTickets
    with limitnum=1 and reports success/failure via the messages framework --
    mirrors the exact transport-vs-payload-error split WHMCSClient.call()
    itself makes, so a bad credential shows WHMCS's own message, not just
    an HTTP status."""

    def setUp(self):
        self.client = admin_client()
        self.api_project = Project.objects.create(
            name="P1", source_type=Project.SOURCE_API,
            whmcs_base_url="https://example.com", whmcs_api_identifier="id", whmcs_api_secret="secret",
        )
        self.dump_project = Project.objects.create(name="P2", source_type=Project.SOURCE_DUMP)

    def _url(self, project):
        return reverse("admin:audit_project_test_connectivity", args=[project.pk])

    def test_dump_sourced_project_is_rejected_without_calling_whmcs(self):
        with patch("audit.admin.WHMCSClient.get_tickets_page") as mock_call:
            response = self.client.get(self._url(self.dump_project), follow=True)
        mock_call.assert_not_called()
        self.assertContains(response, "isn&#x27;t API-sourced")

    def test_successful_connection_shows_ticket_count(self):
        with patch("audit.admin.WHMCSClient.get_tickets_page") as mock_call:
            mock_call.return_value = {"result": "success", "totalresults": "7"}
            response = self.client.get(self._url(self.api_project), follow=True)
        self.assertContains(response, "Connected successfully")
        self.assertContains(response, "7 ticket")

    def test_whmcs_payload_error_shows_whmcs_message(self):
        with patch("audit.admin.WHMCSClient.get_tickets_page") as mock_call:
            mock_call.return_value = {"result": "error", "message": "Invalid or missing credentials"}
            response = self.client.get(self._url(self.api_project), follow=True)
        self.assertContains(response, "Invalid or missing credentials")

    def test_transport_failure_shows_connection_failed(self):
        with patch("audit.admin.WHMCSClient.get_tickets_page") as mock_call:
            mock_call.side_effect = WHMCSAPIError("WHMCS API timeout calling GetTickets")
            response = self.client.get(self._url(self.api_project), follow=True)
        self.assertContains(response, "Connection failed")


class RunAiAuditTriggerTests(TestCase):
    """The trigger view works despite TicketSnapshot's ReadOnlyAdminMixin
    (admin_view() only requires staff/login, not has_change_permission) --
    and must not let a second click double-queue an audit that's already
    in flight."""

    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, subject="test",
            status="Open", opened_at=timezone.now(),
        )
        self.url = reverse("admin:audit_ticketsnapshot_run_ai_audit", args=[self.ticket.id])

    def test_get_renders_confirm_page(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Run AI Audit")

    def test_post_creates_queued_audit_and_redirects(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        audit = TicketAudit.objects.get(ticket=self.ticket)
        self.assertEqual(audit.status, TicketAudit.STATUS_QUEUED)

    def test_post_while_already_in_flight_does_not_double_queue(self):
        TicketAudit.objects.create(ticket=self.ticket, status=TicketAudit.STATUS_PROCESSING)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(TicketAudit.objects.filter(ticket=self.ticket).count(), 1)

    def test_anonymous_access_redirects_to_login(self):
        anon_client = Client(SERVER_NAME="localhost")
        response = anon_client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response.url)


class AiAuditModalTests(TestCase):
    """The confirm step is now an in-page modal (ai_audit_display), not a
    separate page -- and its confirm button POSTs via fetch() rather than a
    <form>, since a <form> here would nest inside the change page's own
    outer <form> (invalid HTML). These confirm both the modal markup and
    that a real CSRF-cookie+header POST (what the fetch() actually sends)
    is genuinely accepted, not just Django test Client's default
    CSRF-checks-disabled POST."""

    def setUp(self):
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, subject="test",
            status="Open", opened_at=timezone.now(),
        )
        self.change_url = reverse("admin:audit_ticketsnapshot_change", args=[self.ticket.id])
        self.trigger_url = reverse("admin:audit_ticketsnapshot_run_ai_audit", args=[self.ticket.id])

    def test_change_page_has_modal_not_a_separate_page_link(self):
        response = admin_client().get(self.change_url)
        self.assertContains(response, 'data-bs-toggle="modal"')
        self.assertContains(response, f'data-ai-audit-confirm="{self.trigger_url}"')
        self.assertNotContains(response, f'href="{self.trigger_url}"')

    def test_modal_confirm_button_disabled_and_explained_when_in_flight(self):
        TicketAudit.objects.create(ticket=self.ticket, status=TicketAudit.STATUS_PROCESSING)
        response = admin_client().get(self.change_url)
        self.assertContains(response, "already running")
        self.assertContains(response, "disabled>Run AI Audit</button>")

    def test_ai_audit_modal_js_included(self):
        response = admin_client().get(self.change_url)
        self.assertContains(response, "ai_audit_modal.js")

    def test_fetch_style_post_with_csrf_cookie_and_header_succeeds(self):
        """Proves the real client-side flow (JS reads the csrftoken cookie
        and sends it as an X-CSRFToken header, no <form>) actually passes
        Django's CSRF validation -- not just a test-client POST with checks
        disabled."""
        client = Client(SERVER_NAME="localhost", enforce_csrf_checks=True)
        client.force_login(make_superuser())
        client.get(self.change_url)  # sets the csrftoken cookie
        token = client.cookies["csrftoken"].value

        response = client.post(self.trigger_url, HTTP_X_CSRFTOKEN=token)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(TicketAudit.objects.filter(ticket=self.ticket).count(), 1)


class ProjectAdminOrdinaryCrudTests(TestCase):
    """Unlike everything else in this app, Project itself is plain editable
    add/change -- it's operator-entered configuration, not a mirror of WHMCS.
    Delete is the one exception: see ProjectDeleteEverythingViewTests below --
    the normal admin delete flow is disabled (has_delete_permission=False)
    since it could never succeed anyway (Department/TicketSnapshot always
    block the cascade), so delete_everything_view is the only real path."""

    # DumpUploadInline (read-only history on the Project page) means every
    # POST to this changeform must include its formset's management fields,
    # even when adding zero rows -- Django's formset validation requires them.
    EMPTY_DUMP_UPLOAD_FORMSET = {
        "dump_uploads-TOTAL_FORMS": "0",
        "dump_uploads-INITIAL_FORMS": "0",
        "dump_uploads-MIN_NUM_FORMS": "0",
        "dump_uploads-MAX_NUM_FORMS": "1000",
    }

    def setUp(self):
        self.client = admin_client()

    def test_add_and_edit_roundtrip(self):
        add_response = self.client.post(
            reverse("admin:audit_project_add"),
            {
                "name": "New Co", "source_type": Project.SOURCE_DUMP, "active": "on",
                **self.EMPTY_DUMP_UPLOAD_FORMSET,
            },
        )
        self.assertEqual(add_response.status_code, 302)
        project = Project.objects.get(name="New Co")

        edit_response = self.client.post(
            reverse("admin:audit_project_change", args=[project.id]),
            {
                "name": "Renamed Co", "source_type": Project.SOURCE_DUMP, "active": "on",
                **self.EMPTY_DUMP_UPLOAD_FORMSET,
            },
        )
        self.assertEqual(edit_response.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.name, "Renamed Co")

    def test_normal_delete_flow_is_disabled(self):
        # Regression for a real point of confusion: Django's own standard "Delete"
        # button used to sit right next to delete_everything_link's button, styled
        # nearly identically, and could never succeed anyway (Department/
        # TicketSnapshot always block the cascade) -- a real user clicked it by
        # mistake and hit that dead end. It must no longer be reachable at all.
        project = Project.objects.create(name="P1", source_type=Project.SOURCE_DUMP)
        response = self.client.get(reverse("admin:audit_project_change", args=[project.id]))
        self.assertNotContains(response, f'href="/admin/audit/project/{project.id}/delete/"')

        delete_response = self.client.post(reverse("admin:audit_project_delete", args=[project.id]))
        self.assertEqual(delete_response.status_code, 403)
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())


class ClosedTicketSummaryAdminTests(TestCase):
    """The one AI-produced model in this app meant to be edited -- a human
    corrects the drafted problem_source/type/root_cause/fixed_on here,
    saving stamps reviewed_by/reviewed_at, and ticket/status stay locked."""

    def setUp(self):
        self.user = make_superuser()
        self.client = Client(SERVER_NAME="localhost")
        self.client.force_login(self.user)
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, status="Closed", opened_at=timezone.now(),
        )
        self.summary = ClosedTicketSummary.objects.create(ticket=self.ticket)

    def test_add_view_denied(self):
        response = self.client.get(reverse("admin:audit_closedticketsummary_add"))
        self.assertEqual(response.status_code, 403)

    def test_change_view_get_renders(self):
        url = reverse("admin:audit_closedticketsummary_change", args=[self.summary.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)

    def test_human_edit_saves_and_stamps_reviewer_while_locking_other_fields(self):
        other_ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=2, status="Closed", opened_at=timezone.now(),
        )
        url = reverse("admin:audit_closedticketsummary_change", args=[self.summary.id])
        response = self.client.post(url, {
            "problem_source": "Storage",
            "problem_type": ClosedTicketSummary.PROBLEM_TYPE_OPS_ACTION,
            "problem_root_cause": "VM deployed from a template experiences a delay.",
            "fixed_on": "Shared the root cause with the client; client agreed.",
            # ticket/status aren't real form fields (readonly) -- attempting
            # to smuggle a change through anyway must have no effect.
            "ticket": other_ticket.id,
            "status": ClosedTicketSummary.STATUS_FAILED,
        })
        self.assertEqual(response.status_code, 302)

        self.summary.refresh_from_db()
        self.assertEqual(self.summary.problem_source, "Storage")
        self.assertEqual(self.summary.problem_type, ClosedTicketSummary.PROBLEM_TYPE_OPS_ACTION)
        self.assertEqual(self.summary.problem_root_cause, "VM deployed from a template experiences a delay.")
        self.assertEqual(self.summary.ticket_id, self.ticket.id)
        self.assertEqual(self.summary.status, ClosedTicketSummary.STATUS_QUEUED)
        self.assertEqual(self.summary.reviewed_by, self.user)
        self.assertIsNotNone(self.summary.reviewed_at)

    def test_invalid_problem_type_rejected_by_form_not_saved(self):
        url = reverse("admin:audit_closedticketsummary_change", args=[self.summary.id])
        response = self.client.post(url, {
            "problem_source": "Storage",
            "problem_type": "not-a-real-choice",
            "problem_root_cause": "x",
            "fixed_on": "y",
        })
        self.assertEqual(response.status_code, 200)  # re-rendered with form errors, not a redirect
        self.summary.refresh_from_db()
        self.assertEqual(self.summary.problem_source, "")
        self.assertIsNone(self.summary.reviewed_at)


class TicketSnapshotClosedSummaryDisplayTests(TestCase):
    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, status="Closed", opened_at=timezone.now(),
        )

    def test_shows_not_queued_when_no_summary_exists(self):
        response = self.client.get(reverse("admin:audit_ticketsnapshot_change", args=[self.ticket.id]))
        self.assertContains(response, "Not queued")

    def test_shows_link_to_summary_when_one_exists(self):
        summary = ClosedTicketSummary.objects.create(ticket=self.ticket)
        response = self.client.get(reverse("admin:audit_ticketsnapshot_change", args=[self.ticket.id]))
        self.assertContains(
            response, reverse("admin:audit_closedticketsummary_change", args=[summary.id]),
        )


class QueueClosedTicketsViewTests(TestCase):
    """The "Queue for AI Summary" button on the Closed Tickets Summary
    report -- GET shows a count + ETA confirm page, POST actually queues,
    scoped to exactly the project + date range the report was showing."""

    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.other_project = Project.objects.create(name="P2", source_type=Project.SOURCE_API)
        self.now = timezone.now()
        self.url = reverse("admin:audit_report_closed_tickets_queue")

    def _make_ticket(self, project, whmcs_ticket_id, **overrides):
        defaults = {
            "project": project, "whmcs_ticket_id": whmcs_ticket_id, "status": "Closed",
            "opened_at": self.now, "closed_at": self.now - timedelta(days=5),
        }
        defaults.update(overrides)
        return TicketSnapshot.objects.create(**defaults)

    def _params(self, project=None, date_from="2020-01-01", date_to=None):
        project = project or self.project
        date_to = date_to or timezone.localdate().isoformat()
        return f"?project={project.id}&date_from={date_from}&date_to={date_to}"

    def test_get_shows_pending_count_and_eta(self):
        self._make_ticket(self.project, 1)
        response = self.client.get(self.url + self._params())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "1</strong> ticket")

    def test_get_with_nothing_pending_shows_nothing_to_queue(self):
        response = self.client.get(self.url + self._params())
        self.assertContains(response, "nothing to queue")

    def test_post_queues_and_redirects_with_message(self):
        self._make_ticket(self.project, 1)
        response = self.client.post(self.url + self._params(), follow=True)
        self.assertEqual(ClosedTicketSummary.objects.count(), 1)
        self.assertContains(response, "Queued 1 closed-ticket summary")

    def test_post_does_not_touch_a_different_project(self):
        self._make_ticket(self.other_project, 1)
        self.client.post(self.url + self._params(project=self.project))
        self.assertEqual(ClosedTicketSummary.objects.count(), 0)

    def test_post_does_not_double_queue_already_summarized_ticket(self):
        ticket = self._make_ticket(self.project, 1)
        ClosedTicketSummary.objects.create(ticket=ticket)

        self.client.post(self.url + self._params())

        self.assertEqual(ClosedTicketSummary.objects.filter(ticket=ticket).count(), 1)

    def test_missing_project_redirects_with_error(self):
        response = self.client.get(self.url, follow=True)
        self.assertContains(response, "Pick a project first")


class ExportClosedTicketsCsvViewTests(TestCase):
    """The "Export CSV" button on the Closed Tickets Summary report --
    same rows/columns as the on-screen table, scoped to the same
    project + date range."""

    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.other_project = Project.objects.create(name="P2", source_type=Project.SOURCE_API)
        self.now = timezone.now()
        self.url = reverse("admin:audit_report_closed_tickets_export")

    def _make_ticket(self, project, whmcs_ticket_id, **overrides):
        defaults = {
            "project": project, "whmcs_ticket_id": whmcs_ticket_id, "tid": f"T-{whmcs_ticket_id}",
            "subject": "Server is down", "status": "Closed",
            "opened_at": self.now, "closed_at": self.now - timedelta(days=5),
        }
        defaults.update(overrides)
        return TicketSnapshot.objects.create(**defaults)

    def _params(self, project=None, date_from="2020-01-01", date_to=None):
        project = project or self.project
        date_to = date_to or timezone.localdate().isoformat()
        return f"?project={project.id}&date_from={date_from}&date_to={date_to}"

    def test_response_is_a_csv_attachment(self):
        response = self.client.get(self.url + self._params())
        self.assertEqual(response["Content-Type"], "text/csv")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("P1", response["Content-Disposition"])

    def test_header_row_matches_report_columns(self):
        response = self.client.get(self.url + self._params())
        content = response.content.decode()
        self.assertIn(
            "S.No,Client Name,Ticket Number,Opened,Closed,Short Description,"
            "Problem Source,Problem Type,Problem Root Cause,Fixed on,Status",
            content,
        )

    def test_ticket_with_no_summary_exports_blank_fields_and_not_queued(self):
        self._make_ticket(self.project, 1)
        response = self.client.get(self.url + self._params())
        content = response.content.decode()
        self.assertIn("T-1", content)
        self.assertIn("Not queued", content)

    def test_ticket_with_drafted_summary_exports_its_fields(self):
        ticket = self._make_ticket(self.project, 1)
        ClosedTicketSummary.objects.create(
            ticket=ticket, status=ClosedTicketSummary.STATUS_DRAFTED,
            problem_source="Storage", problem_type=ClosedTicketSummary.PROBLEM_TYPE_OPS_ACTION,
            problem_root_cause="Root cause text", fixed_on="Fixed by X",
        )
        response = self.client.get(self.url + self._params())
        content = response.content.decode()
        self.assertIn("Storage", content)
        self.assertIn("Root cause text", content)
        self.assertIn("Drafted -- awaiting review", content)

    def test_excludes_a_different_project(self):
        self._make_ticket(self.other_project, 1, tid="OTHER-1")
        response = self.client.get(self.url + self._params(project=self.project))
        self.assertNotIn("OTHER-1", response.content.decode())


class ClosedTicketsReportPagingAndStatusFilterTests(TestCase):
    """The report's "Show 50/100/500/All" page-size selector and status
    filter dropdown, wired through the view's GET params."""

    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.now = timezone.now()
        for i in range(3):
            TicketSnapshot.objects.create(
                project=self.project, whmcs_ticket_id=i, tid=f"T-{i}", status="Closed",
                opened_at=self.now, closed_at=self.now - timedelta(days=1),
            )
        self.url = reverse("admin:audit_report_closed_tickets")

    def _params(self, **extra):
        params = {"project": self.project.id, "date_from": "2020-01-01", "date_to": timezone.localdate().isoformat()}
        params.update(extra)
        return "?" + "&".join(f"{k}={v}" for k, v in params.items())

    def test_default_page_size_is_50(self):
        response = self.client.get(self.url + self._params())
        self.assertContains(response, "Showing 3 of 3 ticket")

    def test_page_size_limits_rows_shown(self):
        # 50 is the smallest real choice -- needs >50 tickets to observe
        # actual truncation, on top of the 3 already made in setUp.
        for i in range(3, 55):
            TicketSnapshot.objects.create(
                project=self.project, whmcs_ticket_id=i, tid=f"T-{i}", status="Closed",
                opened_at=self.now, closed_at=self.now - timedelta(days=1),
            )
        response = self.client.get(self.url + self._params(page_size=50))
        self.assertContains(response, "Showing 50 of 55 ticket")

    def test_page_size_all_shows_everything(self):
        response = self.client.get(self.url + self._params(page_size="all"))
        self.assertContains(response, "Showing 3 of 3 ticket")

    def test_invalid_page_size_falls_back_to_default(self):
        response = self.client.get(self.url + self._params(page_size="9999"))
        self.assertContains(response, "Showing 3 of 3 ticket")

    def test_status_filter_narrows_rows(self):
        response = self.client.get(self.url + self._params(status="not_queued"))
        self.assertContains(response, "Showing 3 of 3 ticket")

        ClosedTicketSummary.objects.create(ticket=TicketSnapshot.objects.get(whmcs_ticket_id=0))
        response = self.client.get(self.url + self._params(status="not_queued"))
        self.assertContains(response, "Showing 2 of 2 ticket")


class RatingsReportViewTests(TestCase):
    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.now = timezone.now()
        self.url = reverse("admin:audit_report_ratings")

    def _reply(self, posted_at, rating=5, admin_name="Midhun Jose", whmcs_reply_id="r1"):
        ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, tid="T-1", status="Answered", opened_at=self.now,
        )
        return TicketReply.objects.create(
            ticket=ticket, whmcs_reply_id=whmcs_reply_id, author_type=TicketReply.OPERATOR,
            admin_name=admin_name, message="msg", posted_at=posted_at, rating=rating,
        )

    def test_url_resolves(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_default_window_includes_a_reply_from_20_days_ago(self):
        self._reply(self.now - timedelta(days=20))
        response = self.client.get(self.url + f"?project={self.project.id}")
        self.assertContains(response, "T-1")

    def test_default_window_excludes_a_reply_from_40_days_ago(self):
        self._reply(self.now - timedelta(days=40))
        response = self.client.get(self.url + f"?project={self.project.id}")
        self.assertContains(response, "No rated replies in this window.")

    def test_explicit_date_range_picks_up_an_older_reply(self):
        self._reply(self.now - timedelta(days=40))
        response = self.client.get(
            self.url + f"?project={self.project.id}&date_from=2020-01-01"
            f"&date_to={timezone.localdate().isoformat()}"
        )
        self.assertContains(response, "T-1")

    def test_table_shows_ticket_client_rating_and_tech(self):
        self._reply(self.now - timedelta(days=1), rating=4, admin_name="Kanagaraj B")
        response = self.client.get(self.url + f"?project={self.project.id}")
        self.assertContains(response, "T-1")
        self.assertContains(response, "Kanagaraj B")
        self.assertContains(response, "4")

    def test_empty_state_when_no_rated_replies(self):
        response = self.client.get(self.url + f"?project={self.project.id}")
        self.assertContains(response, "No rated replies in this window.")


def staff_client():
    User = get_user_model()
    user = User.objects.create_user(
        username="staffer", password="pw", email="s@example.com", is_staff=True,
    )
    client = Client(SERVER_NAME="localhost")
    client.force_login(user)
    return client


class ProjectDeleteEverythingViewTests(TestCase):
    """ProjectAdmin's "Delete project and everything under it" -- a dedicated,
    superuser-only bypass of ReadOnlyAdminMixin's normal delete block, scoped to
    exactly one Project's own cascade, with a type-the-name confirmation gate."""

    def setUp(self):
        self.admin = admin_client()
        self.project = Project.objects.create(name="Doomed Project", source_type=Project.SOURCE_API)
        self.other_project = Project.objects.create(name="Untouched Project", source_type=Project.SOURCE_API)
        self.now = timezone.now()

        self.department = Department.objects.create(project=self.project, whmcs_deptid=1, name="Support")
        self.wclient = ClientModel.objects.create(project=self.project, whmcs_client_id=1, name="Acme")
        self.policy = SLAPolicy.objects.create(
            project=self.project, name="Standard", first_response_target_minutes=30, resolution_target_minutes=240,
        )
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, tid="T-1", status="Open", opened_at=self.now,
        )
        self.reply = TicketReply.objects.create(
            ticket=self.ticket, whmcs_reply_id="1", author_type=TicketReply.OPERATOR,
            admin_name="Bob", message="hi", posted_at=self.now,
        )
        self.note = TicketNote.objects.create(ticket=self.ticket, whmcs_note_id="1", message="internal", posted_at=self.now)
        self.audit = TicketAudit.objects.create(ticket=self.ticket)
        self.summary = ClosedTicketSummary.objects.create(ticket=self.ticket)
        self.upload = DumpUpload.objects.create(
            project=self.project, file=SimpleUploadedFile("dump.sql", b"-- fake dump"),
        )

        # A second project's data must survive untouched.
        self.other_ticket = TicketSnapshot.objects.create(
            project=self.other_project, whmcs_ticket_id=9, tid="T-9", status="Open", opened_at=self.now,
        )

        self.url = reverse("admin:audit_project_delete_everything", args=[self.project.id])

    def test_get_shows_exact_counts_for_every_affected_model(self):
        response = self.admin.get(self.url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        for label, expected in [
            ("Department", 1), ("Client", 1), ("SLA policy", 1), ("Dump upload", 1),
            ("Ticket snapshot", 1), ("Ticket reply", 1), ("Ticket note", 1),
            ("AI ticket audit", 1), ("Closed ticket summary", 1),
        ]:
            self.assertContains(response, f"<td>{label}</td>")
        self.assertContains(response, "Delete 9 row(s) permanently")

    def test_post_with_wrong_name_deletes_nothing(self):
        response = self.admin.post(self.url, {"confirm_name": "not the right name"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())
        self.assertEqual(ProjectDeletionLog.objects.count(), 0)

    def test_post_with_blank_name_deletes_nothing(self):
        response = self.admin.post(self.url, {"confirm_name": ""})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_post_with_exact_name_deletes_the_whole_cascade_and_logs_it(self):
        dump_path = self.upload.file.name
        self.assertTrue(default_storage.exists(dump_path))

        response = self.admin.post(self.url, {"confirm_name": "Doomed Project"})

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Project.objects.filter(pk=self.project.pk).exists())
        self.assertFalse(Department.objects.filter(pk=self.department.pk).exists())
        self.assertFalse(ClientModel.objects.filter(pk=self.wclient.pk).exists())
        self.assertFalse(SLAPolicy.objects.filter(pk=self.policy.pk).exists())
        self.assertFalse(DumpUpload.objects.filter(pk=self.upload.pk).exists())
        self.assertFalse(TicketSnapshot.objects.filter(pk=self.ticket.pk).exists())
        self.assertFalse(TicketReply.objects.filter(pk=self.reply.pk).exists())
        self.assertFalse(TicketNote.objects.filter(pk=self.note.pk).exists())
        self.assertFalse(TicketAudit.objects.filter(pk=self.audit.pk).exists())
        self.assertFalse(ClosedTicketSummary.objects.filter(pk=self.summary.pk).exists())

        log = ProjectDeletionLog.objects.get()
        self.assertEqual(log.project_name, "Doomed Project")
        self.assertEqual(
            log.row_counts,
            {
                "Department": 1, "Client": 1, "SLA policy": 1, "Dump upload": 1,
                "Ticket snapshot": 1, "Ticket reply": 1, "Ticket note": 1,
                "AI ticket audit": 1, "Closed ticket summary": 1,
            },
        )

        self.assertFalse(default_storage.exists(dump_path))

    def test_a_different_projects_data_is_untouched(self):
        self.admin.post(self.url, {"confirm_name": "Doomed Project"})

        self.assertTrue(Project.objects.filter(pk=self.other_project.pk).exists())
        self.assertTrue(TicketSnapshot.objects.filter(pk=self.other_ticket.pk).exists())

    def test_non_superuser_staff_gets_403_on_get(self):
        response = staff_client().get(self.url)
        self.assertEqual(response.status_code, 403)

    def test_non_superuser_staff_gets_403_on_post(self):
        response = staff_client().post(self.url, {"confirm_name": "Doomed Project"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Project.objects.filter(pk=self.project.pk).exists())

    def test_delete_everything_view_source_never_touches_whmcs_or_mysql(self):
        # The safety property the user cares about most, made checkable rather than
        # just asserted in a comment: this view (and its count helper) must never be
        # able to reach WHMCS's own API or the dump-import MySQL container. Checked at
        # the bytecode level (co_names -- every global/attribute name the function
        # actually references) rather than a raw source-text grep, so this can't be
        # fooled by a docstring/comment mentioning these names in prose (as this very
        # method's own docstring does, to explain the guarantee) and can't miss a real
        # usage buried in an expression either.
        from .admin import ProjectAdmin

        names = set(ProjectAdmin.delete_everything_view.__code__.co_names) | set(
            ProjectAdmin._project_delete_counts.__code__.co_names
        )
        for forbidden in ("WHMCSClient", "whmcs_client", "pymysql", "staging_db", "dump_import"):
            self.assertNotIn(forbidden, names)


class EscalationRiskBadgeTests(TestCase):
    """TicketSnapshotAdmin.escalation_risk_badge -- color/emoji/score per sentiment
    category, and the fallback text when there's nothing to show yet. Called
    directly on a real ModelAdmin instance rather than through an HTTP request --
    matches _pending_sla_label's own direct-call test style, no rendering pipeline
    needed to check this one method's output."""

    def setUp(self):
        self.admin = TicketSnapshotAdmin(TicketSnapshot, django_admin.site)
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, tid="T-1", status="Open", opened_at=timezone.now(),
        )

    def test_no_analysis_yet_shows_muted_fallback(self):
        html = self.admin.escalation_risk_badge(self.ticket)
        self.assertIn("Not yet analyzed", html)

    def test_queued_status_shows_status_not_a_score(self):
        TicketEscalationAnalysis.objects.create(ticket=self.ticket, status=TicketEscalationAnalysis.STATUS_QUEUED)
        html = self.admin.escalation_risk_badge(self.ticket)
        self.assertIn("Queued", html)

    def test_critical_renders_red_with_score(self):
        TicketEscalationAnalysis.objects.create(
            ticket=self.ticket, status=TicketEscalationAnalysis.STATUS_DONE,
            sentiment_category=TicketEscalationAnalysis.SENTIMENT_CRITICAL, escalation_risk_score=92,
        )
        html = self.admin.escalation_risk_badge(self.ticket)
        self.assertIn("text-danger", html)
        self.assertIn("🔴", html)
        self.assertIn("92%", html)

    def test_frustrated_renders_yellow_with_score(self):
        TicketEscalationAnalysis.objects.create(
            ticket=self.ticket, status=TicketEscalationAnalysis.STATUS_DONE,
            sentiment_category=TicketEscalationAnalysis.SENTIMENT_FRUSTRATED, escalation_risk_score=55,
        )
        html = self.admin.escalation_risk_badge(self.ticket)
        self.assertIn("text-warning", html)
        self.assertIn("🟡", html)
        self.assertIn("55%", html)

    def test_healthy_renders_green_with_score(self):
        TicketEscalationAnalysis.objects.create(
            ticket=self.ticket, status=TicketEscalationAnalysis.STATUS_DONE,
            sentiment_category=TicketEscalationAnalysis.SENTIMENT_HEALTHY, escalation_risk_score=5,
        )
        html = self.admin.escalation_risk_badge(self.ticket)
        self.assertIn("text-success", html)
        self.assertIn("🟢", html)
        self.assertIn("5%", html)

    def test_column_is_sortable_by_risk_score(self):
        # @admin.display(ordering=...) stamps admin_order_field on the function --
        # this IS what makes the column header a clickable sort link in the real
        # changelist, so checking it directly proves the column is genuinely
        # sortable without needing a full HTTP changelist round-trip.
        self.assertEqual(
            TicketSnapshotAdmin.escalation_risk_badge.admin_order_field,
            "escalation_analysis__escalation_risk_score",
        )

    def test_list_filter_includes_sentiment_category(self):
        self.assertIn("escalation_analysis__sentiment_category", TicketSnapshotAdmin.list_filter)


class TicketEscalationAnalysisAdminTests(TestCase):
    """Standalone, read-only, project-wide escalation-analysis list -- mirrors
    TicketAuditAdmin: creation only ever happens via sync.py's hook, never here."""

    def setUp(self):
        self.client = admin_client()
        self.project = Project.objects.create(name="P1", source_type=Project.SOURCE_API)
        self.ticket = TicketSnapshot.objects.create(
            project=self.project, whmcs_ticket_id=1, tid="T-1", status="Open", opened_at=timezone.now(),
        )
        self.analysis = TicketEscalationAnalysis.objects.create(
            ticket=self.ticket, status=TicketEscalationAnalysis.STATUS_DONE,
            sentiment_category=TicketEscalationAnalysis.SENTIMENT_CRITICAL, escalation_risk_score=90,
        )

    def test_add_permission_denied(self):
        admin_instance = TicketEscalationAnalysisAdmin(TicketEscalationAnalysis, django_admin.site)
        self.assertFalse(admin_instance.has_add_permission(None))

    def test_change_permission_denied(self):
        admin_instance = TicketEscalationAnalysisAdmin(TicketEscalationAnalysis, django_admin.site)
        self.assertFalse(admin_instance.has_change_permission(None))

    def test_changelist_shows_the_row(self):
        response = self.client.get(reverse("admin:audit_ticketescalationanalysis_changelist"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "T-1")
        self.assertContains(response, "90")

"""Closed-set tests for the admin permission locks that make this app
read-only-from-WHMCS where it needs to be. These were only checked manually
(via the Django test client, ad hoc) when the read-only/locked-field/
immutable-log behaviors were first built -- captured here as regression
tests so a later admin.py change can't silently reopen them.
"""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Department, DumpUpload, Project, SLAPolicy, TicketSnapshot


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


class ProjectAdminOrdinaryCrudTests(TestCase):
    """Unlike everything else in this app, Project itself is plain editable
    CRUD -- it's operator-entered configuration, not a mirror of WHMCS."""

    def setUp(self):
        self.client = admin_client()

    def test_add_and_edit_roundtrip(self):
        add_response = self.client.post(
            reverse("admin:audit_project_add"),
            {"name": "New Co", "source_type": Project.SOURCE_DUMP, "active": "on"},
        )
        self.assertEqual(add_response.status_code, 302)
        project = Project.objects.get(name="New Co")

        edit_response = self.client.post(
            reverse("admin:audit_project_change", args=[project.id]),
            {"name": "Renamed Co", "source_type": Project.SOURCE_DUMP, "active": "on"},
        )
        self.assertEqual(edit_response.status_code, 302)
        project.refresh_from_db()
        self.assertEqual(project.name, "Renamed Co")

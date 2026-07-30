from django.db import models
from django.utils import timezone


class Project(models.Model):
    """One audited WHMCS instance/company. Departments, SLA policies, and
    tickets are all scoped to a project so multiple WHMCS sources never mix."""

    SOURCE_API = "api"
    SOURCE_DUMP = "dump"
    SOURCE_CHOICES = [
        (SOURCE_API, "WHMCS API"),
        (SOURCE_DUMP, "DB dump upload"),
    ]

    name = models.CharField(max_length=255, unique=True)
    source_type = models.CharField(max_length=10, choices=SOURCE_CHOICES, default=SOURCE_DUMP)
    whmcs_base_url = models.CharField(max_length=255, blank=True)
    whmcs_api_identifier = models.CharField(max_length=255, blank=True)
    whmcs_api_secret = models.CharField(max_length=255, blank=True)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Department(models.Model):
    """Lightweight cache of WHMCS support departments, upserted at ingest time
    (from either the API sync or a dump import), scoped per project."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="departments")
    whmcs_deptid = models.PositiveIntegerField()
    name = models.CharField(max_length=255)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["project__name", "name"]
        unique_together = [("project", "whmcs_deptid")]

    def __str__(self):
        return f"{self.name} ({self.project.name})"


class SLAPolicy(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="sla_policies")
    name = models.CharField(max_length=255)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.CASCADE, related_name="sla_policies",
        help_text="Leave blank to apply to all departments in this project. "
        "Fixed once the policy is created.",
    )
    first_response_target_minutes = models.PositiveIntegerField()
    resolution_target_minutes = models.PositiveIntegerField()
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "SLA policies"

    def __str__(self):
        scope = self.department.name if self.department_id else "All departments"
        return f"{self.name} ({self.project.name} / {scope})"

    def specificity_rank(self):
        """Precedence when multiple policies match the same ticket:
        department-specific beats the global (department-null) default."""
        return 1 if self.department_id else 0

    def matches(self, department_id):
        return self.department_id is None or self.department_id == department_id


class TicketSnapshot(models.Model):
    SLA_BREACHED = "breached"
    SLA_MET = "met"
    SLA_STATUS_CHOICES = [
        (SLA_BREACHED, "Breached"),
        (SLA_MET, "Met"),
    ]
    ON_HOLD_STATUS = "On Hold"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tickets")
    whmcs_ticket_id = models.PositiveIntegerField()
    tid = models.CharField(max_length=50, blank=True)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets",
    )
    subject = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=100)
    priority = models.CharField(max_length=50, blank=True)
    requestor_name = models.CharField(max_length=255, blank=True)
    requestor_email = models.CharField(max_length=255, blank=True)

    opened_at = models.DateTimeField()
    last_reply_at = models.DateTimeField(null=True, blank=True)
    first_response_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Timestamp of the earliest Operator reply.",
    )
    closed_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set the first time an ingest observes status=Closed; may lag real "
        "closure by up to one sync/import interval.",
    )
    synced_at = models.DateTimeField(default=timezone.now)

    sla_policy = models.ForeignKey(
        SLAPolicy, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets",
        help_text="Snapshot of which policy applied at last evaluation.",
    )
    first_response_due_at = models.DateTimeField(null=True, blank=True)
    first_response_met = models.BooleanField(null=True, blank=True)
    follow_up_met = models.BooleanField(
        null=True, blank=True,
        help_text="True if every follow-up client message seen so far got an operator "
        "reply within target; False if any breached; null if no follow-up exchange "
        "has happened yet.",
    )
    first_response_target_minutes_at_eval = models.PositiveIntegerField(null=True, blank=True)
    follow_up_target_minutes_at_eval = models.PositiveIntegerField(null=True, blank=True)
    sla_status = models.CharField(
        max_length=20, choices=SLA_STATUS_CHOICES, null=True, blank=True,
    )

    class Meta:
        ordering = ["-last_reply_at"]
        unique_together = [("project", "whmcs_ticket_id")]

    def __str__(self):
        return f"#{self.tid or self.whmcs_ticket_id} - {self.subject}"


class TicketReply(models.Model):
    OWNER = "Owner"
    CONTACT = "Contact"
    OPERATOR = "Operator"

    ticket = models.ForeignKey(TicketSnapshot, on_delete=models.CASCADE, related_name="ticket_replies")
    whmcs_reply_id = models.CharField(max_length=50)
    author_name = models.CharField(max_length=255, blank=True)
    author_type = models.CharField(max_length=20, blank=True)
    admin_name = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    posted_at = models.DateTimeField()

    class Meta:
        ordering = ["posted_at"]
        unique_together = [("ticket", "whmcs_reply_id")]

    def __str__(self):
        return f"Reply {self.whmcs_reply_id} on ticket {self.ticket_id}"


class DumpUpload(models.Model):
    """One uploaded WHMCS DB dump and its background-processing lifecycle.
    Immutable once created (admin denies edit/delete) -- it's a log entry, not
    a configuration object."""

    STATUS_QUEUED = "queued"
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_DONE, "Done"),
        (STATUS_FAILED, "Failed"),
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="dump_uploads")
    file = models.FileField(upload_to="whmcs_dumps/")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    tickets_imported = models.PositiveIntegerField(null=True, blank=True)
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-uploaded_at"]

    def __str__(self):
        return f"{self.project.name} dump ({self.uploaded_at:%Y-%m-%d %H:%M}) - {self.status}"

from django.db import models
from django.utils import timezone


class Department(models.Model):
    """Lightweight cache of WHMCS support departments, upserted at sync time from
    ticket payloads (the GetSupportDepartments API action is blocked by our
    credential's role, so we never call it directly)."""

    whmcs_deptid = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=255)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class SLAPolicy(models.Model):
    name = models.CharField(max_length=255)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.CASCADE, related_name="sla_policies",
        help_text="Leave blank to apply to all departments. Fixed once the policy is created.",
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
        return f"{self.name} ({scope})"

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

    whmcs_ticket_id = models.PositiveIntegerField(unique=True)
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
        help_text="Set the first time a sync observes status=Closed; may lag real "
        "closure by up to one sync interval.",
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

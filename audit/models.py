from django.conf import settings
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
    use_synthetic_tid = models.BooleanField(
        default=True,
        help_text="When on, a ticket ID that doesn't already look like WHMCS's own "
        "letter-masked format (e.g. blank, or equal to the internal ticket ID) gets "
        "replaced with a generated one of the same shape. Turn this off if this "
        "project's real WHMCS ticket numbers are being incorrectly replaced -- e.g. "
        "some WHMCS installs use a distinct plain-number ticket ID that isn't masked "
        "but is still real and should be shown as-is.",
    )
    s3_archive_enabled = models.BooleanField(
        default=False,
        help_text="When on, every reply on an eligible ticket in this project is "
        "archived to S3 as it's synced. Eligibility itself (ticket opened today or "
        "later, decided once the first time this project's sync sees the ticket) is "
        "tracked per ticket, not here -- toggling this off only stops new replies "
        "from being uploaded going forward, it does not retroactively un-mark "
        "tickets already flagged eligible, and toggling it on only covers tickets "
        "first synced after the toggle flips. Only meaningful for source_type=\"api\" "
        "projects -- a dump import never checks this flag at all.",
    )
    s3_bucket_name = models.CharField(max_length=255, blank=True)
    s3_access_key_id = models.CharField(max_length=255, blank=True)
    s3_secret_access_key = models.CharField(max_length=255, blank=True)
    s3_region = models.CharField(max_length=50, blank=True)
    escalation_analysis_enabled = models.BooleanField(
        default=False,
        help_text="When on, every non-Closed ticket in this project gets an automatic "
        "sentiment/escalation-risk analysis (TicketEscalationAnalysis) re-run every "
        "time its reply thread changes, via sync.py's own hook -- no manual trigger, "
        "no backfill of existing tickets when first turned on (only tickets that get a "
        "new reply from that point onward are analyzed).",
    )
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


class Client(models.Model):
    """Lightweight cache of WHMCS client accounts, upserted at ingest time
    from tblclients (dump path only, for now), scoped per project. Exists so
    TicketSnapshot can report on the actual account name instead of the
    frequently-blank per-ticket requestor_name field (tbltickets.name is
    often empty for an already-logged-in client)."""

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="clients")
    whmcs_client_id = models.PositiveIntegerField()
    # 500, not 255: real data has hit companyname values over 1200 chars
    # (garbage, not a real company name, but ingest can't assume that never
    # happens again) -- dump_import.py's client_display_name() also
    # defensively truncates to this same length so it can never overflow.
    name = models.CharField(max_length=500)
    email = models.CharField(max_length=255, blank=True)
    last_seen_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["project__name", "name"]
        unique_together = [("project", "whmcs_client_id")]

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
    ANSWERED_STATUS = "Answered"

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="tickets")
    whmcs_ticket_id = models.PositiveIntegerField()
    tid = models.CharField(max_length=50, blank=True)
    department = models.ForeignKey(
        Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets",
    )
    client = models.ForeignKey(
        Client, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets",
        help_text="The WHMCS account this ticket's client belongs to, when known -- "
        "distinct from requestor_name, which is the often-blank per-ticket field.",
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
        help_text="Timestamp of the most recent observed closure; updates again if the "
        "ticket is reopened and closed a second time. May lag real closure by up to "
        "one sync/import interval.",
    )
    synced_at = models.DateTimeField(default=timezone.now)

    sla_policy = models.ForeignKey(
        SLAPolicy, null=True, blank=True, on_delete=models.SET_NULL, related_name="tickets",
        help_text="Snapshot of which policy applied at last evaluation.",
    )
    first_response_met = models.BooleanField(
        null=True, blank=True,
        help_text="True if every client turn -- the original open and every later "
        "reopen/Customer-Reply alike -- got its first operator reply within target; "
        "False if any breached; null if none has been checked yet.",
    )
    follow_up_met = models.BooleanField(
        null=True, blank=True,
        help_text="True if every operator turn's initial ack was followed by a final "
        "answer within target (or never grew past one message); False if any "
        "breached; null if no operator turn has happened yet.",
    )
    first_response_target_minutes_at_eval = models.PositiveIntegerField(null=True, blank=True)
    follow_up_target_minutes_at_eval = models.PositiveIntegerField(null=True, blank=True)
    sla_status = models.CharField(
        max_length=20, choices=SLA_STATUS_CHOICES, null=True, blank=True,
    )
    s3_archive_eligible = models.BooleanField(
        default=False,
        help_text="True if this ticket was first observed by the WHMCS sync on the "
        "same Asia/Kolkata calendar day it was opened, AND its project had "
        "s3_archive_enabled on at that exact moment -- decided once, the first time "
        "this ticket is synced, and never recomputed afterward, so this ticket's own "
        "replies keep archiving for its whole lifecycle even once the calendar day "
        "changes. Always False for dump-imported tickets.",
    )

    class Meta:
        ordering = ["-last_reply_at"]
        unique_together = [("project", "whmcs_ticket_id")]

    @staticmethod
    def should_bump_closed_at(current_closed_at, last_reply_at):
        """True if an observed closure is NEW information -- either the first
        time this ticket's been seen closed, or a later closure than the one
        already recorded (i.e. it was reopened and closed again). Without
        this, closed_at would freeze at the FIRST closure forever, and any
        exchange after a reopen would silently vanish from the breach walk
        (elapsed vs. a stale closed_at goes negative -- neither breach nor
        pending)."""
        return current_closed_at is None or bool(last_reply_at and last_reply_at > current_closed_at)

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
    rating = models.PositiveSmallIntegerField(
        default=0,
        help_text="WHMCS's own 'rate this reply' 1-5 star client rating on this specific "
        "reply -- 0 means unrated (the vast majority). Not a separate table -- WHMCS "
        "stores it as a plain column alongside the rated reply itself, so this reply's "
        "own admin_name IS the tech who wrote the rated reply. WHMCS records no separate "
        "'date the client submitted the rating' anywhere -- only this reply's own "
        "posted_at exists, so any date-filtered rating report is necessarily keyed off "
        "when the reply was POSTED, not when it was actually rated.",
    )
    posted_at = models.DateTimeField()
    archived_to_s3_at = models.DateTimeField(
        null=True, blank=True,
        help_text="Set only after a confirmed-successful S3 upload -- the idempotency "
        "marker checked before uploading, since a ticket's replies get reprocessed "
        "from a fresh WHMCS fetch on every full refresh, not just newly-arrived ones. "
        "Null means archival was never attempted (not eligible/not enabled) or was "
        "attempted and failed; both cases are always safe to retry.",
    )

    class Meta:
        ordering = ["posted_at"]
        unique_together = [("ticket", "whmcs_reply_id")]

    def __str__(self):
        return f"Reply {self.whmcs_reply_id} on ticket {self.ticket_id}"


class TicketNote(models.Model):
    """WHMCS's internal, staff-only admin notes (tblticketnotes) -- separate
    from the client-visible TicketReply thread. Fed into the AI audit
    alongside replies so it can check things only visible here, e.g. whether
    a fix's details were recorded for future techs even when not explained
    to the client."""

    ticket = models.ForeignKey(TicketSnapshot, on_delete=models.CASCADE, related_name="ticket_notes")
    whmcs_note_id = models.CharField(max_length=50)
    admin_name = models.CharField(max_length=255, blank=True)
    message = models.TextField(blank=True)
    posted_at = models.DateTimeField()

    class Meta:
        ordering = ["posted_at"]
        unique_together = [("ticket", "whmcs_note_id")]

    def __str__(self):
        return f"Note {self.whmcs_note_id} on ticket {self.ticket_id}"


class TicketAudit(models.Model):
    """One LLM-generated audit of a single ticket's reply thread. Never
    edited in place -- a failed/needs_review run is retried by creating a
    NEW row (mirrors DumpUpload's own immutable-log-entry convention),
    never by mutating this one, so history of past runs is preserved."""

    STATUS_QUEUED = "queued"
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_DONE, "Done"),
        (STATUS_NEEDS_REVIEW, "Needs review"),
        (STATUS_FAILED, "Failed"),
    ]

    SENTIMENT_POSITIVE = "positive"
    SENTIMENT_NEUTRAL = "neutral"
    SENTIMENT_NEGATIVE = "negative"
    SENTIMENT_MIXED = "mixed"
    SENTIMENT_CHOICES = [
        (SENTIMENT_POSITIVE, "Positive"),
        (SENTIMENT_NEUTRAL, "Neutral"),
        (SENTIMENT_NEGATIVE, "Negative"),
        (SENTIMENT_MIXED, "Mixed"),
    ]

    ticket = models.ForeignKey(TicketSnapshot, on_delete=models.CASCADE, related_name="ai_audits")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)

    requested_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    model_used = models.CharField(
        max_length=100, blank=True,
        help_text="Snapshot of settings.OLLAMA_MODEL at run time, same _at_eval-snapshot "
        "idea sla.py uses for SLA targets -- keeps an old run reproducible even if the "
        "model setting changes later.",
    )

    sentiment_label = models.CharField(max_length=20, choices=SENTIMENT_CHOICES, blank=True)
    sentiment_summary = models.TextField(blank=True)
    missed_queries = models.JSONField(default=list, blank=True)
    positives = models.JSONField(default=list, blank=True)
    negatives = models.JSONField(default=list, blank=True)

    raw_response = models.TextField(
        blank=True, help_text="Full Ollama reply, kept for debugging needs_review/failed rows.",
    )
    error_message = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at"]
        verbose_name_plural = "AI ticket audits"

    def __str__(self):
        return f"AI audit of ticket {self.ticket_id} ({self.status})"


class TicketEscalationAnalysis(models.Model):
    """Automatic, ongoing customer-escalation-risk radar -- distinct from TicketAudit
    above (a manual, on-demand audit of the AGENT's performance). One row PER TICKET
    (OneToOneField, like ClosedTicketSummary), re-evaluated and updated in place every
    time the ticket's reply thread changes (see sync.py's _upsert_replies hook) so the
    score stays current as a conversation evolves -- not an immutable per-run log like
    TicketAudit, since there's exactly one "current" risk assessment to track."""

    STATUS_QUEUED = "queued"
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_DONE, "Done"),
        (STATUS_NEEDS_REVIEW, "Needs review"),
        (STATUS_FAILED, "Failed"),
    ]

    SENTIMENT_CRITICAL = "critical"
    SENTIMENT_FRUSTRATED = "frustrated"
    SENTIMENT_HEALTHY = "healthy"
    SENTIMENT_CHOICES = [
        (SENTIMENT_CRITICAL, "🔴 Critical (Near Escalation)"),
        (SENTIMENT_FRUSTRATED, "🟡 Frustrated (Not Happy)"),
        (SENTIMENT_HEALTHY, "🟢 Healthy (Satisfied)"),
    ]

    DRIVER_DOWNTIME = "downtime"
    DRIVER_SLOW_RESPONSE = "slow_response"
    DRIVER_UNRESOLVED_LOOP = "unresolved_technical_loop"
    DRIVER_BILLING_ISSUE = "billing_issue"
    DRIVER_REPEATED_ESCALATION = "repeated_escalation"
    DRIVER_MISCOMMUNICATION = "miscommunication"
    DRIVER_OTHER = "other"
    DRIVER_CHOICES = [
        (DRIVER_DOWNTIME, "Downtime"),
        (DRIVER_SLOW_RESPONSE, "Slow Response"),
        (DRIVER_UNRESOLVED_LOOP, "Unresolved Technical Loop"),
        (DRIVER_BILLING_ISSUE, "Billing Issue"),
        (DRIVER_REPEATED_ESCALATION, "Repeated Escalation"),
        (DRIVER_MISCOMMUNICATION, "Miscommunication"),
        (DRIVER_OTHER, "Other"),
    ]

    ticket = models.OneToOneField(TicketSnapshot, on_delete=models.CASCADE, related_name="escalation_analysis")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)

    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    model_used = models.CharField(
        max_length=100, blank=True,
        help_text="Snapshot of settings.OLLAMA_MODEL at run time, same _at_eval-snapshot idea as elsewhere in this app.",
    )

    sentiment_category = models.CharField(max_length=20, choices=SENTIMENT_CHOICES, blank=True)
    escalation_risk_score = models.PositiveSmallIntegerField(
        null=True, blank=True, help_text="0-100 -- 0 means no risk of escalation, 100 means about to escalate/churn.",
    )
    frustration_driver = models.CharField(
        max_length=30, choices=DRIVER_CHOICES, blank=True,
        help_text="Blank only when sentiment_category is 'healthy' -- there's no frustration driver for a satisfied ticket.",
    )
    justification = models.TextField(blank=True, help_text="1-sentence reason for the score, drafted by the LLM.")

    raw_response = models.TextField(
        blank=True, help_text="Full Ollama reply, kept for debugging needs_review/failed rows.",
    )
    error_message = models.TextField(blank=True)

    class Meta:
        verbose_name_plural = "Ticket escalation analyses"

    def __str__(self):
        return f"Escalation analysis of ticket {self.ticket_id} ({self.status})"


class ClosedTicketSummary(models.Model):
    """AI-drafted Problem Source/Type/Root Cause/Fixed-on for one Closed
    ticket, for the "Closed Tickets Summary" report -- one row per ticket
    that gets EDITED over time, unlike TicketAudit's immutable per-run log:
    there's exactly one "current" classification per ticket to track, not a
    history of repeated runs to preserve. A human reviewing/editing the
    drafted fields via the admin IS the review step (see reviewed_at/
    reviewed_by) -- unrelated to STATUS_NEEDS_REVIEW below, which (exactly
    like on TicketAudit) means the AI's own JSON output failed to parse or
    validate, nothing to do with human review."""

    STATUS_QUEUED = "queued"
    STATUS_PROCESSING = "processing"
    STATUS_DRAFTED = "drafted"
    STATUS_NEEDS_REVIEW = "needs_review"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_QUEUED, "Queued"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_DRAFTED, "Drafted"),
        (STATUS_NEEDS_REVIEW, "Needs review"),
        (STATUS_FAILED, "Failed"),
    ]

    PROBLEM_TYPE_BUG = "bug"
    PROBLEM_TYPE_FEATURE = "feature"
    PROBLEM_TYPE_UPDATE = "update"
    PROBLEM_TYPE_SECURITY_FIX = "security_fix"
    PROBLEM_TYPE_SCALING = "scaling"
    PROBLEM_TYPE_NEW_CONFIG = "new_config"
    PROBLEM_TYPE_OPS_ACTION = "ops_action"
    PROBLEM_TYPE_CHOICES = [
        (PROBLEM_TYPE_BUG, "Bug"),
        (PROBLEM_TYPE_FEATURE, "Feature"),
        (PROBLEM_TYPE_UPDATE, "Update"),
        (PROBLEM_TYPE_SECURITY_FIX, "Security Fix"),
        (PROBLEM_TYPE_SCALING, "Scaling"),
        (PROBLEM_TYPE_NEW_CONFIG, "New Config"),
        (PROBLEM_TYPE_OPS_ACTION, "Ops Action"),
    ]

    ticket = models.OneToOneField(TicketSnapshot, on_delete=models.CASCADE, related_name="closed_summary")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_QUEUED)

    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    model_used = models.CharField(
        max_length=100, blank=True,
        help_text="Snapshot of settings.OLLAMA_MODEL at run time, same idea as TicketAudit.model_used.",
    )

    problem_source = models.CharField(
        max_length=255, blank=True,
        help_text="Short label naming the subsystem/technical area, e.g. 'Storage', 'Cloudstack'. "
        "AI-drafted, human-editable.",
    )
    problem_type = models.CharField(max_length=20, choices=PROBLEM_TYPE_CHOICES, blank=True)
    problem_root_cause = models.TextField(blank=True)
    fixed_on = models.TextField(
        blank=True, help_text="How it was actually resolved and what was communicated back to the client.",
    )

    raw_response = models.TextField(
        blank=True, help_text="Full Ollama reply, kept for debugging needs_review/failed rows.",
    )
    error_message = models.TextField(blank=True)

    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="reviewed_closed_ticket_summaries",
    )

    class Meta:
        ordering = ["-queued_at"]
        verbose_name_plural = "Closed ticket summaries"

    def __str__(self):
        return f"Closed-ticket summary for ticket {self.ticket_id} ({self.status})"


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
        when = timezone.localtime(self.uploaded_at).strftime("%Y-%m-%d %H:%M")
        return f"{self.project.name} dump ({when}) - {self.status}"


class ProjectDeletionLog(models.Model):
    """Immutable audit trail for the 'delete project and everything under it' admin
    action -- created ONLY by ProjectAdmin.delete_everything_view, in the same
    transaction as the project.delete() call it records, right before the Project row
    (and everything cascading from it) stops existing. No FK to Project -- that row is
    gone by the time this is ever read back -- so it stores a plain id/name snapshot
    instead."""

    project_id = models.PositiveIntegerField(
        help_text="The deleted Project's own id -- not a live FK, since that row no longer exists.",
    )
    project_name = models.CharField(max_length=255)
    deleted_at = models.DateTimeField(auto_now_add=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="project_deletions",
    )
    row_counts = models.JSONField(
        default=dict, blank=True,
        help_text="Model label -> row count deleted, snapshotted immediately before the delete.",
    )

    class Meta:
        ordering = ["-deleted_at"]

    def __str__(self):
        return f"Deleted '{self.project_name}' on {self.deleted_at:%Y-%m-%d %H:%M}"

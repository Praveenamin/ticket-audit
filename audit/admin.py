import csv
from datetime import datetime, time, timedelta

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.utils import display_for_field
from django.core.exceptions import PermissionDenied
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import NoReverseMatch, path, reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.html import format_html, format_html_join
from django.utils.text import capfirst

from . import reports
from .models import (
    Client, ClosedTicketSummary, Department, DumpUpload, Project, ProjectDeletionLog,
    SLAPolicy, TicketAudit, TicketEscalationAnalysis, TicketNote, TicketReply, TicketSnapshot,
)
from .closed_ticket_summary import average_processing_seconds, queue_closed_tickets_for_range
from .sla import breach_summary, explain_sla
from .whmcs_client import WHMCSAPIError, WHMCSClient


class TicketOpenedDateFilter(admin.SimpleListFilter):
    """Defaults to 'Last 7 days' on first load (not just an option) -- overrides
    value() so an absent query param behaves the same as explicitly selecting the
    default tier, instead of Django's usual unfiltered default."""

    title = "date opened"
    parameter_name = "date_range"
    default_value = "7d"
    RANGES = {
        "7d": ("Last 7 days", timedelta(days=7)),
        "30d": ("Last 30 days", timedelta(days=30)),
        "90d": ("Last 3 months", timedelta(days=90)),
        "all": ("All time", None),
    }

    def lookups(self, request, model_admin):
        return [(key, label) for key, (label, _delta) in self.RANGES.items()]

    def value(self):
        value = super().value()
        return value if value in self.RANGES else self.default_value

    def queryset(self, request, queryset):
        _label, delta = self.RANGES[self.value()]
        if delta is None:
            return queryset
        return queryset.filter(opened_at__gte=timezone.now() - delta)

    def choices(self, changelist):
        for key, (label, _delta) in self.RANGES.items():
            yield {
                "selected": self.value() == key,
                "query_string": changelist.get_query_string({self.parameter_name: key}),
                "display": label,
            }


class ProjectScopedDepartmentFilter(admin.SimpleListFilter):
    """Same idea as SLAPolicyAdmin's dependent-dropdown JS, but for a list
    filter rather than a form field: Django's default filter for a `department`
    FK lists every department across every project, so it's easy to end up
    with a department selected that doesn't even belong to the also-selected
    project. Reads the same GET param the plain `project` list filter writes
    (Django's default for a FK field list filter) to narrow its own choices
    to just that project once one is picked; with no project picked, every
    department shows, same as the default behavior this replaces."""

    title = "department"
    parameter_name = "department__id__exact"

    def lookups(self, request, model_admin):
        departments = Department.objects.select_related("project").order_by("project__name", "name")
        project_id = request.GET.get("project__id__exact")
        if project_id:
            departments = departments.filter(project_id=project_id)
        return [(department.id, str(department)) for department in departments]

    def queryset(self, request, queryset):
        value = self.value()
        if value:
            queryset = queryset.filter(department__id=value)
        return queryset


class ReadOnlyAdminMixin:
    """These models are a mirror of data pulled from the WHMCS API -- editing
    them locally would only be overwritten by the next sync, and could mislead
    an auditor into thinking they'd changed something real."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


def _dump_upload_status_badge(obj):
    """Shared between DumpUploadAdmin (list + detail) and DumpUploadInline --
    a spinner while queued/processing (marked with a class dump_upload_
    progress.js polls for, auto-reloading the page until it's gone) rather
    than plain status text with no visual cue that something's in flight."""
    if obj.status in (DumpUpload.STATUS_QUEUED, DumpUpload.STATUS_PROCESSING):
        return format_html(
            '<span class="dump-upload-in-progress">'
            '<span class="spinner-border spinner-border-sm text-primary" role="status"></span> {}'
            "</span>",
            obj.get_status_display(),
        )
    css_class = {
        DumpUpload.STATUS_DONE: "text-success",
        DumpUpload.STATUS_FAILED: "text-danger",
    }.get(obj.status, "")
    return format_html('<strong class="{}">{}</strong>', css_class, obj.get_status_display())


def _closed_ticket_summary_status_text(summary):
    """Plain-text status label -- the single source of truth for the
    status/reviewed-state branching, shared by the HTML badge below (which
    just wraps this in the right span/class) and the CSV export (which has
    no use for HTML at all). summary is None whenever a ticket hasn't been
    auto-queued yet (e.g. closed outside the queue window) -- a real,
    common case for this report, not an error."""
    if summary is None:
        return "Not queued"
    if summary.status in (ClosedTicketSummary.STATUS_QUEUED, ClosedTicketSummary.STATUS_PROCESSING):
        return summary.get_status_display()
    if summary.status == ClosedTicketSummary.STATUS_FAILED:
        return "Failed"
    if summary.status == ClosedTicketSummary.STATUS_NEEDS_REVIEW:
        return "Needs review (AI)"
    if summary.reviewed_at is None:
        return "Drafted -- awaiting review"
    return "Reviewed"


def _closed_ticket_summary_status_badge(summary):
    """Shared between the Closed Tickets Summary report and
    TicketSnapshotAdmin's cross-link display."""
    text = _closed_ticket_summary_status_text(summary)
    if summary is None or summary.status in (ClosedTicketSummary.STATUS_QUEUED, ClosedTicketSummary.STATUS_PROCESSING):
        return format_html('<span class="text-muted">{}</span>', text)
    if summary.status == ClosedTicketSummary.STATUS_FAILED:
        return format_html('<strong class="text-danger">{}</strong>', text)
    if summary.status == ClosedTicketSummary.STATUS_NEEDS_REVIEW:
        return format_html('<strong class="text-warning">{}</strong>', text)
    if summary.reviewed_at is None:
        return format_html('<strong class="text-info">{}</strong>', text)
    return format_html('<span class="text-success">{}</span>', text)


class DumpUploadInline(admin.TabularInline):
    """Read-only history of this project's dump uploads -- the actual upload
    action stays on DumpUpload's own add form (a TabularInline can't cleanly
    show "just a file picker" for a new row alongside read-only status/counts
    for past ones in the same formset, since Django renders every row through
    one shared form). ProjectAdmin.upload_dump_link is the actual trigger,
    deep-linking to that add form with this project preselected."""

    model = DumpUpload
    extra = 0
    fields = ("file", "status_display", "uploaded_at", "finished_at", "tickets_imported", "error_message")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    @admin.display(description="Status")
    def status_display(self, obj):
        return _dump_upload_status_badge(obj)


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """The WHMCS API fields only apply to source_type="api" projects -- the
    JS toggles them out of the form for "dump" projects (default and initial
    state on Add) since they'd otherwise be a confusing, unused prompt."""

    list_display = (
        "name", "source_type", "active", "s3_archive_enabled", "escalation_analysis_enabled", "created_at",
    )
    list_filter = ("source_type", "active", "s3_archive_enabled", "escalation_analysis_enabled")
    inlines = [DumpUploadInline]

    def has_delete_permission(self, request, obj=None):
        # Django's own standard delete flow can never actually succeed here anyway --
        # Department/TicketSnapshot always block the cascade (ReadOnlyAdminMixin) --
        # so leaving it enabled only means a plain "Delete" button sits right next to
        # delete_everything_link's "Delete project and everything under it" button,
        # identically styled, and easy to click by mistake (confirmed: a real user did
        # exactly this and got the old blocked-cascade error). Hiding it removes both
        # that button AND the "Delete selected projects" bulk changelist action (Django
        # checks this same method for both), leaving delete_everything_view as the one
        # real way to delete a project.
        return False

    class Media:
        js = (
            "audit/js/project_admin.js", "audit/js/dump_upload_progress.js",
            "audit/js/auto_submit_filters.js",
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is None or not obj.pk:
            return ()
        fields = ("upload_dump_link", "test_connectivity_link")
        if request.user.is_superuser:
            fields += ("delete_everything_link",)
        return fields

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # Stored as plain text (like whmcs_api_secret already is -- a secret that
        # must be sent back to AWS verbatim can't be hashed, so masking here is a
        # display concern only, not a storage one), but shouldn't be legible by
        # default on screen the way a plain CharField would render it.
        if db_field.name == "s3_secret_access_key":
            kwargs["widget"] = forms.PasswordInput(render_value=True)
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    @admin.display(description="Dump uploads")
    def upload_dump_link(self, obj):
        add_url = reverse("admin:audit_dumpupload_add")
        return format_html(
            '<a href="{}?project={}" class="btn btn-primary btn-sm">Upload new dump</a>',
            add_url, obj.pk,
        )

    @admin.display(description="API connectivity")
    def test_connectivity_link(self, obj):
        test_url = reverse("admin:audit_project_test_connectivity", args=[obj.pk])
        return format_html(
            '<a href="{}" class="btn btn-primary btn-sm">Test API connectivity</a>',
            test_url,
        )

    @admin.display(description="Danger zone")
    def delete_everything_link(self, obj):
        url = reverse("admin:audit_project_delete_everything", args=[obj.pk])
        return format_html(
            '<a href="{}" class="btn btn-danger btn-sm">Delete project and everything under it</a>',
            url,
        )

    def get_urls(self):
        custom = [
            path(
                "<int:project_id>/test-connectivity/",
                self.admin_site.admin_view(self.test_connectivity_view),
                name="audit_project_test_connectivity",
            ),
            path(
                "<int:project_id>/delete-everything/",
                self.admin_site.admin_view(self.delete_everything_view),
                name="audit_project_delete_everything",
            ),
        ]
        return custom + super().get_urls()

    def test_connectivity_view(self, request, project_id):
        # A plain GET, not a POST+confirm like run_ai_audit_view -- this
        # makes one read-only WHMCS API call and writes nothing locally, so
        # there's no queued job or double-submit risk to guard against.
        project = get_object_or_404(Project, pk=project_id)
        change_url = reverse("admin:audit_project_change", args=[project.pk])

        if project.source_type != Project.SOURCE_API:
            messages.error(request, "This project isn't API-sourced -- nothing to test.")
            return redirect(change_url)

        client = WHMCSClient(
            base_url=project.whmcs_base_url,
            identifier=project.whmcs_api_identifier,
            secret=project.whmcs_api_secret,
        )
        try:
            result = client.get_tickets_page(limitstart=0, limitnum=1)
        except WHMCSAPIError as exc:
            messages.error(request, f"Connection failed: {exc}")
            return redirect(change_url)

        if result.get("result") != "success":
            messages.error(
                request, f"WHMCS rejected the request: {result.get('message') or 'unknown error'}",
            )
        else:
            total = result.get("totalresults", "?")
            messages.success(
                request,
                f"Connected successfully -- WHMCS reports {total} ticket(s) visible to this API credential.",
            )
        return redirect(change_url)

    def _project_delete_counts(self, project):
        return {
            "Department": Department.objects.filter(project=project).count(),
            "Client": Client.objects.filter(project=project).count(),
            "SLA policy": SLAPolicy.objects.filter(project=project).count(),
            "Dump upload": DumpUpload.objects.filter(project=project).count(),
            "Ticket snapshot": TicketSnapshot.objects.filter(project=project).count(),
            "Ticket reply": TicketReply.objects.filter(ticket__project=project).count(),
            "Ticket note": TicketNote.objects.filter(ticket__project=project).count(),
            "AI ticket audit": TicketAudit.objects.filter(ticket__project=project).count(),
            "Closed ticket summary": ClosedTicketSummary.objects.filter(ticket__project=project).count(),
        }

    def delete_everything_view(self, request, project_id):
        """Hard-deletes a Project and every row that cascades from it, in THIS app's
        own Postgres database ONLY. Safety-by-inspection: this method and
        _project_delete_counts import nothing from .whmcs_client and never open a
        connection to the staging_db MySQL container dump_import.py uses. The only
        write performed is project.delete() -- a plain ORM call against this app's own
        tables. Every Project child is on_delete=CASCADE; WHMCSClient implements no
        write/delete action against WHMCS at all -- so there is no code path here, or
        anywhere in this repo, that could reach WHMCS's own database or tickets. See
        test_delete_everything_view_source_never_touches_whmcs_or_mysql in
        test_admin.py, which greps this method's own source for the relevant forbidden
        tokens so this stays true if anyone edits it later."""
        if not request.user.is_superuser:
            raise PermissionDenied("Deleting a project and all its data is restricted to superusers.")

        project = get_object_or_404(Project, pk=project_id)
        changelist_url = reverse("admin:audit_project_changelist")
        counts = self._project_delete_counts(project)
        total = sum(counts.values())

        if request.method == "POST":
            if request.POST.get("confirm_name", "").strip() != project.name:
                messages.error(request, "Typed name didn't match the project's name -- nothing was deleted.")
                return redirect(reverse("admin:audit_project_delete_everything", args=[project.pk]))

            # Snapshot before .delete() -- the project row (and its .name/.id
            # attribute access) won't exist once the cascade below returns.
            project_id_snapshot, project_name = project.id, project.name
            dump_file_names = list(
                DumpUpload.objects.filter(project=project).exclude(file="").values_list("file", flat=True)
            )

            with transaction.atomic():
                project.delete()
                ProjectDeletionLog.objects.create(
                    project_id=project_id_snapshot, project_name=project_name,
                    deleted_by=request.user, row_counts=counts,
                )

            # Best-effort only, after the DB transaction above has already committed --
            # a failure here doesn't undo or affect the delete, only leaves a file
            # behind (matters only for FAILED dump uploads; successful ones already
            # self-clean via process_dump_upload's own upload.file.delete(save=True)).
            removed_files = 0
            for name in dump_file_names:
                try:
                    if default_storage.exists(name):
                        default_storage.delete(name)
                        removed_files += 1
                except OSError:
                    pass

            messages.success(
                request,
                f"Deleted project '{project_name}' and {total} related row(s) "
                f"({removed_files} dump file(s) also removed from storage).",
            )
            return redirect(changelist_url)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Delete {project.name} and everything under it",
            "project": project,
            "counts": counts,
            "total": total,
            "changelist_url": changelist_url,
        }
        return TemplateResponse(request, "admin/audit/delete_project_confirm.html", context)


@admin.register(Department)
class DepartmentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("name", "project", "whmcs_deptid", "last_seen_at")
    list_filter = ("project",)
    ordering = ("project__name", "name")

    class Media:
        js = ("audit/js/auto_submit_filters.js",)


@admin.register(Client)
class ClientAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("name", "project", "email", "whmcs_client_id", "last_seen_at")
    list_filter = ("project",)
    search_fields = ("name", "email")
    ordering = ("project__name", "name")

    class Media:
        js = ("audit/js/auto_submit_filters.js",)


class DepartmentSelect(forms.Select):
    """Tags each <option> with its department's project id as a data-project
    attribute, so sla_policy_admin.js can hide every department that doesn't
    belong to the currently-selected project -- Django admin has no built-in
    dependent-dropdown support, so the filtering has to happen client-side."""

    def __init__(self, *args, department_projects=None, **kwargs):
        self.department_projects = department_projects or {}
        super().__init__(*args, **kwargs)

    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        project_id = self.department_projects.get(str(value))
        if project_id is not None:
            option["attrs"]["data-project"] = project_id
        return option


@admin.register(SLAPolicy)
class SLAPolicyAdmin(admin.ModelAdmin):
    list_display = (
        "name", "project", "department",
        "first_response_target_minutes", "resolution_target_minutes", "active",
    )
    list_filter = ("active", "project", "department")

    class Media:
        js = ("audit/js/sla_policy_admin.js", "audit/js/auto_submit_filters.js")

    def get_readonly_fields(self, request, obj=None):
        # Project/department are chosen when a policy is created but fixed
        # after that -- reassigning either later would silently change which
        # tickets it scores.
        if obj is None:
            return ()
        return ("project", "department")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # department is optional and genuinely means "all departments in
        # this project" when left blank (see its help_text) -- unlike
        # Django's generic "---------" placeholder for a required field
        # (project, unchanged below) where blank isn't a valid choice at all.
        if db_field.name == "department":
            kwargs["empty_label"] = "All"
            kwargs["widget"] = DepartmentSelect(
                department_projects={
                    str(dept_id): str(project_id)
                    for dept_id, project_id in Department.objects.values_list("id", "project_id")
                },
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


@admin.register(DumpUpload)
class DumpUploadAdmin(admin.ModelAdmin):
    """Upload IS the ingestion trigger (admin's default add-form gives us the
    project dropdown + file picker for free); the record becomes an immutable
    processing-status log entry once created -- its list/detail view is the
    status page (status/counts/error), refreshed by reloading the page."""

    list_display = ("project", "status_display", "uploaded_at", "finished_at", "tickets_imported")
    list_filter = ("status", "project")

    class Media:
        js = ("audit/js/dump_upload_progress.js", "audit/js/auto_submit_filters.js")

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("project", "file")
        return (
            "project", "file", "status_display", "uploaded_at", "started_at", "finished_at",
            "tickets_imported", "error_message",
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return (
            "project", "file", "status_display", "uploaded_at", "started_at", "finished_at",
            "tickets_imported", "error_message",
        )

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Status", ordering="status")
    def status_display(self, obj):
        return _dump_upload_status_badge(obj)


@admin.register(ProjectDeletionLog)
class ProjectDeletionLogAdmin(admin.ModelAdmin):
    """Immutable audit trail for ProjectAdmin.delete_everything_view -- read-only for
    everyone, including delete: unlike ClosedTicketSummary's "free redo" escape hatch,
    there's no legitimate redo case for a deleted project's own audit trail."""

    list_display = ("project_name", "deleted_at", "deleted_by", "total_rows_deleted")
    ordering = ("-deleted_at",)
    readonly_fields = ("project_id", "project_name", "deleted_at", "deleted_by", "row_counts")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Total rows deleted")
    def total_rows_deleted(self, obj):
        return sum(obj.row_counts.values())


class TicketReplyInline(admin.TabularInline):
    model = TicketReply
    extra = 0
    fields = ("posted_at", "author_type", "author_name", "admin_name", "message", "rating", "archived_to_s3_at")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


class TicketNoteInline(admin.TabularInline):
    """Internal, staff-only notes (tblticketnotes) -- never sent to the
    client, distinct from the reply thread above."""

    model = TicketNote
    extra = 0
    fields = ("posted_at", "admin_name", "message")
    readonly_fields = fields
    can_delete = False
    verbose_name_plural = "Internal notes (staff-only)"

    def has_add_permission(self, request, obj=None):
        return False


class TicketAuditInline(admin.TabularInline):
    """Read-only history of past AI audit runs -- the rich detail of the
    LATEST run lives in TicketSnapshotAdmin.ai_audit_display instead; this
    tab is just "what happened, when, with what verdict" at a glance."""

    model = TicketAudit
    extra = 0
    fields = ("requested_at", "status", "sentiment_label", "model_used", "finished_at")
    readonly_fields = fields
    can_delete = False
    # ai_audit_display only ever shows the LATEST run's full report -- this
    # lets a past run's row link through to its own change page (sentiment
    # summary, raw missed_queries/positives/negatives) instead of being a
    # dead-end row.
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(TicketSnapshot)
class TicketSnapshotAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "tid_display", "opened_at_display", "last_reply_at_display", "project", "department_display", "status",
        "operator_display", "sla_status_badge", "escalation_risk_badge", "breach_detail",
    )
    # None, not () -- Django's ModelAdmin default for list_display_links IS ()
    # already, and items_for_result() treats "falsy but not None" as "auto-link
    # the first column" (confirmed against Django's own source), so () was a
    # no-op: tid_display was still getting wrapped in a second, nested <a> to
    # the change page around its own modal-trigger link. Only None actually
    # suppresses it.
    list_display_links = None  # tid_display supplies its own click target (a modal, not the change page)
    list_filter = (
        TicketOpenedDateFilter, "project", ProjectScopedDepartmentFilter, "sla_status", "priority", "status",
        "escalation_analysis__sentiment_category",
    )
    search_fields = ("tid", "subject", "requestor_name", "requestor_email")
    readonly_fields = (
        "breach_explanation", "ai_audit_display", "closed_ticket_summary_display", "escalation_analysis_display",
        "details_display",
    )
    # Plain fields (not fieldsets) -- Jazzmin's horizontal_tabs template maps
    # every named fieldset to its OWN tab (confirmed against its own
    # horizontal_tabs.html), so a second fieldset for "Details" would render
    # as a sibling tab next to "General", not a section within it.
    # details_display builds the collapsible section itself instead.
    fields = (
        "breach_explanation", "ai_audit_display", "closed_ticket_summary_display", "escalation_analysis_display",
        "details_display",
    )
    inlines = [TicketReplyInline, TicketNoteInline, TicketAuditInline]

    class Media:
        js = (
            "audit/js/ai_audit_modal.js", "audit/js/ticket_breadcrumb.js",
            "audit/js/auto_submit_filters.js",
        )

    def get_queryset(self, request):
        # breach_detail()/operator_display()/tid_display()/ai_audit_display()
        # need ticket_replies/ai_audits -- prefetch once per page instead of
        # one query per row. select_related("closed_summary")/
        # select_related("escalation_analysis") for closed_ticket_summary_display/
        # escalation_risk_badge/escalation_analysis_display -- both OneToOne, so
        # each is a single JOIN, not a second query. select_related("project",
        # "department") for the list_display columns of the same names --
        # each was otherwise a fresh query per row (N+1), pre-existing before
        # department_display but worth fixing now that this method is
        # touched anyway.
        return super().get_queryset(request).select_related(
            "closed_summary", "escalation_analysis", "project", "department",
        ).prefetch_related("ticket_replies", "ticket_notes", "ai_audits")

    @admin.display(description="Ticket ID", ordering="tid")
    def tid_display(self, obj):
        modal_id = f"breach-modal-{obj.pk}"
        change_url = reverse("admin:audit_ticketsnapshot_change", args=[obj.pk])
        explanation_html = format_html_join("", "<p>{}</p>", ((line,) for line in explain_sla(obj)))
        return format_html(
            '<a href="#" data-bs-toggle="modal" data-bs-target="#{modal_id}">{tid}</a>'
            '<div class="modal fade app-modal" id="{modal_id}" tabindex="-1" aria-hidden="true">'
            '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">'
            '<div class="modal-header">'
            '<h5 class="modal-title">SLA breach explanation — {tid}</h5>'
            '<button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" aria-label="Close"></button>'
            "</div>"
            '<div class="modal-body">{explanation}</div>'
            '<div class="modal-footer">'
            '<a href="{change_url}" class="btn btn-primary btn-sm">View full ticket</a>'
            "</div>"
            "</div></div></div>",
            modal_id=modal_id, tid=obj.tid or obj.whmcs_ticket_id,
            explanation=explanation_html, change_url=change_url,
        )

    @staticmethod
    def _short_datetime(value):
        # Admin's default rendering ("Aug. 11, 2026, 5:14 p.m.") is long for
        # a dense list of tickets -- same local-time conversion (this app's
        # TIME_ZONE, Asia/Kolkata), just a compact "Y-m-d H:i" instead.
        return timezone.localtime(value).strftime("%Y-%m-%d %H:%M") if value else "-"

    @admin.display(description="Opened", ordering="opened_at")
    def opened_at_display(self, obj):
        return self._short_datetime(obj.opened_at)

    @admin.display(description="Last reply", ordering="last_reply_at")
    def last_reply_at_display(self, obj):
        return self._short_datetime(obj.last_reply_at)

    @admin.display(description="Department", ordering="department__name")
    def department_display(self, obj):
        # Department.__str__ deliberately includes "(Project name)" -- needed
        # in cross-project contexts like dropdowns/filters, but redundant
        # here since this list already has its own separate Project column.
        return obj.department.name if obj.department else "-"

    @admin.display(description="Operator")
    def operator_display(self, obj):
        for reply in reversed(list(obj.ticket_replies.all())):
            if reply.author_type == TicketReply.OPERATOR:
                return reply.admin_name or "-"
        return "-"

    @admin.display(description="Breach detail")
    def breach_detail(self, obj):
        return breach_summary(obj)

    @admin.display(description="SLA status", ordering="sla_status")
    def sla_status_badge(self, obj):
        url = reverse("admin:audit_ticketsnapshot_change", args=[obj.pk])
        css_class = {
            TicketSnapshot.SLA_BREACHED: "text-danger",
            TicketSnapshot.SLA_MET: "text-success",
        }.get(obj.sla_status)
        if css_class is None:
            return format_html(
                '<a href="{}" class="text-muted">{}</a>', url, self._pending_sla_label(obj)
            )
        return format_html(
            '<a href="{}"><strong class="{}">{}</strong></a>',
            url, css_class, obj.get_sla_status_display(),
        )

    @staticmethod
    def _pending_sla_label(obj):
        """sla_status is None for three distinct, legitimate reasons (see
        _overall_status in sla.py) -- previously all rendered as a bare "-",
        indistinguishable from a data problem. Spelling out which one avoids
        that; "On hold" checked first since it overrides everything else for
        display, same precedence _overall_status itself uses."""
        if obj.status == TicketSnapshot.ON_HOLD_STATUS:
            return "On hold"
        if obj.sla_policy_id is None:
            return "No policy"
        return "Pending"

    @admin.display(description="Escalation risk", ordering="escalation_analysis__escalation_risk_score")
    def escalation_risk_badge(self, obj):
        # getattr(..., None), not obj.escalation_analysis: a OneToOneField reverse
        # accessor raises RelatedObjectDoesNotExist (not just returns None) when no
        # row exists yet -- see the identical caveat on closed_ticket_summary_display.
        analysis = getattr(obj, "escalation_analysis", None)
        url = reverse("admin:audit_ticketsnapshot_change", args=[obj.pk])
        if analysis is None or analysis.status != TicketEscalationAnalysis.STATUS_DONE:
            label = "Not yet analyzed" if analysis is None else analysis.get_status_display()
            return format_html('<a href="{}" class="text-muted">{}</a>', url, label)
        emoji = {
            TicketEscalationAnalysis.SENTIMENT_CRITICAL: "🔴",
            TicketEscalationAnalysis.SENTIMENT_FRUSTRATED: "🟡",
            TicketEscalationAnalysis.SENTIMENT_HEALTHY: "🟢",
        }[analysis.sentiment_category]
        css_class = {
            TicketEscalationAnalysis.SENTIMENT_CRITICAL: "text-danger",
            TicketEscalationAnalysis.SENTIMENT_FRUSTRATED: "text-warning",
            TicketEscalationAnalysis.SENTIMENT_HEALTHY: "text-success",
        }[analysis.sentiment_category]
        return format_html(
            '<a href="{}"><strong class="{}">{} {}%</strong></a>',
            url, css_class, emoji, analysis.escalation_risk_score,
        )

    @admin.display(description="Escalation analysis")
    def escalation_analysis_display(self, obj):
        analysis = getattr(obj, "escalation_analysis", None)
        if analysis is None:
            return format_html("<p>Not yet analyzed -- this project may not have escalation analysis enabled.</p>")
        if analysis.status != TicketEscalationAnalysis.STATUS_DONE:
            detail = analysis.error_message or "in progress"
            return format_html("<p>Escalation analysis {}: {}</p>", analysis.get_status_display().lower(), detail)
        return format_html(
            "<p><strong>{}</strong> -- {}% escalation risk</p>"
            "<p>Primary frustration driver: {}</p>"
            "<p>{}</p>"
            "<p class='text-muted'>Model: {} -- last analyzed {}</p>",
            analysis.get_sentiment_category_display(), analysis.escalation_risk_score,
            analysis.get_frustration_driver_display() if analysis.frustration_driver else "(none)",
            analysis.justification,
            analysis.model_used or "(unknown)",
            timezone.localtime(analysis.finished_at).strftime("%Y-%m-%d %H:%M") if analysis.finished_at else "-",
        )

    # Every raw record field down through sla_status -- deliberately NOT a
    # second fieldset (see the "fields" comment above for why that renders
    # as a separate tab under horizontal_tabs). Collapsed by default: these
    # are reference data, not the reason anyone opens a ticket page.
    _DETAILS_FIELDS = (
        "project", "whmcs_ticket_id", "tid", "department", "client", "subject",
        "status", "priority", "requestor_name", "requestor_email", "opened_at",
        "last_reply_at", "first_response_at", "closed_at", "synced_at", "sla_policy",
        "first_response_met", "follow_up_met", "first_response_target_minutes_at_eval",
        "follow_up_target_minutes_at_eval", "sla_status", "s3_archive_eligible",
    )

    @admin.display(description="")
    def details_display(self, obj):
        rows = format_html_join("", "{}", ((self._details_row(obj, name),) for name in self._DETAILS_FIELDS))
        # Hidden marker, not a rendered row -- ticket_breadcrumb.js reads
        # this to replace the page breadcrumb's default "#TID - Subject"
        # (str(obj), from Jazzmin's change_form.html) with just the ID plus
        # a copy button. Reading this exact value instead of parsing the
        # breadcrumb's own rendered text avoids breaking on a subject that
        # happens to contain " - " itself.
        tid_marker = format_html(
            '<span id="ticket-tid-value" class="d-none">{}</span>', obj.tid or obj.whmcs_ticket_id
        )
        return format_html(
            '{}<a href="#" class="btn btn-outline-secondary btn-sm" data-bs-toggle="collapse" '
            'data-bs-target="#ticket-details-collapse" aria-expanded="false">Show details</a>'
            '<div class="collapse mt-3" id="ticket-details-collapse">{}</div>',
            tid_marker, rows,
        )

    @staticmethod
    def _details_row(obj, field_name):
        field = TicketSnapshot._meta.get_field(field_name)
        value = getattr(obj, field_name)
        if value is None or value == "":
            display = "-"
        elif field.is_relation:
            try:
                url = reverse(f"admin:audit_{field.related_model._meta.model_name}_change", args=[value.pk])
                display = format_html('<a href="{}">{}</a>', url, value)
            except NoReverseMatch:
                display = str(value)
        else:
            display = display_for_field(value, field, "-")
        help_html = (
            format_html('<div class="form-text text-muted">{}</div>', field.help_text)
            if field.help_text else ""
        )
        return format_html(
            '<div class="row mb-2"><div class="col-3 text-muted">{}</div>'
            '<div class="col-9">{}{}</div></div>',
            capfirst(field.verbose_name), display, help_html,
        )

    @admin.display(description="SLA breach explanation")
    def breach_explanation(self, obj):
        return format_html_join("", "<div>{}</div>", ((line,) for line in explain_sla(obj)))

    @admin.display(description="AI Audit")
    def ai_audit_display(self, obj):
        latest = obj.ai_audits.first()  # TicketAudit.Meta.ordering = ["-requested_at"]
        modal_id = f"ai-audit-modal-{obj.pk}"
        trigger_url = reverse("admin:audit_ticketsnapshot_run_ai_audit", args=[obj.pk])
        in_flight = latest is not None and latest.status in (
            TicketAudit.STATUS_QUEUED, TicketAudit.STATUS_PROCESSING
        )
        button = format_html(
            '<a href="#" class="btn btn-primary btn-sm" data-bs-toggle="modal" data-bs-target="#{}">'
            "Run AI Audit</a>",
            modal_id,
        )
        modal = self._ai_audit_modal(obj, modal_id, trigger_url, in_flight)

        if latest is None:
            result = format_html("<p>No AI audit has been run for this ticket yet.</p>")
        elif in_flight:
            result = format_html(
                "<p>AI audit {} -- refresh this page to check progress.</p>",
                latest.get_status_display().lower(),
            )
        elif latest.status in (TicketAudit.STATUS_FAILED, TicketAudit.STATUS_NEEDS_REVIEW):
            result = format_html(
                "<p>Last AI audit {}: {}</p>",
                latest.get_status_display().lower(), latest.error_message or "(no details)",
            )
        else:
            positives = format_html_join("", "<li>{}</li>", ((p,) for p in latest.positives))
            negatives = format_html_join("", "<li>{}</li>", ((n,) for n in latest.negatives))
            missed = format_html_join("", "<li>{}</li>", ((m,) for m in latest.missed_queries))
            result = format_html(
                "<p><strong>Sentiment:</strong> {sentiment} -- {summary}</p>"
                "<p><strong>Positives:</strong></p><ul>{positives}</ul>"
                "<p><strong>Negatives:</strong></p><ul>{negatives}</ul>"
                "<p><strong>Missed queries:</strong></p><ul>{missed}</ul>",
                sentiment=latest.get_sentiment_label_display(), summary=latest.sentiment_summary,
                positives=positives, negatives=negatives, missed=missed,
            )
        return format_html("{}{}{}", button, result, modal)

    @staticmethod
    def _ai_audit_modal(obj, modal_id, trigger_url, in_flight):
        """A <form> here would nest inside the change page's own outer
        <form> (invalid HTML -- the same class of bug the tid_display modal
        had with a nested <a>), so the confirm button POSTs via fetch()
        instead (ai_audit_modal.js), reading the CSRF token from the cookie
        Django's own change-form already sets on this same page."""
        if in_flight:
            body = "An AI audit is already running for this ticket. Wait for it to finish, then refresh this page."
            confirm_button = format_html(
                '<button type="button" class="btn btn-primary btn-sm" disabled>Run AI Audit</button>'
            )
        else:
            body = format_html(
                'Run an LLM-based audit (sentiment, missed queries, positives/negatives) for '
                'ticket #{} -- "{}"? This calls a local Ollama model and can take several minutes '
                "on longer threads; you'll stay on this page and can refresh to check progress.",
                obj.tid or obj.whmcs_ticket_id, obj.subject or "(no subject)",
            )
            confirm_button = format_html(
                '<button type="button" class="btn btn-primary btn-sm" data-ai-audit-confirm="{}">'
                "Run AI Audit</button>",
                trigger_url,
            )
        return format_html(
            '<div class="modal fade app-modal" id="{modal_id}" tabindex="-1" aria-hidden="true">'
            '<div class="modal-dialog modal-dialog-centered"><div class="modal-content">'
            '<div class="modal-header">'
            '<h5 class="modal-title">Run AI Audit</h5>'
            '<button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" '
            'aria-label="Close"></button>'
            "</div>"
            '<div class="modal-body"><p>{body}</p></div>'
            '<div class="modal-footer">'
            '<button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">Cancel</button>'
            "{confirm_button}"
            "</div>"
            "</div></div></div>",
            modal_id=modal_id, body=body, confirm_button=confirm_button,
        )

    @admin.display(description="Closed Ticket Summary")
    def closed_ticket_summary_display(self, obj):
        # OneToOne reverse accessor raises RelatedObjectDoesNotExist (a
        # subclass of AttributeError) when absent, rather than returning
        # None -- getattr(..., None) is required here, a bare obj.closed_summary
        # would throw for any ticket not yet auto-queued.
        summary = getattr(obj, "closed_summary", None)
        badge = _closed_ticket_summary_status_badge(summary)
        if summary is None:
            return format_html("<p>{}</p>", badge)
        edit_url = reverse("admin:audit_closedticketsummary_change", args=[summary.pk])
        return format_html(
            "<p>{}</p><p><a href='{}' class='btn btn-primary btn-sm'>View / edit summary</a></p>",
            badge, edit_url,
        )

    def get_urls(self):
        custom = [
            path(
                "reports/clients/",
                self.admin_site.admin_view(self.client_report_view),
                name="audit_report_clients",
            ),
            path(
                "reports/techs/",
                self.admin_site.admin_view(self.tech_report_view),
                name="audit_report_techs",
            ),
            path(
                "reports/top-tickets/",
                self.admin_site.admin_view(self.top_tickets_report_view),
                name="audit_report_top_tickets",
            ),
            path(
                "reports/closed-tickets/",
                self.admin_site.admin_view(self.closed_tickets_report_view),
                name="audit_report_closed_tickets",
            ),
            path(
                "reports/closed-tickets/queue/",
                self.admin_site.admin_view(self.queue_closed_tickets_view),
                name="audit_report_closed_tickets_queue",
            ),
            path(
                "reports/closed-tickets/export/",
                self.admin_site.admin_view(self.export_closed_tickets_csv_view),
                name="audit_report_closed_tickets_export",
            ),
            path(
                "reports/ratings/",
                self.admin_site.admin_view(self.ratings_report_view),
                name="audit_report_ratings",
            ),
            path(
                "<int:ticket_id>/run-ai-audit/",
                self.admin_site.admin_view(self.run_ai_audit_view),
                name="audit_ticketsnapshot_run_ai_audit",
            ),
        ]
        return custom + super().get_urls()

    def run_ai_audit_view(self, request, ticket_id):
        ticket = get_object_or_404(TicketSnapshot, pk=ticket_id)
        change_url = reverse("admin:audit_ticketsnapshot_change", args=[ticket.pk])
        in_flight = ticket.ai_audits.filter(
            status__in=[TicketAudit.STATUS_QUEUED, TicketAudit.STATUS_PROCESSING]
        ).first()

        if request.method == "POST":
            if in_flight:
                messages.info(request, "An AI audit is already running for this ticket.")
            else:
                TicketAudit.objects.create(ticket=ticket)
                messages.success(request, "AI audit queued -- refresh this page in a bit to see results.")
            return redirect(change_url)

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Run AI Audit",
            "ticket": ticket,
            "change_url": change_url,
            "in_flight": in_flight,
        }
        return TemplateResponse(request, "admin/audit/run_ai_audit_confirm.html", context)

    def _monthly_group_report(self, request, title, subtitle, query_fn):
        projects = list(Project.objects.all())
        project_id = request.GET.get("project")
        project_id = int(project_id) if project_id else (projects[0].id if projects else None)

        today = timezone.localdate()
        year = int(request.GET.get("year") or today.year)
        month = int(request.GET.get("month") or today.month)

        rows = query_fn(project_id, year, month) if project_id else []

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": title,
            "subtitle": subtitle,
            "projects": projects,
            "selected_project": project_id,
            "year": year,
            "month": month,
            "rows": rows,
        }
        return TemplateResponse(request, "admin/audit/report_group_counts.html", context)

    def client_report_view(self, request):
        return self._monthly_group_report(
            request, "Client Monthly Report", "Tickets opened", reports.client_monthly_report,
        )

    def tech_report_view(self, request):
        return self._monthly_group_report(
            request, "Tech Monthly Report", "Tickets worked", reports.tech_monthly_report,
        )

    def top_tickets_report_view(self, request):
        projects = list(Project.objects.all())
        project_id = request.GET.get("project")
        project_id = int(project_id) if project_id else (projects[0].id if projects else None)
        window = request.GET.get("window", "month")
        days = 7 if window == "week" else 30

        rows = reports.top_tickets_by_replies(project_id, days) if project_id else []

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Top Tickets by Replies",
            "projects": projects,
            "selected_project": project_id,
            "window": window,
            "rows": rows,
        }
        return TemplateResponse(request, "admin/audit/report_top_tickets.html", context)

    def _report_date_range(self, request):
        # Explicit "from / to" date-picker range, not a fixed preset -- defaults to
        # the last 30 days. Bad/partial input from the date inputs falls back to the
        # same default rather than erroring. Shared across every date-range report
        # (Closed Tickets Summary's own "closed from/to", Client Satisfaction
        # Ratings' "reply posted from/to") plus queue_closed_tickets_view, so the
        # "Queue for AI Summary" button always queues exactly the window its own
        # report is showing.
        today = timezone.localdate()
        date_from = parse_date(request.GET.get("date_from", "")) or (today - timedelta(days=30))
        date_to = parse_date(request.GET.get("date_to", "")) or today
        range_start = timezone.make_aware(datetime.combine(date_from, time.min))
        range_end = timezone.make_aware(datetime.combine(date_to, time.min)) + timedelta(days=1)
        return date_from, date_to, range_start, range_end

    CLOSED_TICKETS_PAGE_SIZES = ("50", "100", "500", "all")

    def closed_tickets_report_view(self, request):
        projects = list(Project.objects.all())
        project_id = request.GET.get("project")
        project_id = int(project_id) if project_id else (projects[0].id if projects else None)

        date_from, date_to, range_start, range_end = self._report_date_range(request)
        status_filter = request.GET.get("status") or ""
        page_size = request.GET.get("page_size") or "50"
        if page_size not in self.CLOSED_TICKETS_PAGE_SIZES:
            page_size = "50"

        all_rows = reports.closed_tickets_summary(
            project_id, range_start, range_end, status_filter=status_filter or None,
        ) if project_id else []
        rows = all_rows if page_size == "all" else all_rows[:int(page_size)]
        for row in rows:
            row["status_badge"] = _closed_ticket_summary_status_badge(row["summary"])

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Closed Tickets Summary",
            "projects": projects,
            "selected_project": project_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "status_filter": status_filter,
            "page_size": page_size,
            "page_size_choices": self.CLOSED_TICKETS_PAGE_SIZES,
            "total_count": len(all_rows),
            "shown_count": len(rows),
            "rows": rows,
        }
        return TemplateResponse(request, "admin/audit/report_closed_tickets.html", context)

    def export_closed_tickets_csv_view(self, request):
        # Same project/date-range/status resolution as the report itself, so
        # the export always matches what's currently filtered on screen --
        # fed by formaction on the same filter form, not a separate stale
        # link. Deliberately ignores page_size, though -- "export" means
        # every matching row, not just the current on-screen page.
        projects = list(Project.objects.all())
        project_id = request.GET.get("project")
        project_id = int(project_id) if project_id else (projects[0].id if projects else None)
        date_from, date_to, range_start, range_end = self._report_date_range(request)
        status_filter = request.GET.get("status") or ""

        rows = reports.closed_tickets_summary(
            project_id, range_start, range_end, status_filter=status_filter or None,
        ) if project_id else []
        project_name = next((p.name for p in projects if p.id == project_id), "export")

        response = HttpResponse(content_type="text/csv")
        filename = f"closed-tickets-{project_name}-{date_from}-to-{date_to}.csv".replace(" ", "-")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow([
            "S.No", "Client Name", "Ticket Number", "Opened", "Closed", "Short Description",
            "Problem Source", "Problem Type", "Problem Root Cause", "Fixed on", "Status",
        ])
        for i, row in enumerate(rows, start=1):
            ticket = row["ticket"]
            summary = row["summary"]
            writer.writerow([
                i,
                row["client_label"],
                ticket.tid or ticket.whmcs_ticket_id,
                ticket.opened_at.strftime("%Y-%m-%d") if ticket.opened_at else "",
                ticket.closed_at.strftime("%Y-%m-%d") if ticket.closed_at else "",
                ticket.subject,
                summary.problem_source if summary else "",
                summary.get_problem_type_display() if summary else "",
                summary.problem_root_cause if summary else "",
                summary.fixed_on if summary else "",
                _closed_ticket_summary_status_text(summary),
            ])
        return response

    def queue_closed_tickets_view(self, request):
        project_id = request.GET.get("project")
        project = get_object_or_404(Project, pk=project_id) if project_id else None

        date_from, date_to, range_start, range_end = self._report_date_range(request)
        report_url = (
            reverse("admin:audit_report_closed_tickets")
            + f"?project={project_id or ''}&date_from={date_from.isoformat()}&date_to={date_to.isoformat()}"
        )

        if project is None:
            messages.error(request, "Pick a project first.")
            return redirect(report_url)

        pending = TicketSnapshot.objects.filter(
            project=project, status="Closed",
            closed_at__gte=range_start, closed_at__lt=range_end,
            closed_summary__isnull=True,
        ).count()

        if request.method == "POST":
            created = queue_closed_tickets_for_range(project.id, range_start, range_end)
            messages.success(
                request,
                f"Queued {created} closed-ticket summary/summaries for {project.name}. "
                "They process one at a time in the background -- refresh this report to check progress.",
            )
            return redirect(report_url)

        avg_seconds = average_processing_seconds()
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Queue Closed Tickets for AI Summary",
            "project": project,
            "date_from": date_from,
            "date_to": date_to,
            "pending": pending,
            "eta_hours": round(pending * avg_seconds / 3600, 1),
            "report_url": report_url,
        }
        return TemplateResponse(request, "admin/audit/queue_closed_tickets_confirm.html", context)

    def ratings_report_view(self, request):
        projects = list(Project.objects.all())
        project_id = request.GET.get("project")
        project_id = int(project_id) if project_id else (projects[0].id if projects else None)

        date_from, date_to, range_start, range_end = self._report_date_range(request)
        rows = reports.rated_replies_report(project_id, range_start, range_end) if project_id else []

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": "Client Satisfaction Ratings",
            "projects": projects,
            "selected_project": project_id,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "rows": rows,
        }
        return TemplateResponse(request, "admin/audit/report_ratings.html", context)


@admin.register(TicketAudit)
class TicketAuditAdmin(admin.ModelAdmin):
    """Every AI audit across every ticket, filterable by status/sentiment --
    mirrors DumpUploadAdmin: read-only, creation only happens via the
    trigger view on the ticket detail page, never here."""

    list_display = ("ticket_display", "status", "sentiment_label", "requested_at", "finished_at")
    list_filter = ("status", "sentiment_label")
    # None, not the default -- see the identical fix on TicketSnapshotAdmin.
    # ticket_display supplies its own link (to this row's own change page,
    # not the ticket's), so Django's auto-link-first-column default would
    # otherwise wrap it in a second, nested <a>.
    list_display_links = None

    class Media:
        js = ("audit/js/auto_submit_filters.js",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Ticket", ordering="ticket")
    def ticket_display(self, obj):
        url = reverse("admin:audit_ticketaudit_change", args=[obj.pk])
        return format_html("<a href=\"{}\">#{}</a>", url, obj.ticket.tid or obj.ticket.whmcs_ticket_id)


@admin.register(TicketEscalationAnalysis)
class TicketEscalationAnalysisAdmin(admin.ModelAdmin):
    """Every escalation analysis across every ticket, project-wide, filterable by
    sentiment/project and sortable by risk score -- "show me every Critical ticket
    across the whole account," not just one at a time (TicketSnapshotAdmin's own
    escalation_risk_badge column covers the per-ticket-list view). Read-only,
    mirrors TicketAuditAdmin exactly: creation only ever happens via sync.py's hook,
    never here."""

    list_display = (
        "ticket_display", "project_display", "sentiment_category", "escalation_risk_score",
        "frustration_driver", "status", "finished_at",
    )
    list_filter = ("sentiment_category", "frustration_driver", "status", "ticket__project")
    ordering = ("-escalation_risk_score",)
    list_display_links = None

    class Media:
        js = ("audit/js/auto_submit_filters.js",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("ticket", "ticket__project")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    @admin.display(description="Ticket", ordering="ticket")
    def ticket_display(self, obj):
        url = reverse("admin:audit_ticketescalationanalysis_change", args=[obj.pk])
        return format_html("<a href=\"{}\">#{}</a>", url, obj.ticket.tid or obj.ticket.whmcs_ticket_id)

    @admin.display(description="Project", ordering="ticket__project__name")
    def project_display(self, obj):
        return obj.ticket.project.name


@admin.register(ClosedTicketSummary)
class ClosedTicketSummaryAdmin(admin.ModelAdmin):
    """Unlike every other AI-audit-style model in this app, this ONE is
    meant to be edited -- the AI drafts problem_source/type/root_cause/
    fixed_on, a human reviews and corrects them here before they're final
    (an inline on TicketSnapshotAdmin can't do this: ReadOnlyAdminMixin's
    has_change_permission=False blocks the whole page's POST regardless of
    what an inline itself would allow, confirmed against
    test_change_view_post_denied)."""

    list_display = ("ticket_display", "status", "problem_type", "reviewed_indicator", "queued_at")
    list_filter = ("status", "problem_type")
    search_fields = ("ticket__tid", "ticket__subject", "problem_source")
    ordering = ("-queued_at",)
    list_display_links = None  # ticket_display supplies its own link, see TicketAuditAdmin's identical note
    fields = (
        "ticket", "status", "problem_source", "problem_type", "problem_root_cause", "fixed_on",
        "reviewed_by", "reviewed_at", "queued_at", "started_at", "finished_at", "model_used",
        "raw_response", "error_message",
    )
    readonly_fields = (
        "ticket", "status", "reviewed_by", "reviewed_at", "queued_at", "started_at", "finished_at",
        "model_used", "raw_response", "error_message",
    )

    class Media:
        js = ("audit/js/auto_submit_filters.js",)

    # Rows only ever come from queue_recent_closed_tickets() -- never a
    # generic add form, mirrors DumpUploadAdmin/TicketAuditAdmin's
    # "creation only via a specific trigger" rule. has_delete_permission is
    # left at its default (True) deliberately: deleting a bad row un-sets
    # closed_summary__isnull, so the next scheduler tick just regenerates
    # and reprocesses it -- a free "redo" escape hatch.
    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        # Opening this change form and clicking Save IS the review step --
        # no separate checkbox needed.
        obj.reviewed_at = timezone.now()
        obj.reviewed_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Ticket", ordering="ticket__tid")
    def ticket_display(self, obj):
        url = reverse("admin:audit_closedticketsummary_change", args=[obj.pk])
        return format_html("<a href=\"{}\">#{}</a>", url, obj.ticket.tid or obj.ticket.whmcs_ticket_id)

    @admin.display(description="Reviewed", boolean=True)
    def reviewed_indicator(self, obj):
        return obj.reviewed_at is not None


def _dashboard_is_ticket_snapshots(request, extra_context=None):
    """This app has no generic dashboard content worth landing on --
    "Dashboard" (admin:index, the sidebar's top link and /admin/ itself)
    goes straight to Ticket Snapshots instead."""
    return redirect("admin:audit_ticketsnapshot_changelist")


admin.site.index = _dashboard_is_ticket_snapshots

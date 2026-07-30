from datetime import timedelta

from django.contrib import admin
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html, format_html_join

from .models import Department, DumpUpload, Project, SLAPolicy, TicketReply, TicketSnapshot
from .sla import explain_sla


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


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """The WHMCS API fields only apply to source_type="api" projects -- the
    JS toggles them out of the form for "dump" projects (default and initial
    state on Add) since they'd otherwise be a confusing, unused prompt."""

    list_display = ("name", "source_type", "active", "created_at")
    list_filter = ("source_type", "active")

    class Media:
        js = ("audit/js/project_admin.js",)


@admin.register(Department)
class DepartmentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("name", "project", "whmcs_deptid", "last_seen_at")
    list_filter = ("project",)
    ordering = ("project__name", "name")


@admin.register(SLAPolicy)
class SLAPolicyAdmin(admin.ModelAdmin):
    list_display = (
        "name", "project", "department",
        "first_response_target_minutes", "resolution_target_minutes", "active",
    )
    list_filter = ("active", "project", "department")

    def get_readonly_fields(self, request, obj=None):
        # Project/department are chosen when a policy is created but fixed
        # after that -- reassigning either later would silently change which
        # tickets it scores.
        if obj is None:
            return ()
        return ("project", "department")


@admin.register(DumpUpload)
class DumpUploadAdmin(admin.ModelAdmin):
    """Upload IS the ingestion trigger (admin's default add-form gives us the
    project dropdown + file picker for free); the record becomes an immutable
    processing-status log entry once created -- its list/detail view is the
    status page (status/counts/error), refreshed by reloading the page."""

    list_display = ("project", "status", "uploaded_at", "finished_at", "tickets_imported")
    list_filter = ("status", "project")

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("project", "file")
        return (
            "project", "file", "status", "uploaded_at", "started_at", "finished_at",
            "tickets_imported", "error_message",
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return (
            "project", "file", "status", "uploaded_at", "started_at", "finished_at",
            "tickets_imported", "error_message",
        )

    def has_change_permission(self, request, obj=None):
        return False


class TicketReplyInline(admin.TabularInline):
    model = TicketReply
    extra = 0
    fields = ("posted_at", "author_type", "author_name", "admin_name", "message")
    readonly_fields = fields
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(TicketSnapshot)
class TicketSnapshotAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = (
        "tid", "subject", "project", "department", "priority", "status", "sla_status_badge",
    )
    list_filter = (TicketOpenedDateFilter, "sla_status", "project", "department", "priority", "status")
    search_fields = ("tid", "subject", "requestor_name", "requestor_email")
    readonly_fields = ("breach_explanation",)
    inlines = [TicketReplyInline]

    @admin.display(description="SLA status", ordering="sla_status")
    def sla_status_badge(self, obj):
        url = reverse("admin:audit_ticketsnapshot_change", args=[obj.pk])
        css_class = {
            TicketSnapshot.SLA_BREACHED: "text-danger",
            TicketSnapshot.SLA_MET: "text-success",
        }.get(obj.sla_status)
        if css_class is None:
            return format_html('<a href="{}">-</a>', url)
        return format_html(
            '<a href="{}"><strong class="{}">{}</strong></a>',
            url, css_class, obj.get_sla_status_display(),
        )

    @admin.display(description="SLA breach explanation")
    def breach_explanation(self, obj):
        return format_html_join("", "<div>{}</div>", ((line,) for line in explain_sla(obj)))

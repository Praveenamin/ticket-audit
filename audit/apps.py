from django.apps import AppConfig
from django.contrib.auth.apps import AuthConfig


class AuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "audit"


class UserManagementConfig(AuthConfig):
    """Renames the admin sidebar's "Authentication and Authorization" group
    -- this app only ever uses auth for staff login/permissions, "User
    Management" is what that group actually means here."""

    verbose_name = "User Management"

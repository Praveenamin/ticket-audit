import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "django-insecure-change-this-in-production")
DEBUG = os.environ.get("DEBUG", "False") == "True"
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0").split(",")

INSTALLED_APPS = [
    "jazzmin",
    "audit",
    "django.contrib.admin",
    "audit.apps.UserManagementConfig",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "ticket_audit.urls"

LOGIN_REDIRECT_URL = "/admin/"
LOGIN_URL = "/admin/login/"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # Checked before any app's own bundled templates (including
        # jazzmin's, which is listed before "audit" in INSTALLED_APPS) --
        # lets templates/admin/filter.html override Jazzmin's own copy.
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "ticket_audit.wsgi.application"

# Database
# All connection details come from environment variables so they stay in sync
# with docker-compose.yml and .env.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "whmcsaudit_db"),
        "USER": os.environ.get("POSTGRES_USER", "whmcsaudit_user"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "changeme"),
        "HOST": os.environ.get("POSTGRES_HOST", "localhost"),
        "PORT": os.environ.get("POSTGRES_PORT", "5434"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# Data is stored in UTC regardless (USE_TZ=True); this only controls display
# conversion -- the business and every ticket timestamp we compare against
# (WHMCS is IST) is India-based, so admin-rendered datetimes should read IST.
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# WHMCS dump uploads are 500MB+ -- this is an internal single-admin tool, not
# public-facing, so disabling Django's default upload-size guard is an
# acceptable tradeoff for accepting them.
DATA_UPLOAD_MAX_MEMORY_SIZE = None

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# WHMCS API (only used by Project rows with source_type="api"; credentials are
# stored per-project in the DB, these are just fallback defaults for the
# original Dev project, kept in sync with docker-compose.yml/.env)
WHMCS_BASE_URL = os.environ.get("WHMCS_BASE_URL", "").rstrip("/")
WHMCS_API_IDENTIFIER = os.environ.get("WHMCS_API_IDENTIFIER", "")
WHMCS_API_SECRET = os.environ.get("WHMCS_API_SECRET", "")
WHMCS_API_TIMEOUT = int(os.environ.get("WHMCS_API_TIMEOUT", "30"))

# Staging MySQL used to transform an uploaded WHMCS dump before deriving this
# app's own Postgres-backed models -- internal-only service, see
# docker-compose.yml's `staging_db`.
STAGING_DB_HOST = os.environ.get("STAGING_DB_HOST", "staging_db")
STAGING_DB_PORT = int(os.environ.get("STAGING_DB_PORT", "3306"))
STAGING_DB_ROOT_USER = os.environ.get("STAGING_DB_ROOT_USER", "root")
STAGING_DB_ROOT_PASSWORD = os.environ.get("STAGING_DB_ROOT_PASSWORD", "staging")

# Ollama (added in Phase 2 for AI-assisted rubric scoring)
OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "600"))

# Auto-queue window for Closed Tickets Summary -- intentionally narrow at
# first launch (Aug 2026); widen via this env var/setting without a code
# change, not by hardcoding the window anywhere else.
CLOSED_TICKET_SUMMARY_QUEUE_WINDOW_DAYS = int(
    os.environ.get("CLOSED_TICKET_SUMMARY_QUEUE_WINDOW_DAYS", "3")
)

# Redis
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
        },
    }
}

# Security settings
if not DEBUG:
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"

CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "http://localhost:8010").split(",")
    if origin.strip()
]

USE_TLS = os.environ.get("USE_TLS", "False") == "True"
if USE_TLS or os.environ.get("BEHIND_PROXY", "False") == "True":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{levelname} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"level": "INFO", "class": "logging.StreamHandler", "formatter": "simple"},
    },
    "loggers": {
        "audit": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
    "root": {"handlers": ["console"], "level": "INFO"},
}

JAZZMIN_SETTINGS = {
    "site_title": "WHMCS Ticket Audit",
    "site_header": "Ticket Audit",
    "site_brand": "Ticket Audit",
    # Global navbar search, next to the sidebar-toggle hamburger -- same
    # spot and same "always there" behavior on every page, not just the
    # Ticket Snapshots changelist's own (now-hidden, see search_form.html)
    # inline search box. Searches TicketSnapshotAdmin.search_fields (tid,
    # subject, requestor name/email).
    "search_model": "audit.TicketSnapshot",
    # Was "dark" (not a real theme name -- silently fell back to "default")
    # then briefly corrected to "darkly" -- reverted back to "default" (the
    # light theme this app has actually been running/tuned against) per
    # explicit request. Only the sidebar/navbar are dark, via UI-tweaks below.
    "theme": "default",
    "welcome_sign": "Welcome to the WHMCS Ticket Audit System",
    "copyright": "Ticket Audit",
    "show_sidebar": True,
    "navigation_expanded": True,
    # "Reports" isn't an installed app -- it's the custom_links key below,
    # which Jazzmin still treats as a sortable top-level sidebar group. The
    # audit.* entries order that app's own models (Jazzmin matches both the
    # bare app label and "app.model" prefixes out of this same list).
    "order_with_respect_to": [
        "audit", "audit.project", "audit.slapolicy", "audit.client", "audit.department",
        "audit.closedticketsummary", "Reports", "auth",
    ],
    "changeform_format": "horizontal_tabs",
    # Ticket Snapshots is reached via "Dashboard" now, and Dump Uploads is
    # managed from within its Project (ProjectAdmin.upload_dump_link +
    # DumpUploadInline) -- hiding both from the Audit app's own sidebar list
    # so they aren't listed twice. Still fully functional (admin registration
    # untouched), just not in this menu.
    "hide_models": ["audit.ticketsnapshot", "audit.dumpupload"],
    # Without this, every Audit-app sidebar entry falls back to Jazzmin's
    # generic default (a plain dot) -- distinct, purpose-matched icons per
    # model, one visual signature borrowed from the Stack Alert reference
    # design's sidebar (grouped sections, each row with its own crisp icon
    # rather than a repeated placeholder glyph).
    "icons": {
        "audit.project": "fas fa-building",
        "audit.slapolicy": "fas fa-gauge-high",
        "audit.client": "fas fa-user-tie",
        "audit.department": "fas fa-sitemap",
        "audit.closedticketsummary": "fas fa-robot",
        "audit.ticketaudit": "fas fa-magnifying-glass-chart",
        "audit.ticketescalationanalysis": "fas fa-triangle-exclamation",
    },
    # "Reports" doesn't match any installed app label, so Jazzmin renders it
    # as its own sidebar group rather than appending to an existing one.
    # The dict key IS the displayed sidebar-group label verbatim (Jazzmin
    # applies no capitalization) -- confirmed via the rendered HTML, which is
    # why this must be spelled exactly as it should appear, not lowercased.
    "custom_links": {
        "Reports": [
            {
                "name": "Client Monthly Report",
                "url": "admin:audit_report_clients",
                "icon": "fas fa-users",
            },
            {
                "name": "Tech Monthly Report",
                "url": "admin:audit_report_techs",
                "icon": "fas fa-user-cog",
            },
            {
                "name": "Top Tickets by Replies",
                "url": "admin:audit_report_top_tickets",
                "icon": "fas fa-fire",
            },
            {
                "name": "Closed Tickets Summary",
                "url": "admin:audit_report_closed_tickets",
                "icon": "fas fa-clipboard-check",
            },
            {
                "name": "Client Satisfaction Ratings",
                "url": "admin:audit_report_ratings",
                "icon": "fas fa-star",
            },
        ],
    },
    # Re-themed to match StackSense's design tokens (Inter/JetBrains Mono,
    # sky-blue primary, slate-900 sidebar) -- see the CSS file for the
    # actual color/font values.
    "custom_css": "audit/css/stacksense_theme.css",
    # Moves the logged-in username from the sidebar's user-panel (hidden via
    # CSS below) to visible text next to the top-right user-menu icon.
    "custom_js": "audit/js/navbar_user.js",
    "use_google_fonts_cdn": False,  # the custom CSS supplies Inter/JetBrains Mono itself
}

JAZZMIN_UI_TWEAKS = {
    # No navbar override -- the navbar template renders bg-body (light,
    # matching the "default" theme above), and "navbar-dark" sets icon/text
    # color to near-white for a DARK background, fighting it -- confirmed via
    # adminlte.min.css: that's exactly why the top-right user icon vanished
    # (white on white). Bootstrap 5 here doesn't even ship .navbar-light --
    # the plain, unmodified .navbar already defaults to dark-on-light colors.
    "sidebar": "sidebar-dark-primary",
    "accent": "accent-primary",
    # A second, independent "theme" setting from JAZZMIN_SETTINGS["theme"]
    # above -- get_ui_tweaks() reads its OWN copy from here and silently
    # defaults to "default" (light) if this key is absent, regardless of
    # what JAZZMIN_SETTINGS says. This is what the body's "theme-*" class
    # and the bootswatch <link> actually key off. Reverted to "default"
    # alongside the other one -- see comment there.
    "theme": "default",
}

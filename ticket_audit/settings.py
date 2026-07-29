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
    "django.contrib.auth",
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
        "DIRS": [],
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
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# WHMCS API
# REST-style action API at {WHMCS_BASE_URL}/includes/api.php, authenticated with
# an Identifier/Secret pair created under Setup > API Credentials in WHMCS.
WHMCS_BASE_URL = os.environ.get("WHMCS_BASE_URL", "").rstrip("/")
WHMCS_API_IDENTIFIER = os.environ.get("WHMCS_API_IDENTIFIER", "")
WHMCS_API_SECRET = os.environ.get("WHMCS_API_SECRET", "")
WHMCS_API_TIMEOUT = int(os.environ.get("WHMCS_API_TIMEOUT", "30"))

# Ollama (added in Phase 2 for AI-assisted rubric scoring)
OLLAMA_API_URL = os.environ.get("OLLAMA_API_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:latest")
OLLAMA_TIMEOUT = int(os.environ.get("OLLAMA_TIMEOUT", "120"))

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
    "theme": "dark",
    "welcome_sign": "Welcome to the WHMCS Ticket Audit System",
    "copyright": "Ticket Audit",
    "show_sidebar": True,
    "navigation_expanded": True,
    "order_with_respect_to": ["auth", "audit"],
    "changeform_format": "horizontal_tabs",
}

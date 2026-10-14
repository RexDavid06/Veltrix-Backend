from __future__ import annotations

from pathlib import Path
import os
import environ
import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from datetime import timedelta

BASE_DIR = Path(__file__).resolve().parent.parent

# Load variables from a local, git-ignored .env file (real values come from the
# process environment in production). read_env never overwrites real env vars.
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
DEBUG = os.environ.get("DEBUG", "1") == "1"
# Read ALLOWED_HOSTS from the environment (comma-separated). If the variable is
# not provided (e.g. Render dashboard), fall back to a safe default that keeps
# localhost and the known Render hostname for this deployment. Prefer an
# explicitly-provided ALLOWED_HOSTS so operators can override or add hosts.
_raw_allowed = os.environ.get("ALLOWED_HOSTS")
# Render provides RENDER_EXTERNAL_HOSTNAME in some environments; prefer adding
# it when present so the deployment's external hostname is always allowed.
_render_host = os.environ.get("RENDER_EXTERNAL_HOSTNAME")
if _raw_allowed:
    ALLOWED_HOSTS = [item.strip() for item in _raw_allowed.split(",") if item.strip()]
    # Ensure the Render-provided host is present when available.
    if _render_host and _render_host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(_render_host)
else:
    # If no explicit ALLOWED_HOSTS are provided, include localhost + Render
    # hostname (or the known deployment hostname as a safe fallback).
    _render_host = _render_host or "veltrix-backend-5cwa.onrender.com"
    ALLOWED_HOSTS = ["127.0.0.1", "localhost", _render_host]

# Deduplicate while preserving order to avoid accidental duplicates from env + render
_seen = set()
_deduped = []
for _h in ALLOWED_HOSTS:
    if _h not in _seen:
        _seen.add(_h)
        _deduped.append(_h)
if DEBUG and "*" not in _deduped:
    _deduped.append("*")
ALLOWED_HOSTS = _deduped

# Security: require SECRET_KEY in production
if not DEBUG and SECRET_KEY == "dev-insecure-secret-change-me":
    raise ImproperlyConfigured("SECRET_KEY must be set in production environment")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # third-party
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt",
    "channels",

    # local
    "veltrix_app",
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # whitenoise for static file serving in production
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "veltrix_app.middleware.AuditLogMiddleware",
]

ROOT_URLCONF = "veltrix_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
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

WSGI_APPLICATION = "veltrix_project.wsgi.application"
ASGI_APPLICATION = "veltrix_project.asgi.application"

# Database configuration (prefer DATABASE_URL)
DATABASE_URL = os.environ.get("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {"default": dj_database_url.parse(DATABASE_URL, conn_max_age=600)}
else:
    instance_dir = BASE_DIR / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": instance_dir / "veltrix.db"}}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# Static files (production)
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"] if (BASE_DIR / "static").exists() else []
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "veltrix_app.User"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

if not DEBUG:
    # Additional production hardening
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", 31536000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_BROWSER_XSS_FILTER = True
    X_FRAME_OPTIONS = "DENY"

MARKET_USE_LIVE = os.environ.get("MARKET_USE_LIVE", "0") == "1"
DEFAULT_MARKET_SYMBOLS = [
    "AAPL",
    "MSFT",
    "NVDA",
    "BTC-USD",
    "ETH-USD",
    "EURUSD=X",
    "XAUUSD=X",
]

# CORS
CORS_ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "http://127.0.0.1:5173").split(",") if o.strip()]
CORS_ALLOW_CREDENTIALS = True

# DRF and authentication
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "20/min",
        "user": "1000/day",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
}

# Email (send only if configured) - keep defaults to console for dev
EMAIL_BACKEND = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587") or 587)
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "noreply@veltrix.local")

# Frontend URL for links in emails
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# Basic logging
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "%(levelname)s %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
}

# Channels / Redis
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {
            "hosts": [REDIS_URL],
        },
    }
}

# Admin support emails (comma-separated)
SUPPORT_ADMIN_EMAILS = [e.strip() for e in os.environ.get("SUPPORT_ADMIN_EMAILS", "").split(",") if e.strip()]

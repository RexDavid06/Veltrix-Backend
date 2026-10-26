from __future__ import annotations

from pathlib import Path
import os
import sys
import environ
import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from datetime import timedelta
from decimal import Decimal, InvalidOperation

BASE_DIR = Path(__file__).resolve().parent.parent

# Load variables from a local, git-ignored .env file (real values come from the
# process environment in production). read_env never overwrites real env vars.
environ.Env.read_env(BASE_DIR / ".env")


def _non_negative_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a non-negative integer") from exc
    if value < 0:
        raise ImproperlyConfigured(f"{name} must be a non-negative integer")
    return value


def _non_negative_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ImproperlyConfigured(f"{name} must be a non-negative number") from exc
    if value < 0:
        raise ImproperlyConfigured(f"{name} must be a non-negative number")
    return value


def _optional_decimal(name: str) -> str:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return ""
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ImproperlyConfigured(f"{name} must be a non-negative decimal") from exc
    if not value.is_finite() or value < 0:
        raise ImproperlyConfigured(f"{name} must be a non-negative decimal")
    return raw

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-change-me")
_raw_debug = os.environ.get("DEBUG")
_DEBUG_CONFIGURED = _raw_debug is not None and bool(_raw_debug.strip())
if not _DEBUG_CONFIGURED:
    # Missing DEBUG must never turn on debug in a deployed service. Production
    # also requires a real SECRET_KEY below, so an incomplete deployment fails
    # closed rather than silently running with development settings.
    DEBUG = False
else:
    _normalized_debug = _raw_debug.strip().lower()
    if _normalized_debug in {"1", "true", "yes", "on"}:
        DEBUG = True
    elif _normalized_debug in {"0", "false", "no", "off"}:
        DEBUG = False
    else:
        raise ImproperlyConfigured("DEBUG must be an explicit boolean value.")
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
if not DEBUG and (not SECRET_KEY.strip() or SECRET_KEY == "dev-insecure-secret-change-me"):
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
    # Enables refresh-token blacklisting so rotation actually invalidates the
    # previous refresh token and logout can revoke a session server-side.
    "rest_framework_simplejwt.token_blacklist",
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
# STATICFILES_STORAGE was removed in Django 5.1 in favour of STORAGES; the old
# setting was silently ignored, so this restores the intended compressed+hashed
# static storage. WHITENOISE_MANIFEST_STRICT=False keeps a missing manifest
# entry from turning into a 500 (it falls back to the unhashed path).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MANIFEST_STRICT = False

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "veltrix_app.User"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_SSL_REDIRECT = not DEBUG
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"

# Production runs behind a TLS-terminating reverse proxy (Render), which forwards
# the original scheme via X-Forwarded-Proto. Without this, request.is_secure()
# stays False while SECURE_SSL_REDIRECT is on, causing an infinite redirect loop
# that breaks login/admin and prevents secure cookies from ever being set.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG:
    # Additional production hardening
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", 31536000))
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
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

# Demo (simulated) funding switch. This is the unbacked paper-trading top-up that
# mints fake USD into DemoAccount.cash_balance. It is DISABLED BY DEFAULT IN
# EVERY ENVIRONMENT: the only way to enable it is to set
# FUNDING_DEMO_TOPUP_ENABLED=1 explicitly (local development or tests). There is
# no implicit default or DEBUG-based fallback, so a production/deployment
# misconfiguration cannot silently re-enable it.
FUNDING_DEMO_TOPUP_ENABLED = os.environ.get("FUNDING_DEMO_TOPUP_ENABLED", "0") == "1"

# Production safety valve. Veltrix is a production financial application, so
# simulated funding must never be able to reach real customers. A deployment is
# treated as production when DEBUG is off OR when a real database is configured
# via DATABASE_URL (the Render / docker-compose production setup). In that case
# the flag is forced back off at load time regardless of the environment variable,
# so an accidental FUNDING_DEMO_TOPUP_ENABLED=1 in production can never mint
# artificial balances. Demo-mode tests opt in explicitly via override_settings,
# which bypasses this load-time guard.
_funding_is_production_deployment = (not DEBUG) or bool(os.environ.get("DATABASE_URL"))
if FUNDING_DEMO_TOPUP_ENABLED and _funding_is_production_deployment:
    FUNDING_DEMO_TOPUP_ENABLED = False

# Real deposits remain disabled until an operator completes provider, wallet,
# network, webhook and settlement verification. Never infer enablement from DEBUG.
FUNDING_DEPOSITS_ENABLED = os.environ.get("FUNDING_DEPOSITS_ENABLED", "0") == "1"
NOWPAYMENTS_ENABLED = os.environ.get("NOWPAYMENTS_ENABLED", "0") == "1"
# Direct TRON deposits are a separate, opt-in rail. Only a public, business
# controlled receiving address is configured here: this application never
# receives, derives, stores or signs with private key material.
CRYPTO_DEPOSIT_MODE = os.environ.get("CRYPTO_DEPOSIT_MODE", "direct_tron").strip().lower()
if CRYPTO_DEPOSIT_MODE not in ("nowpayments", "direct_tron"):
    raise ImproperlyConfigured("CRYPTO_DEPOSIT_MODE must be nowpayments or direct_tron")
TRON_DEPOSITS_ENABLED = os.environ.get("TRON_DEPOSITS_ENABLED", "0") == "1"
TRON_NETWORK = os.environ.get("TRON_NETWORK", "").strip().lower()
TRON_API_BASE_URL = os.environ.get("TRON_API_BASE_URL", "").strip().rstrip("/")
TRON_API_KEY = os.environ.get("TRON_API_KEY", "").strip()
# One shared receiving wallet for every client. Never invent or default this.
TRON_USDT_RECEIVING_ADDRESS = (
    os.environ.get("VELTRIX_USDT_TRC20_RECEIVING_ADDRESS", "").strip()
    or os.environ.get("TRON_USDT_RECEIVING_ADDRESS", "").strip()
)
# Optional override. On mainnet an empty value resolves to the pinned official
# Tether TRC-20 contract; a supplied value must match it exactly.
TRON_USDT_CONTRACT = os.environ.get("TRON_USDT_CONTRACT", "").strip()
TRON_REQUIRED_CONFIRMATIONS = _non_negative_int("TRON_REQUIRED_CONFIRMATIONS", 0)
TRON_LIVE_VERIFIED = os.environ.get("TRON_LIVE_VERIFIED", "0") == "1"
# Timestamp-based event scanning. TronGrid only supports min/max timestamp
# windows plus a fingerprint that must be replayed with identical parameters.
TRON_SCAN_START_TIMESTAMP = _non_negative_int("TRON_SCAN_START_TIMESTAMP", 0)
TRON_SCAN_WINDOW_SECONDS = _non_negative_int("TRON_SCAN_WINDOW_SECONDS", 3600)
TRON_SCAN_OVERLAP_SECONDS = _non_negative_int("TRON_SCAN_OVERLAP_SECONDS", 120)
TRON_SCAN_LAG_SECONDS = _non_negative_int("TRON_SCAN_LAG_SECONDS", 60)
TRON_HTTP_RETRIES = _non_negative_int("TRON_HTTP_RETRIES", 3)
TRON_HTTP_BACKOFF_SECONDS = _non_negative_float("TRON_HTTP_BACKOFF_SECONDS", 0.5)
# Operational job leases and alert thresholds. A crashed job releases its lock
# when the lease expires; heartbeats make a stopped scheduler visible.
TRON_JOB_LOCK_SECONDS = _non_negative_int("TRON_JOB_LOCK_SECONDS", 900)
TRON_MONITOR_STALE_SECONDS = _non_negative_int("TRON_MONITOR_STALE_SECONDS", 900)
TRON_RECONCILE_STALE_SECONDS = _non_negative_int("TRON_RECONCILE_STALE_SECONDS", 93600)
TRON_UNALLOCATED_ALERT_MINUTES = _non_negative_int("TRON_UNALLOCATED_ALERT_MINUTES", 60)
TRON_FAILURE_ALERT_THRESHOLD = _non_negative_int("TRON_FAILURE_ALERT_THRESHOLD", 3)
# A detected transfer that never reaches the confirmation threshold is moved
# to manual review after this many minutes. It is never credited automatically.
TRON_UNCONFIRMED_REVIEW_MINUTES = _non_negative_int("TRON_UNCONFIRMED_REVIEW_MINUTES", 1440)
if TRON_SCAN_WINDOW_SECONDS < 1:
    raise ImproperlyConfigured("TRON_SCAN_WINDOW_SECONDS must be at least 1")
if TRON_HTTP_RETRIES > 10:
    raise ImproperlyConfigured("TRON_HTTP_RETRIES must not exceed 10")
if TRON_JOB_LOCK_SECONDS < 60:
    raise ImproperlyConfigured("TRON_JOB_LOCK_SECONDS must be at least 60")
if TRON_MONITOR_STALE_SECONDS < 1:
    raise ImproperlyConfigured("TRON_MONITOR_STALE_SECONDS must be at least 1")
if TRON_RECONCILE_STALE_SECONDS < 1:
    raise ImproperlyConfigured("TRON_RECONCILE_STALE_SECONDS must be at least 1")
if TRON_FAILURE_ALERT_THRESHOLD < 1:
    raise ImproperlyConfigured("TRON_FAILURE_ALERT_THRESHOLD must be at least 1")
# Shared-address attribution is conservative. Unique payable amounts are an
# optional matching aid, not proof of ownership, and automatic crediting is a
# separate opt-in from automatic matching.
TRON_UNIQUE_AMOUNT_ENABLED = os.environ.get("TRON_UNIQUE_AMOUNT_ENABLED", "0") == "1"
TRON_AUTO_MATCH_UNIQUE_AMOUNT = os.environ.get("TRON_AUTO_MATCH_UNIQUE_AMOUNT", "0") == "1"
TRON_AUTO_CREDIT_UNIQUE_AMOUNT = os.environ.get("TRON_AUTO_CREDIT_UNIQUE_AMOUNT", "0") == "1"
TRON_REQUEST_TTL_MINUTES = _non_negative_int("TRON_REQUEST_TTL_MINUTES", 0)
TRON_MIN_DEPOSIT = _optional_decimal("TRON_MIN_DEPOSIT")
TRON_MAX_DEPOSIT = _optional_decimal("TRON_MAX_DEPOSIT")
if TRON_MIN_DEPOSIT and TRON_MAX_DEPOSIT and Decimal(TRON_MIN_DEPOSIT) > Decimal(TRON_MAX_DEPOSIT):
    raise ImproperlyConfigured("TRON_MIN_DEPOSIT must not exceed TRON_MAX_DEPOSIT")
# Manual bank transfers are a separate rail. The currency must be explicitly
# configured; account details are provided by an administrator outside the app.
BANK_TRANSFERS_ENABLED = os.environ.get("BANK_TRANSFERS_ENABLED", "0") == "1"
BANK_TRANSFER_CURRENCY = os.environ.get("BANK_TRANSFER_CURRENCY", "").strip().upper()
FUNDING_REQUIRE_KYC = os.environ.get("FUNDING_REQUIRE_KYC", "1") == "1"
NOWPAYMENTS_SANDBOX = os.environ.get("NOWPAYMENTS_SANDBOX", "0") == "1"
# Hard code this off until the submitted amount's USD pricing, USDT outcome,
# and ledger fee semantics have one approved and tested model. Environment
# configuration alone must not bypass that unresolved financial contract.
NOWPAYMENTS_LIVE_AMOUNT_MODEL_APPROVED = False
# Live production funding requires a separate operator attestation after wallet,
# callback, provider account and settlement have been verified. Sandbox remains
# available in non-production environments without this production switch.
FUNDING_LIVE_VERIFIED = os.environ.get("FUNDING_LIVE_VERIFIED", "0") == "1"
_funding_is_production = (not DEBUG) or bool(DATABASE_URL)
# Expose this to safety checks that must distinguish a local testnet rehearsal
# from a deployed production service. Testnet is never a production rail.
FUNDING_DEPLOYMENT_IS_PRODUCTION = _funding_is_production
_live_funding_requested = not NOWPAYMENTS_SANDBOX
_live_funding_approved = _DEBUG_CONFIGURED and not DEBUG and FUNDING_LIVE_VERIFIED
if ((_funding_is_production or _live_funding_requested) and not _live_funding_approved):
    FUNDING_DEPOSITS_ENABLED = False
    NOWPAYMENTS_ENABLED = False

# CORS - the deployed SPA and both local dev servers must always be allowed.
# Known app origins are MERGED with (not replaced by) CORS_ALLOWED_ORIGINS, so a
# stale or restrictive environment value can never break login/registration in
# production: without an Access-Control-Allow-Origin header the browser blocks
# the response and axios reports a bare "Network Error".
_default_cors_origins = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:5174",
    "http://localhost:5174",
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "https://veltrix-frontend-9mc7.onrender.com",
]
_env_cors_origins = [o.strip() for o in os.environ.get("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
CORS_ALLOWED_ORIGINS = list(dict.fromkeys([*_default_cors_origins, *_env_cors_origins]))
CORS_ALLOW_CREDENTIALS = True

# CSRF trusted origins (used by the Django admin / session views in production).
_raw_csrf = os.environ.get("CSRF_TRUSTED_ORIGINS", "")
if _raw_csrf:
    CSRF_TRUSTED_ORIGINS = [o.strip() for o in _raw_csrf.split(",") if o.strip()]
else:
    CSRF_TRUSTED_ORIGINS = [f"https://{h}" for h in ALLOWED_HOSTS if h not in ("*", "127.0.0.1", "localhost")]

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
        "deposit_create": "5/hour",
        "deposit_txid": "20/hour",
        "provider_webhook": "120/min",
    },
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=60),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "ROTATE_REFRESH_TOKENS": True,
    # Blacklist the rotated-out refresh token. Without this, ROTATE_REFRESH_TOKENS
    # alone leaves the old token valid until it naturally expires.
    "BLACKLIST_AFTER_ROTATION": True,
}

# Running the test suite: disable caching so DRF's anon throttle (20/min) never
# trips while tests accumulate requests across a run that can exceed a minute.
if "test" in sys.argv:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}}

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

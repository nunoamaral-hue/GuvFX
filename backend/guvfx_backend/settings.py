from pathlib import Path
from datetime import timedelta
from rest_framework.settings import api_settings  as jwt_api_settings # optional, just for clarity
import os
from dotenv import load_dotenv

# Allow Authorization header from browser
from corsheaders.defaults import default_headers

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

def env(key: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(key, default)
    if required and value is None:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return value

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/5.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!

SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env("DJANGO_DEBUG", "False").lower() == "true"

_raw_hosts = env("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
ALLOWED_HOSTS = [h.strip() for h in _raw_hosts.split(",") if h.strip()]



# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",

    # Third-party
    "rest_framework",
    "corsheaders",

    # Local apps
    "users",
    "core",
    "trading",
    "strategies",
    "backtests",
    "analytics",
    "ai_helper",
    "execution",  # NEW
    "hosting",
    "mt5",
    "billing",
    "reconciliation",
    "admin_ops",
    "onboarding",
    "reliability",  # RX-2 Reliability Core (Phase 1: detection/visibility/alerting)
    "terminal_provisioning",  # TX-1A/TX-1B Terminal Isolation foundation (additive)
    "hosted_workspace",  # ADR-0033 Hosted Persistent MT5 Workspace foundation (additive, DARK)
    "wims",  # WP-1 — Educational Content Flow (logically separate per ADR-009)
    "intelligence",  # Phase 7A — GuvFX Signal Intelligence Producer (produces; WIMS consumes)
    "signal_intake",  # EXEC-E0 — Wayond signal -> human PendingSignalApproval (shadow; no execution)
    "operational_events",  # WP5.1 — Operational Event Model (ADR-0032; owner-scoped ops read model, DARK)
    "approvals",  # A5 — generic human-gated artefact-SHA approval primitive (DARK; no auto-promote)
    "broker_catalogue",  # Broker Catalogue V1 — immutable versioned broker-bootstrap store (DARK; approval-gated)
    "broker_intelligence",  # Withdrawal Intelligence V1 — broker-email identity + evidence (DARK; isolated ingestion)
    "broker_public",  # Public broker directory + privacy-safe metric aggregates (DARK; unauthenticated read surface)
    "customer_notifications.apps.CustomerNotificationsConfig",  # Customer Telegram plane; DARK
    "trade_copier",  # ADR-0051 MT5 Master/Follower Copier — read-only observer + ledger (DARK; observer-first)
]

MIDDLEWARE = [
    "corsheaders.middleware.CorsMiddleware",
    'django.middleware.security.SecurityMiddleware',
    'core.middleware.SecurityHeadersMiddleware',  # Custom security headers (HSTS, CSP, etc.)
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'guvfx_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'guvfx_backend.wsgi.application'


# Database
# https://docs.djangoproject.com/en/5.1/ref/settings/#databases

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", required=True),
        "USER": env("DB_USER", required=True),
        "PASSWORD": env("DB_PASSWORD", required=True),
        "HOST": env("DB_HOST", "127.0.0.1"),
        "PORT": env("DB_PORT", "5432"),
    }
}

# --- Controlled-erasure executor connection (ADR-0054, Finding C) -------------------------------------------
# A SEPARATE, tightly-scoped DB alias used ONLY by the governed erasure management command to invoke the
# privileged SECURITY DEFINER procedure as the dedicated `guvfx_erasure_exec` login. Its credential MUST NOT be
# present in the web/worker runtime environment — it is injected only into the governed job's environment. When
# these env vars are absent (the normal case: web, workers, CI, local dev), the alias is NOT defined and erasure
# fails closed (erasure.erasure_executor_alias() returns None). It must never reuse DB_USER/DB_PASSWORD.
_ERASURE_EXEC_USER = env("ERASURE_EXECUTOR_DB_USER", "")
_ERASURE_EXEC_PASSWORD = env("ERASURE_EXECUTOR_DB_PASSWORD", "")
if _ERASURE_EXEC_USER and _ERASURE_EXEC_PASSWORD:
    DATABASES["erasure_executor"] = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("ERASURE_EXECUTOR_DB_NAME", "") or DATABASES["default"]["NAME"],
        "USER": _ERASURE_EXEC_USER,
        "PASSWORD": _ERASURE_EXEC_PASSWORD,
        "HOST": env("ERASURE_EXECUTOR_DB_HOST", "") or DATABASES["default"]["HOST"],
        "PORT": env("ERASURE_EXECUTOR_DB_PORT", "") or DATABASES["default"]["PORT"],
        "TEST": {"MIRROR": "default"},   # never provision a separate test DB for this alias
    }

GUAC_BASE_URL = os.environ.get(
    "GUAC_BASE_URL",
    "http://127.0.0.1:8081/guacamole",
)

# Windows Agent Configuration (for MT5 backtests + strategy assignment)
GUVFX_WINDOWS_AGENT_BASE_URL = env(
    "GUVFX_WINDOWS_AGENT_BASE_URL",
    "http://10.50.0.2:8787",
)
GUVFX_WINDOWS_AGENT_TOKEN = env("GUVFX_WINDOWS_AGENT_TOKEN", "")

# Legacy worker token toggle.  Set to "false" to disable legacy X-Worker-Token
# header authentication and require all workers to use WorkerIdentity credentials.
ENABLE_LEGACY_WORKER_TOKEN: bool = env("ENABLE_LEGACY_WORKER_TOKEN", "true").lower() == "true"

# Market-data research data root (GFX-PKT-006C).
# Intentionally has NO default and is NOT required: the application starts without
# a data root. Any real market-data operation must validate this separately and
# fail closed when it is unset/blank (see research/market_data/config.py). There is
# no repository fallback.
GUVFX_DATA_ROOT = env("GUVFX_DATA_ROOT")


# Password validation
# https://docs.djangoproject.com/en/5.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.1/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.1/howto/static-files/

STATIC_URL = 'static/'

# Default primary key field type
# https://docs.djangoproject.com/en/5.1/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# REST Framework Configuration
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "users.auth_cookie.CookieJWTAuthentication",
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_THROTTLE_CLASSES": [
        "core.throttling.GuvFXUserRateThrottle",
        "core.throttling.GuvFXIPRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "user": "100/min",
        "ip": "1000/min",
        "csrf": "60/min",
        "auth": "20/min",
        "customer_telegram_webhook": "120/min",
        # Dedicated anonymous ceiling for the public broker-directory surface (broker_public). Conservative by
        # design; the public surface is DARK (404) until BROKER_PUBLIC_DIRECTORY_ENABLED is armed. The exact value is
        # a governance/capacity decision (Amber) — this is the proposed pre-activation default.
        "public": "60/min",
    },
}

# Customer Telegram notifications are a separate security domain from provider intake
# (TELEGRAM_API_*) and stakeholder/operations delivery (TELEGRAM_* and VALIDATION_AGENT_*).
# The customer plane is inert unless explicitly enabled with a dedicated approved bot.
CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED: bool = env(
    "CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED", "false").lower() in ("1", "true", "yes", "on")
CUSTOMER_TELEGRAM_BOT_USERNAME: str = env("CUSTOMER_TELEGRAM_BOT_USERNAME", "")
CUSTOMER_TELEGRAM_BOT_TOKEN: str = env("CUSTOMER_TELEGRAM_BOT_TOKEN", "")
CUSTOMER_TELEGRAM_WEBHOOK_SECRET: str = env("CUSTOMER_TELEGRAM_WEBHOOK_SECRET", "")
CUSTOMER_TELEGRAM_WEBHOOK_URL: str = env("CUSTOMER_TELEGRAM_WEBHOOK_URL", "")
CUSTOMER_TELEGRAM_WORKER_ENABLED: bool = env(
    "CUSTOMER_TELEGRAM_WORKER_ENABLED", "false").lower() in ("1", "true", "yes", "on")
CUSTOMER_TELEGRAM_TOKEN_TTL_SECONDS: int = int(env("CUSTOMER_TELEGRAM_TOKEN_TTL_SECONDS", "600"))
CUSTOMER_NOTIFICATION_MAX_ATTEMPTS: int = int(env("CUSTOMER_NOTIFICATION_MAX_ATTEMPTS", "5"))

# Cache configuration for rate limiting
# Production: DatabaseCache (shared across gunicorn workers, uses existing Postgres)
# Development: LocMemCache (simpler, no setup needed)
# Optional: Redis if REDIS_URL is set (fastest option)

_redis_url = env("REDIS_URL", "")

if _redis_url:
    # Redis available - use it (fastest, best for high traffic)
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": _redis_url,
        }
    }
elif DEBUG:
    # Development mode - use in-memory cache (simple, per-process)
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "guvfx-rate-limit-cache",
        }
    }
else:
    # Production without Redis - use DatabaseCache (shared across workers)
    # Requires: python manage.py createcachetable
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "django_cache",
            "OPTIONS": {
                "MAX_ENTRIES": 10000,  # Reasonable limit for rate limiting
            },
        }
    }

SIMPLE_JWT = {
    "ALGORITHM": "HS256",
    "SIGNING_KEY": env("JWT_SECRET_KEY", SECRET_KEY),  # fallback to SECRET_KEY in dev
    "ACCESS_TOKEN_LIFETIME": timedelta(
        minutes=int(env("JWT_ACCESS_MINUTES", "15"))
    ),
    "REFRESH_TOKEN_LIFETIME": timedelta(
        days=int(env("JWT_REFRESH_DAYS", "7"))
    ),
    # other SIMPLE_JWT settings as needed...
}
# Custom User Model
AUTH_USER_MODEL = "users.User"

# CORS Configuration
CORS_ALLOW_CREDENTIALS = True

# Production defaults are FIXED to the guvfx.com origins; an isolated staging/investor-demo host (or local dev)
# ADDS its origins via the env vars below. With the env vars UNSET this block is byte-identical to production
# (prod origins only) — the env is additive, never a replacement, so a demo deployment can never narrow or lose the
# production origins. (ADR-0050 / WV1 demo-env: see docs/WV1_BOARD_DEMO_RUNBOOK.md.)
_CORS_PROD = ["https://guvfx.com", "https://www.guvfx.com"]
_CSRF_PROD = ["https://guvfx.com", "https://www.guvfx.com", "https://api.guvfx.com"]
_extra_cors = [o.strip() for o in env("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()]
_extra_csrf = [o.strip() for o in env("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
CORS_ALLOWED_ORIGINS = _CORS_PROD + [o for o in _extra_cors if o not in _CORS_PROD]
CORS_ALLOW_HEADERS = list(default_headers) + [
    "Authorization",
    "X-CSRFToken",
]

# CSRF Settings
CSRF_TRUSTED_ORIGINS = _CSRF_PROD + [o for o in _extra_csrf if o not in _CSRF_PROD]
# Default ".guvfx.com" (prod unchanged); a demo host sets CSRF_COOKIE_DOMAIN to its own domain, or empty for a
# host-only cookie (localhost). Empty env ⇒ the production default.
CSRF_COOKIE_DOMAIN = env("CSRF_COOKIE_DOMAIN", ".guvfx.com") or None

# Auth (JWT) cookie attributes — consumed by users.auth_cookie_views._cookie_kwargs. Defaults are the PRODUCTION
# values, so with the env UNSET this is byte-identical to production; a demo/staging host overrides them (e.g.
# AUTH_COOKIE_SECURE=False + AUTH_COOKIE_DOMAIN= for http://localhost). Additive/override, never a silent weakening.
AUTH_COOKIE_SECURE = env("AUTH_COOKIE_SECURE", "True").lower() == "true"
AUTH_COOKIE_SAMESITE = env("AUTH_COOKIE_SAMESITE", "Lax")
AUTH_COOKIE_DOMAIN = env("AUTH_COOKIE_DOMAIN", ".guvfx.com") or None
CSRF_COOKIE_HTTPONLY = False  # Must be False so JS can read and send X-CSRFToken header
CSRF_USE_SESSIONS = False  # Use cookie-based CSRF (double-submit pattern)

# Security Settings
# SameSite=None requires Secure=True (enforced by browsers).
# Cross-origin cookie auth (guvfx.com → api.guvfx.com) needs SameSite=None.
if not DEBUG:
    SESSION_COOKIE_SAMESITE = "None"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SAMESITE = "None"
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "31536000"))  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = env("DJANGO_SSL_REDIRECT", "True").lower() == "true"
else:
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SAMESITE = "Lax"
    CSRF_COOKIE_SECURE = False
    SECURE_HSTS_SECONDS = 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = False
    SECURE_HSTS_PRELOAD = False
    SECURE_SSL_REDIRECT = False

# ─────────────────────────────────────────────────────────────────────
# Email (Google Workspace SMTP) — genuine customer email verification.
# Every value is env-driven. EMAIL_HOST_PASSWORD is a Google Workspace App
# Password: a SECRET supplied ONLY via the deploy env / secret store, never
# committed to Git (RULE 3 — no secrets in tracked files). Defaults below are
# non-secret and safe to keep in source; with EMAIL_HOST_USER/PASSWORD unset the
# send path fails closed (a clear error), it never silently drops mail.
# ─────────────────────────────────────────────────────────────────────
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "smtp.gmail.com")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_USE_TLS = env("EMAIL_USE_TLS", "True").lower() == "true"
EMAIL_USE_SSL = env("EMAIL_USE_SSL", "False").lower() == "true"
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")  # SECRET — deploy env only, never in Git
EMAIL_TIMEOUT = int(env("EMAIL_TIMEOUT", "20"))
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "GuvFX Support <support@guvfx.com>")
SERVER_EMAIL = env("SERVER_EMAIL", "admin@guvfx.com")
# Reply-To for customer-facing mail (support inbox). Falls back to the From address.
EMAIL_REPLY_TO = env("EMAIL_REPLY_TO", "support@guvfx.com")
# Public site base used to build customer-facing links in emails.
FRONTEND_BASE_URL = env("FRONTEND_BASE_URL", "https://guvfx.com")

# Logging Configuration
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "remove_sensitive": {
            "()": "django.utils.log.CallbackFilter",
            "callback": lambda record: not any(
                s in str(record.getMessage()).lower()
                for s in ["password", "secret", "token"]
            ),
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "filters": ["remove_sensitive"],
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env("DJANGO_LOG_LEVEL", "INFO"),
    },
}
# Backtest Artifact Storage (Packet B — B3)
# Local filesystem root for artifact files.  PostgreSQL stores metadata only.
BACKTEST_ARTIFACT_ROOT = env("BACKTEST_ARTIFACT_ROOT", str(BASE_DIR / "backtest_artifacts"))
BACKTEST_ARTIFACT_MAX_BYTES = int(env("BACKTEST_ARTIFACT_MAX_BYTES", str(50 * 1024 * 1024)))  # 50 MB

# --- Validation-agent monitoring pipeline (Monitoring-Runner packet) ---
# The whole pipeline is DARK by default: monitoring OFF, alert sink NULL, and NO Telegram/email destination
# configured. Enabling it (and, separately, selecting an external delivery channel) is a Sponsor-gated deploy
# step — never a repository default. NO SECRET is committed here; every value comes from the environment.
#
#   VALIDATION_AGENT_MONITORING_ENABLED  master switch for run_agent_readiness_probe (default OFF)
#   VALIDATION_AGENT_PROBE_INTERVAL_SECONDS  nominal scheduler tick; drives stale (coverage-gap) detection
#   VALIDATION_AGENT_ALERT_COOLDOWN_SECONDS  durable per-alert suppression window (RR-11 storm guard)
#   VALIDATION_AGENT_STALE_DETECTION_ENABLED  page when a scheduled run was missed (default OFF — a paused
#                                             scheduler would otherwise page; opt in only with continuous cron)
#   AGENT_ALERT_SINK                    '' | 'null' (default) | 'logging' | 'telegram'
#   AGENT_ALERT_OWNER                   NAMED human/rota an alert terminates at (required for a live sink)
#   VALIDATION_AGENT_TELEGRAM_CHAT_ID   DEDICATED ops chat id (MUST differ from the customer TELEGRAM_CHAT_ID)
#   VALIDATION_AGENT_TELEGRAM_BOT_TOKEN ops bot token (its OWN secret — never the customer TELEGRAM_BOT_TOKEN)
#   VALIDATION_AGENT_ALERT_FALLBACK_EMAIL  optional mailbox used only if the primary external sink fails
VALIDATION_AGENT_MONITORING_ENABLED: bool = env("VALIDATION_AGENT_MONITORING_ENABLED", "false").lower() == "true"
VALIDATION_AGENT_PROBE_INTERVAL_SECONDS: int = int(env("VALIDATION_AGENT_PROBE_INTERVAL_SECONDS", "60"))
VALIDATION_AGENT_ALERT_COOLDOWN_SECONDS: int = int(env("VALIDATION_AGENT_ALERT_COOLDOWN_SECONDS", "900"))
VALIDATION_AGENT_STALE_DETECTION_ENABLED: bool = env(
    "VALIDATION_AGENT_STALE_DETECTION_ENABLED", "false").lower() == "true"
AGENT_ALERT_SINK: str = env("AGENT_ALERT_SINK", "null")
AGENT_ALERT_OWNER: str = env("AGENT_ALERT_OWNER", "")
VALIDATION_AGENT_TELEGRAM_CHAT_ID: str = env("VALIDATION_AGENT_TELEGRAM_CHAT_ID", "")
VALIDATION_AGENT_TELEGRAM_BOT_TOKEN: str = env("VALIDATION_AGENT_TELEGRAM_BOT_TOKEN", "")
VALIDATION_AGENT_ALERT_FALLBACK_EMAIL: str = env("VALIDATION_AGENT_ALERT_FALLBACK_EMAIL", "")

# --- Behind Traefik (TLS terminated upstream) ---
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

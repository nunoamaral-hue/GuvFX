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
    },
}

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

_raw_cors = env("CORS_ALLOWED_ORIGINS", "http://localhost:3000")
CORS_ALLOWED_ORIGINS = ["https://guvfx.com", "https://www.guvfx.com"]
CORS_ALLOW_HEADERS = list(default_headers) + [
    "Authorization",
    "X-CSRFToken",
]

# CSRF Settings
CSRF_TRUSTED_ORIGINS = [
    "https://guvfx.com",
    "https://www.guvfx.com",
    "https://api.guvfx.com",
]
CSRF_COOKIE_DOMAIN = ".guvfx.com"
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

# --- Behind Traefik (TLS terminated upstream) ---
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
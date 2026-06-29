"""
Throwaway settings for running the WP-1 demo / tests in isolation.

Inherits the real GuvFX settings but swaps in a local SQLite database so the
Educational Content Flow can be demonstrated without the production Postgres
role. NOT used by the running application.

    DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py migrate
    DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py wims_demo
"""

from guvfx_backend.settings import *  # noqa: F401,F403

# WIMS is logically separable (ADR-009), so the isolated demo DB only needs the
# core framework, auth/user model and WIMS itself. Excluding the trading apps
# also avoids their Postgres-only RunSQL migrations under SQLite.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "users",
    "wims",
    "intelligence",  # Phase 7A — GuvFX-side producer (no models; demo/tests)
]

ROOT_URLCONF = "wims._demo_urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "wims_demo.sqlite3",  # noqa: F405
    }
}

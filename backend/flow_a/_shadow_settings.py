"""
Throwaway settings for running the Flow A shadow pipeline demo / tests.

Inherits the real GuvFX settings but swaps in a local SQLite database and a
minimal app set so Flow A can be demonstrated/tested without the production
Postgres role and without the trading apps' Postgres-only migrations. NOT used
by the running application — Flow A is deliberately absent from production
``INSTALLED_APPS`` (Shadow Mode, no production deployment).

    cd backend
    DJANGO_SETTINGS_MODULE=flow_a._shadow_settings python manage.py migrate
    DJANGO_SETTINGS_MODULE=flow_a._shadow_settings python manage.py run_flow_a_shadow
    DJANGO_SETTINGS_MODULE=flow_a._shadow_settings python manage.py test flow_a
"""

from guvfx_backend.settings import *  # noqa: F401,F403

# Flow A persists no models and only reuses the (model-less) intelligence
# producer, so the isolated DB needs just the framework, auth/user model,
# intelligence and flow_a.
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "users",
    "intelligence",  # reused producer (Phase 7A); no models
    "flow_a",        # Shadow Delivery pipeline; no models
]

ROOT_URLCONF = "flow_a._shadow_urls"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "flow_a_shadow.sqlite3",  # noqa: F405
    }
}

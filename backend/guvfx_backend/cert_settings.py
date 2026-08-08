"""ADR-0034 / M3b-2 — cert-only Django settings (isolated, DARK, no production infrastructure).

The disposable-host certification command ``certify_workspace_observation`` is a Django command, so it needs
``django.setup()`` to import the certified observation chain's modules (``hosted_workspace`` →
``operational_events`` model imports). It performs NO database query and uses NO web/API surface. This module
provides the SMALLEST settings that let that import + command run, WITHOUT the production settings stack
(no middleware, DRF, JWT, CORS, logging, templates, static, auth backends) and WITHOUT a real database.

NOT for production. Not referenced by any production code path or deployment. Used only by the
disposable-host certification runbook (``DJANGO_SETTINGS_MODULE=guvfx_backend.cert_settings``). Every value
here is disposable — never a production secret, never the production database.
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Disposable, non-secret. The cert command signs nothing and serves nothing; this only satisfies Django.
SECRET_KEY = "cert-only-disposable-not-a-secret"  # noqa: S105
DEBUG = False
ALLOWED_HOSTS: list = []

# Minimal app set required to IMPORT the certified chain's models. hosted_workspace + operational_events FK to
# trading.TradingAccount, which (with AUTH_USER_MODEL = users.User) pulls in users + trading. The model graph
# is deliberately NOT FK-closed (trading further references mt5 / execution) — that is exactly why the cert
# command skips Django system checks (requires_system_checks = []); importing the models does not require the
# FK *targets* to be installed. NO rest_framework / corsheaders / admin / sessions / staticfiles is needed to
# import models or run the read-only, DB-free certification command.
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "users",
    "trading",
    "operational_events",
    "hosted_workspace",
]

# No query is ever executed by the cert command; an in-memory sqlite satisfies django.setup() only. The
# production database is NEVER referenced.
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}

AUTH_USER_MODEL = "users.User"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True

import os
import requests
from urllib.parse import quote


def _require_guac_env():
    """
    Lazily require Guacamole environment variables.

    This must NOT run at import time, otherwise Django checks / CI will fail.
    """
    base = os.environ.get("GUAC_BASE")
    user = os.environ.get("GUAC_ADMIN_USER")
    password = os.environ.get("GUAC_ADMIN_PASS")

    if not base or not user or not password:
        raise RuntimeError(
            "Missing Guacamole env "
            "(GUAC_BASE / GUAC_ADMIN_USER / GUAC_ADMIN_PASS)"
        )

    return base.rstrip("/"), user, password


# Optional globals (safe for imports; NOT required for runtime)
GUAC_BASE = os.environ.get("GUAC_BASE")
if GUAC_BASE:
    GUAC_BASE = GUAC_BASE.rstrip("/")

GUAC_ADMIN_USER = os.environ.get("GUAC_ADMIN_USER")
GUAC_ADMIN_PASS = os.environ.get("GUAC_ADMIN_PASS")


def guac_token() -> str:
    """
    Obtain an auth token from Guacamole.
    Runtime-only dependency on Guacamole env vars.
    """
    base, user, password = _require_guac_env()

    r = requests.post(
        f"{base}/api/tokens",
        data={
            "username": user,
            "password": password,
        },
        timeout=10,
    )
    r.raise_for_status()

    return r.json()["authToken"]


def launch_url(auth_token: str, connection_id: int) -> str:
    """
    Build a Guacamole launch URL for a connection.
    """
    base, _, _ = _require_guac_env()
    return f"{base}/#/client/{connection_id}?token={quote(auth_token)}"

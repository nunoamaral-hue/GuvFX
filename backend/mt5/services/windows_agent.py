import os
import requests


def _require_agent_env():
    """
    Lazily require MT5 Windows Agent environment variables.

    This must NOT run at import time, otherwise Django checks / CI will fail.
    """
    url = os.environ.get("GUVFX_AGENT_URL")
    token = os.environ.get("GUVFX_AGENT_TOKEN")

    if not url or not token:
        raise RuntimeError(
            "Missing MT5 Windows Agent env "
            "(GUVFX_AGENT_URL / GUVFX_AGENT_TOKEN)"
        )

    return url.rstrip("/"), token


# Optional globals (safe for imports; NOT required for runtime)
AGENT_URL = os.environ.get("GUVFX_AGENT_URL")
if AGENT_URL:
    AGENT_URL = AGENT_URL.rstrip("/")

AGENT_TOKEN = os.environ.get("GUVFX_AGENT_TOKEN")


def provision_windows_user(username: str, password: str) -> None:
    """
    Provision a Windows user via the MT5 Windows Agent.

    Runtime-only dependency on agent env vars.
    """
    agent_url, agent_token = _require_agent_env()

    r = requests.post(
        f"{agent_url}/provision-user",
        headers={
            "Content-Type": "application/json",
            "X-GuvFX-Agent-Token": agent_token,
        },
        json={
            "username": username,
            "password": password,
        },
        timeout=10,
    )

    r.raise_for_status()

    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Agent error: {data}")

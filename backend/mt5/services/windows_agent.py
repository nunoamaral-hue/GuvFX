import os
import requests

AGENT_URL = os.environ.get("GUVFX_AGENT_URL")

if AGENT_URL:
    AGENT_URL = AGENT_URL.rstrip("/")
if not AGENT_URL:
    # Agent URL is required only when MT5 Windows agent is used
    pass

AGENT_TOKEN = os.environ["GUVFX_AGENT_TOKEN"]

def provision_windows_user(username: str, password: str) -> None:
    r = requests.post(
        f"{AGENT_URL}/provision-user",
        headers={
            "Content-Type": "application/json",
            "X-GuvFX-Agent-Token": AGENT_TOKEN,
        },
        json={"username": username, "password": password},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Agent error: {data}")

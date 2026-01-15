import os
import requests

AGENT_URL = os.environ["GUVFX_AGENT_URL"].rstrip("/")
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

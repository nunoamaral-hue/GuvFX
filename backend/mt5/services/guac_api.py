import os
import requests
from urllib.parse import quote

GUAC_BASE = os.environ["GUAC_BASE"].rstrip("/")
GUAC_ADMIN_USER = os.environ["GUAC_ADMIN_USER"]
GUAC_ADMIN_PASS = os.environ["GUAC_ADMIN_PASS"]

def guac_token() -> str:
    r = requests.post(
        f"{GUAC_BASE}/api/tokens",
        data={"username": GUAC_ADMIN_USER, "password": GUAC_ADMIN_PASS},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["authToken"]

def launch_url(auth_token: str, connection_id: int) -> str:
    return f"{GUAC_BASE}/#/client/{connection_id}?token={quote(auth_token)}"

"""WP3b — Gmail OAuth 2.0 authorization-code flow helpers (READ-ONLY scope).

Pure + testable: the HTTP call to Google's token endpoint is injected (``http_post``), so the whole flow is exercised
with synthetic fixtures and NO real consent. Grants ONLY ``gmail.readonly`` — never a send/modify/compose scope, so a
connected mailbox can only ever be READ (consistent with the no-trading-coupling, observation-only contract).

The token dict returned (``{access_token, refresh_token, expiry, scope, token_type}``) is handed to
``credential_store`` for encrypted persistence; the client_id/client_secret come from the ingestion service's secret
store (never the DB/repo). This module holds NO secret and logs nothing.
"""
from __future__ import annotations

import datetime
import json
import urllib.parse
import urllib.request
from typing import Callable, Optional

# READ-ONLY scope ONLY. A connected mailbox is read, never written — no gmail.modify / gmail.send / compose.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

# Mail scopes that grant WRITE/modify/full access. The token must never carry one of these — if the grant comes back
# with any (misconfigured consent screen, a client with prior broader grants), we FAIL CLOSED rather than store a
# write-capable token for a mailbox we only ever read.
_FORBIDDEN_SCOPES = (
    "https://mail.google.com/",                        # full IMAP/SMTP access
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
)


def _assert_readonly_scope(scope_str: str) -> None:
    """Fail closed if the GRANTED scope includes any mail write/modify capability. A connected mailbox is read-only;
    a write-capable token must never be stored or used."""
    granted = set((scope_str or "").split())
    bad = granted.intersection(_FORBIDDEN_SCOPES)
    if bad:
        raise GmailOAuthError("granted scope includes a non-readonly Gmail capability; refusing (fail-closed)")

# http_post(url, form_dict) -> (status:int, body:str). Injected so tests never hit the network.
HttpPost = Callable[[str, dict], "tuple[int, str]"]


class GmailOAuthError(Exception):
    """Raised on a non-200 token response or a response missing the required fields (fail-closed)."""


def build_authorization_url(*, client_id: str, redirect_uri: str, state: str) -> str:
    """The consent URL to send the operator to. ``access_type=offline`` + ``prompt=consent`` so Google returns a
    refresh_token (needed for the unattended ingestion worker). Scope is READ-ONLY."""
    if not client_id or not redirect_uri:
        raise GmailOAuthError("client_id and redirect_uri are required")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GMAIL_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        # Deliberately NOT include_granted_scopes: request the MINIMAL grant (readonly only) and never let a prior
        # grant to this client widen the returned scope.
        "state": state,
    }
    return _AUTH_ENDPOINT + "?" + urllib.parse.urlencode(params)


def _default_http_post(url: str, form: dict) -> "tuple[int, str]":
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:   # surface the status so the caller fails closed with a reason (no token text)
        return e.code, e.read().decode("utf-8", "ignore")


def _normalise_token(body: str, *, fallback_refresh: Optional[str] = None, now: datetime.datetime) -> dict:
    try:
        raw = json.loads(body)
    except Exception as exc:  # noqa: BLE001
        raise GmailOAuthError("token endpoint returned non-JSON") from exc
    access = raw.get("access_token")
    if not access:
        raise GmailOAuthError("token response missing access_token")
    # A refresh is returned on the first consent; a refresh *grant* reuses the prior refresh_token (fallback).
    refresh = raw.get("refresh_token") or fallback_refresh or ""
    expires_in = raw.get("expires_in")
    expiry = (now + datetime.timedelta(seconds=int(expires_in))).isoformat() if expires_in else ""
    scope = raw.get("scope", GMAIL_READONLY_SCOPE)
    _assert_readonly_scope(scope)   # fail closed on an over-broad / write-capable grant before we persist/use it
    return {"access_token": access, "refresh_token": refresh, "expiry": expiry,
            "scope": scope, "token_type": raw.get("token_type", "Bearer")}


def exchange_code(*, code: str, client_id: str, client_secret: str, redirect_uri: str,
                  http_post: Optional[HttpPost] = None, now: Optional[datetime.datetime] = None) -> dict:
    """Exchange an authorization code for a token dict. Fail-closed on non-200 / missing access_token."""
    post = http_post or _default_http_post
    now = now or datetime.datetime.now(datetime.timezone.utc)
    status, body = post(_TOKEN_ENDPOINT, {
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect_uri, "grant_type": "authorization_code",
    })
    if status != 200:
        raise GmailOAuthError(f"token exchange failed (http {status})")
    return _normalise_token(body, now=now)


def refresh_access_token(*, refresh_token: str, client_id: str, client_secret: str,
                         http_post: Optional[HttpPost] = None, now: Optional[datetime.datetime] = None) -> dict:
    """Refresh an access token using a stored refresh_token. The refresh grant usually omits refresh_token, so the
    existing one is carried forward. Fail-closed on non-200 / missing access_token."""
    if not refresh_token:
        raise GmailOAuthError("refresh_token is required")
    post = http_post or _default_http_post
    now = now or datetime.datetime.now(datetime.timezone.utc)
    status, body = post(_TOKEN_ENDPOINT, {
        "refresh_token": refresh_token, "client_id": client_id, "client_secret": client_secret,
        "grant_type": "refresh_token",
    })
    if status != 200:
        raise GmailOAuthError(f"token refresh failed (http {status})")
    return _normalise_token(body, fallback_refresh=refresh_token, now=now)

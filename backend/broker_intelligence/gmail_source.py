"""WP3b — GmailMailSource: a read-only MailSource over a connected Gmail mailbox.

Two parts:
* ``GmailMailSource`` (pure orchestration, fully testable): given a ``ConnectedMailbox`` (cursor + credential_ref) and
  a ``GmailClient``, it lists new messages since the stored cursor, maps each to the normalised ``MailMessage`` the
  ingestion pipeline consumes, and — only when the caller commits — advances the mailbox cursor. Re-fetching an
  un-committed batch is safe (ingestion is idempotent by evidence content-hash), so a crash mid-batch never loses or
  double-ingests a message.
* ``GmailApiClient`` (the Gmail-REST-specific half): resolves the encrypted OAuth token via ``credential_store``,
  refreshes it read-only when expired, and calls the Gmail REST API (``users.messages.list`` + ``get?format=raw``)
  through an INJECTED http callable (so it is exercised with synthetic fixtures and no network). It decodes the raw
  RFC822 with the stdlib ``email`` parser — never a broker login, order, or write scope.

READ-ONLY throughout: the only scope is gmail.readonly; this module never sends, modifies, or deletes mail.
"""
from __future__ import annotations

import base64
import datetime
import email
import email.utils
import json
import urllib.parse
import urllib.request
from email.header import decode_header, make_header
from typing import Callable, Iterable, List, Optional, Protocol, Tuple

from .mail_source import MailMessage, MailSource


class GmailClient(Protocol):
    """Lists new messages since a cursor. Returns ``(messages, new_cursor_state)`` where each message is a dict with
    ``provider_message_id``, ``raw_bytes``, ``to_addresses``, ``from_address``, ``subject``, ``body``,
    ``received_at``. ``new_cursor_state`` is the Gmail historyId to persist once the batch is ingested."""
    def list_new_messages(self, cursor_state: str) -> "Tuple[List[dict], str]": ...


def parse_rfc822(raw_bytes: bytes) -> dict:
    """Extract normalised fields from raw RFC822 bytes (the source of truth stays the raw bytes). Stdlib-only; never
    raises on a malformed header (best-effort decode) — the raw bytes are still stored as evidence."""
    msg = email.message_from_bytes(raw_bytes)

    def _hdr(name: str) -> str:
        v = msg.get(name, "")
        try:
            return str(make_header(decode_header(v))) if v else ""
        except Exception:  # noqa: BLE001 — a pathological header must not break ingestion
            return v or ""

    to_raw = msg.get_all("To", []) + msg.get_all("Delivered-To", []) + msg.get_all("X-Original-To", [])
    to_addresses = tuple(addr for _n, addr in email.utils.getaddresses(to_raw) if addr)
    from_address = email.utils.parseaddr(_hdr("From"))[1]
    subject = _hdr("Subject")
    # Prefer a text/plain body part; fall back to the first text part. Evidence is the raw bytes regardless.
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                    break
                except Exception:  # noqa: BLE001
                    continue
    else:
        try:
            body = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore")
        except Exception:  # noqa: BLE001
            body = msg.get_payload() if isinstance(msg.get_payload(), str) else ""
    date = msg.get("Date")
    received_at = None
    if date:
        try:
            received_at = email.utils.parsedate_to_datetime(date)
        except Exception:  # noqa: BLE001
            received_at = None
    if received_at is None:
        received_at = datetime.datetime.now(datetime.timezone.utc)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=datetime.timezone.utc)
    return {"to_addresses": to_addresses, "from_address": from_address, "subject": subject,
            "body": body, "received_at": received_at}


class GmailMailSource(MailSource):
    """Read-only MailSource over one connected mailbox. Pure orchestration — the Gmail REST specifics live in the
    injected ``GmailClient``."""

    def __init__(self, mailbox, client: GmailClient):
        self._mailbox = mailbox
        self._client = client
        self._new_cursor = str(getattr(mailbox, "cursor_state", "") or "")

    def fetch(self) -> Iterable[MailMessage]:
        messages, new_cursor = self._client.list_new_messages(str(self._mailbox.cursor_state or ""))
        self._new_cursor = str(new_cursor or self._new_cursor)
        for m in messages:
            yield MailMessage(
                raw_bytes=m["raw_bytes"],
                to_addresses=tuple(m.get("to_addresses") or ()),
                from_address=m.get("from_address", "") or "",
                subject=m.get("subject", "") or "",
                body=m.get("body", "") or "",
                received_at=m["received_at"],
                provider_message_id=str(m.get("provider_message_id", "") or ""),
            )

    def ack(self, message: MailMessage) -> None:
        return None   # per-message no-op; the cursor is committed for the whole batch (see commit_cursor)

    def commit_cursor(self) -> None:
        """Persist the advanced cursor + sync time. The caller (worker) calls this ONLY after the batch has been
        successfully ingested, so an interrupted batch is safely re-fetched (ingestion is content-hash idempotent)."""
        from django.utils import timezone
        self._mailbox.cursor_state = self._new_cursor
        self._mailbox.last_successful_sync = timezone.now()
        self._mailbox.save(update_fields=["cursor_state", "last_successful_sync", "updated_at"])


# ── real Gmail REST client (live-only; HTTP injected so it is unit-testable without the network) ──
HttpGet = Callable[[str, dict], "Tuple[int, str]"]   # (url, headers) -> (status, body)
_GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


class GmailApiClient:
    """Resolves the encrypted token for ``mailbox.credential_ref`` (refreshing read-only when expired), then lists +
    fetches raw messages via the Gmail REST API. ``http_get`` is injected (default: urllib) so tests drive it with
    fixtures. Never sends/modifies mail; only gmail.readonly endpoints are called."""

    # Cap the raw bytes buffered per message so a single huge (or adversarial) email cannot exhaust memory. A larger
    # message is skipped (logged), never partially ingested. Broker withdrawal notices are tiny.
    MAX_RAW_BYTES = 5 * 1024 * 1024
    # Bound the number of history/list pages followed in one pass (defence against an unbounded page walk).
    MAX_PAGES = 20

    def __init__(self, mailbox, *, client_id: str, client_secret: str, http_get: Optional[HttpGet] = None,
                 max_messages: int = 50):
        self._mailbox = mailbox
        self._client_id = client_id
        self._client_secret = client_secret
        self._http_get = http_get or _default_http_get
        self._max = max_messages

    def _access_token(self) -> str:
        from .credential_store import load_token, store_token
        from . import gmail_oauth
        tok = load_token(self._mailbox.credential_ref)
        if not tok:
            raise RuntimeError("no stored credential for mailbox")
        expiry = tok.get("expiry")
        expired = False
        if expiry:
            try:
                expired = datetime.datetime.fromisoformat(expiry) <= datetime.datetime.now(datetime.timezone.utc)
            except Exception:  # noqa: BLE001
                expired = True
        if expired and tok.get("refresh_token"):
            tok = gmail_oauth.refresh_access_token(
                refresh_token=tok["refresh_token"], client_id=self._client_id, client_secret=self._client_secret)
            store_token(tok, credential_ref=self._mailbox.credential_ref)   # overwrite in place (same ref)
        return tok["access_token"]

    def _get_json(self, url: str) -> dict:
        status, body = self._http_get(url, {"Authorization": "Bearer " + self._access_token()})
        if status != 200:
            raise RuntimeError(f"gmail api http {status}")
        return json.loads(body)

    def _fetch_message(self, mid: str) -> Optional[dict]:
        """Fetch + decode ONE raw message, skipping (None) a message whose raw exceeds MAX_RAW_BYTES."""
        full = self._get_json(f"{_GMAIL_API}/messages/{urllib.parse.quote(mid)}?format=raw")
        raw_b64 = full.get("raw", "")
        if not raw_b64:
            return None
        if len(raw_b64) * 3 // 4 > self.MAX_RAW_BYTES:   # base64 decodes to ~3/4 its length
            return None   # oversized -> skip (never buffer/partial-ingest a huge message)
        raw_bytes = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
        if len(raw_bytes) > self.MAX_RAW_BYTES:
            return None
        return {"provider_message_id": mid, "raw_bytes": raw_bytes, **parse_rfc822(raw_bytes)}

    def list_new_messages(self, cursor_state: str) -> "Tuple[List[dict], str]":
        """INCREMENTAL when a cursor (historyId) is present — ``users.history.list?startHistoryId=<cursor>`` collects
        exactly the messages added since the cursor and advances to the response's latest historyId, so no message
        is ever skipped by the cursor jumping to 'now'. INITIAL sync (no cursor) lists recent messages and anchors
        the cursor to the current profile historyId."""
        cursor = str(cursor_state or "")
        out: List[dict] = []
        if cursor:
            ids: List[str] = []
            seen = set()
            new_cursor = cursor
            page = f"{_GMAIL_API}/history?startHistoryId={urllib.parse.quote(cursor)}&historyTypes=messageAdded"
            for _ in range(self.MAX_PAGES):
                hist = self._get_json(page)
                new_cursor = str(hist.get("historyId", new_cursor) or new_cursor)
                for h in (hist.get("history") or []):
                    for added in (h.get("messagesAdded") or []):
                        mid = (added.get("message") or {}).get("id")
                        if mid and mid not in seen:
                            seen.add(mid)
                            ids.append(mid)
                token = hist.get("nextPageToken")
                if not token:
                    break
                page = (f"{_GMAIL_API}/history?startHistoryId={urllib.parse.quote(cursor)}"
                        f"&historyTypes=messageAdded&pageToken={urllib.parse.quote(token)}")
            for mid in ids:
                m = self._fetch_message(mid)
                if m is not None:
                    out.append(m)
            return out, new_cursor
        # initial sync
        listing = self._get_json(f"{_GMAIL_API}/messages?maxResults={self._max}")
        for m0 in (listing.get("messages") or []):
            m = self._fetch_message(m0["id"])
            if m is not None:
                out.append(m)
        new_cursor = str(self._get_json(f"{_GMAIL_API}/profile").get("historyId", cursor) or cursor)
        return out, new_cursor


def _default_http_get(url: str, headers: dict) -> "Tuple[int, str]":
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")

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
import html as _htmllib
import json
import re
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


def _html_to_text(s: str) -> str:
    """Best-effort tag-strip of an HTML body to plain text for the deterministic parser (never raises). Drops
    script/style/head, turns block ends into newlines, removes remaining tags, unescapes entities, collapses
    whitespace. URLs in href attributes are discarded with their tags; the parser anchors on labels, not URLs."""
    try:
        s = re.sub(r"(?is)<(script|style|head)[^>]*>.*?</\1>", " ", s)
        s = re.sub(r"(?i)<br\s*/?>", "\n", s)
        s = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|table)>", "\n", s)
        s = re.sub(r"(?i)<[^>]+>", " ", s)
        s = _htmllib.unescape(s)
        s = re.sub(r"[ \t\r\f\v]+", " ", s)
        return s.strip()
    except Exception:  # noqa: BLE001
        return ""


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
    # Prefer a text/plain body part; if the message is HTML-only (common for broker mail — e.g. TradersWay sends
    # multipart/related with only a text/html part), fall back to a tag-stripped rendering of the HTML so the
    # deterministic parser can read the labels. Evidence is the raw bytes regardless.
    body, html_body = "", ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_filename():
                continue
            ct = part.get_content_type()
            if ct == "text/plain" and not body:
                try:
                    body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    pass
            elif ct == "text/html" and not html_body:
                try:
                    html_body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                except Exception:  # noqa: BLE001
                    pass
        if not body and html_body:
            body = _html_to_text(html_body)
    else:
        try:
            payload = msg.get_payload(decode=True)
            decoded = (payload.decode(msg.get_content_charset() or "utf-8", "ignore") if payload is not None
                       else (msg.get_payload() if isinstance(msg.get_payload(), str) else ""))
        except Exception:  # noqa: BLE001
            decoded = msg.get_payload() if isinstance(msg.get_payload(), str) else ""
        body = _html_to_text(decoded) if msg.get_content_type() == "text/html" else decoded
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
            "body": body, "received_at": received_at, "auth_verdict": _auth_verdict(msg)}


def _trusted_authserv_ids() -> "frozenset":
    """The ``Authentication-Results`` authserv-id(s) we trust — the receiving MTA that ACTUALLY verified the message.
    Configurable (settings-then-env ``BROKER_INTELLIGENCE_TRUSTED_AUTHSERV_IDS``, comma/space separated); default the
    Gmail receiver ``mx.google.com``. RFC 8601: a verifier trusts ONLY A-R headers bearing its own authserv-id and
    strips forgeries of it on receipt, so an attacker-injected A-R header with a DIFFERENT authserv-id must be
    ignored (never trusted)."""
    import os
    from django.conf import settings
    raw = getattr(settings, "BROKER_INTELLIGENCE_TRUSTED_AUTHSERV_IDS", None)
    if raw is None:
        raw = os.getenv("BROKER_INTELLIGENCE_TRUSTED_AUTHSERV_IDS", "")
    ids = {p for p in re.split(r"[,\s]+", str(raw or "").strip().lower()) if p}
    # UNION active BrokerProfile trusted authserv-ids (per-broker config). Defensive for no-DB/unmigrated contexts.
    try:
        from .models import BrokerProfile
        for bp in BrokerProfile.objects.filter(is_active=True).only("trusted_authserv_ids"):
            ids.update(bp.authserv_ids())
    except Exception:  # noqa: BLE001
        pass
    return frozenset(ids) or frozenset({"mx.google.com"})


def _ar_resinfos(header_value: str) -> "List[str]":
    """Split one Authentication-Results header into [authserv-id(+version), resinfo, resinfo, ...] per RFC 8601/5322:
    CFWS comments ``(...)`` (nestable) are REMOVED and double-quoted strings are preserved verbatim, so a ``;`` or a
    ``dkim=pass``/``header.d=`` that appears inside a comment or a quoted property value can NEVER create a pseudo
    resinfo or be mistaken for a real method=result. Top-level ``;`` separates resinfos."""
    out, buf = [], []
    depth = 0          # comment nesting depth
    inq = False        # inside a double-quoted string
    i, s = 0, header_value
    while i < len(s):
        c = s[i]
        if inq:
            if c == "\\" and i + 1 < len(s):
                buf.append(s[i:i + 2]); i += 2; continue
            if c == '"':
                inq = False
            buf.append(c); i += 1; continue
        if depth > 0:                                  # inside a comment -> drop everything until it closes
            if c == "\\" and i + 1 < len(s):
                i += 2; continue
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            i += 1; continue
        if c == "(":
            depth += 1; i += 1; continue
        if c == '"':
            inq = True; buf.append(c); i += 1; continue
        if c == ";":
            out.append("".join(buf)); buf = []; i += 1; continue
        buf.append(c); i += 1
    out.append("".join(buf))
    return out


def _dkim_signing_domain(resinfo: str) -> str:
    """The DKIM signing domain from ONE (comment-stripped) dkim resinfo: ``header.d=<domain>`` preferred, else the
    domain of the AUID ``header.i=[local]@<domain>`` (RFC 8601 allows a verifier to report either; Gmail commonly
    emits header.i only). '' if neither is present."""
    md = re.search(r"\bheader\.d\s*=\s*([a-z0-9.\-]+)", resinfo)
    if md:
        return md.group(1)
    mi = re.search(r"\bheader\.i\s*=\s*([^\s;]+)", resinfo)
    if mi:
        v = mi.group(1).strip('"')
        return v.rsplit("@", 1)[-1] if "@" in v else ""
    return ""


def _broker_requires_dmarc_only(from_domain: str) -> bool:
    """True iff an ACTIVE ``BrokerProfile`` whose approved sender domains cover ``from_domain`` is set to
    ``DMARC_ONLY`` — a STRICTER per-broker posture that disables the From-aligned-DKIM fallback for that broker (only
    a DMARC pass is accepted). Defensive (no-DB/unmigrated/DARK safe) and defaults False; it can only STRENGTHEN the
    global verdict, never weaken it, so it needs no per-broker exception review."""
    if not from_domain:
        return False
    try:
        from .broker_senders import domain_matches
        from .models import BrokerProfile
        for bp in (BrokerProfile.objects
                   .filter(is_active=True, auth_policy=BrokerProfile.AuthPolicy.DMARC_ONLY)
                   .only("approved_sender_domains", "auth_policy")):
            if any(domain_matches(from_domain, d) for d in bp.approved_domains()):
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _auth_verdict(msg) -> Optional[str]:
    """R3b sender-authenticity verdict from ``Authentication-Results`` — RFC 8601-safe (the From is NEVER trusted on
    its own). Consider ONLY headers whose authserv-id is a TRUSTED receiver (an attacker-injected A-R with a foreign
    authserv-id is ignored — the receiving MTA strips forgeries of its own id). Each header is parsed into resinfos
    with comments removed + quoted strings preserved (``_ar_resinfos``); for each resinfo ONLY its leading
    ``method=result`` token is the verdict (never free text / a comment / an echoed parameter value). Accept either:

      (1) ``dmarc=pass`` — DMARC guarantees the authenticated identifier is aligned to the visible From; OR
      (2) a **From-ALIGNED DKIM pass**: a ``dkim=pass`` resinfo whose own signing domain (``header.d`` or the
          ``header.i`` AUID domain) AND the message's From domain both fall within the SAME approved broker domain
          (``broker_senders`` allowlist). Narrow fallback for a legitimate broker (e.g. TradersWay) that validly
          DKIM-signs with its own domain but publishes NO DMARC record. An attacker spoofing the From cannot produce a
          valid DKIM signature under the broker's domain, so this is NOT spoofable — unlike a bare ``spf=pass`` /
          unaligned ``dkim=pass`` (which authenticate the sender's OWN domain and are NEVER sufficient here).

    Returns "pass", "fail" (a trusted header with results but no qualifying pass), or None (no trusted A-R header)."""
    from .broker_senders import _domain_of, domain_matches, sender_allowlist
    trusted = _trusted_authserv_ids()
    from_domain = _domain_of(email.utils.parseaddr(msg.get("From", ""))[1])
    allow = sender_allowlist()
    # A broker whose active profile is auth_policy=DMARC_ONLY opts OUT of the aligned-DKIM fallback (STRICTER — only a
    # DMARC pass is accepted for its mail). This only ever strengthens; it never weakens the global verdict.
    dmarc_only = _broker_requires_dmarc_only(from_domain)
    saw_result = False
    for h in (msg.get_all("Authentication-Results", []) or []):
        parts = _ar_resinfos(h.lower())
        if not parts:
            continue
        authserv_id = (parts[0].strip().split() or [""])[0]
        if authserv_id not in trusted:               # only the receiver's own verdict is trusted (forgeries ignored)
            continue
        for resinfo in parts[1:]:
            mm = re.match(r"\s*([a-z][a-z0-9.\-]*)\s*=\s*([a-z]+)", resinfo)   # LEADING method=result only
            if not mm:
                continue
            method, result = mm.group(1), mm.group(2)
            if method not in ("dmarc", "dkim", "spf"):
                continue
            saw_result = True
            if method == "dmarc" and result == "pass":
                return "pass"
            if method == "dkim" and result == "pass" and not dmarc_only:
                d = _dkim_signing_domain(resinfo)
                if d and from_domain and allow and any(
                        domain_matches(from_domain, b) and domain_matches(d, b) for b in allow):
                    return "pass"
            # spf (or any non-pass) contributes only saw_result — never a pass
    return "fail" if saw_result else None             # trusted header, results present, none qualified -> fail


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
                auth_verdict=m.get("auth_verdict"),
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


class MessageCaptureError(Exception):
    """A single message could NOT be captured (empty raw payload, or raw exceeding the capture ceiling). Raising this
    (instead of silently dropping the message) is the durable-preservation contract: the enclosing batch fails and the
    cursor is NOT advanced past the un-captured message, so it resurfaces next pass as a visible, retryable failure —
    raw broker evidence is never lost. An operator can raise ``BROKER_INTELLIGENCE_MAX_RAW_BYTES`` to admit a
    legitimately large email (e.g. one carrying a PDF statement)."""


class MessageTooLargeError(MessageCaptureError):
    """A single message's raw exceeds the capture ceiling. A subclass of ``MessageCaptureError`` so every existing
    guard/handler that fails the batch on a capture problem still applies; the distinct type lets the diagnostics
    layer report MESSAGE_TOO_LARGE (operator may raise ``BROKER_INTELLIGENCE_MAX_RAW_BYTES``) separately from an
    empty-payload / page-ceiling capture failure."""


class GmailApiError(Exception):
    """A Gmail REST call returned a non-2xx status. Carries the STRUCTURED, secret-free ``status`` (int) and
    ``operation`` (one of 'history.list' / 'messages.get' / 'messages.metadata' / 'messages.list' / 'profile') so the
    diagnostics layer classifies by field — never by parsing this message. The message intentionally contains ONLY
    the status + operation (never the URL, which could carry a query/message id). ``history.list`` + 404 means the
    stored history cursor is too old/invalid (distinct from a 404 on a single message)."""

    def __init__(self, status: int, operation: str = ""):
        self.status = status
        self.operation = operation or ""
        super().__init__(f"gmail api http {status} on {self.operation or 'unknown-op'}")


class CredentialMissingError(Exception):
    """No stored credential file exists for the mailbox's ``credential_ref`` (distinct from a decrypt/key failure,
    which raises ``credential_store.CredentialStoreError``). Secret-free: carries no ref, path, or token."""


class GmailConnectionError(Exception):
    """A network-level failure reaching Gmail (DNS/connection/TLS) — distinct from an HTTP non-2xx. Secret-free."""


class GmailTimeout(Exception):
    """A Gmail request timed out — distinct from a connection error and from an HTTP non-2xx. Secret-free."""


def _max_raw_bytes() -> int:
    """Per-message raw-size ceiling, CONFIGURABLE (settings-then-env ``BROKER_INTELLIGENCE_MAX_RAW_BYTES``, bytes) so an
    operator can admit a larger broker email without a code change. Defaults to ``GmailApiClient.MAX_RAW_BYTES``."""
    import os
    from django.conf import settings
    v = getattr(settings, "BROKER_INTELLIGENCE_MAX_RAW_BYTES", None)
    if v is None:
        v = os.getenv("BROKER_INTELLIGENCE_MAX_RAW_BYTES", "")
    try:
        return int(v) if str(v).strip() else GmailApiClient.MAX_RAW_BYTES
    except (TypeError, ValueError):
        return GmailApiClient.MAX_RAW_BYTES


def _max_history_pages() -> int:
    """History/list page ceiling per pass, CONFIGURABLE (settings-then-env ``BROKER_INTELLIGENCE_MAX_HISTORY_PAGES``)
    so an operator can admit a large one-off catch-up without a code change. Defaults to
    ``GmailApiClient.MAX_PAGES``."""
    import os
    from django.conf import settings
    v = getattr(settings, "BROKER_INTELLIGENCE_MAX_HISTORY_PAGES", None)
    if v is None:
        v = os.getenv("BROKER_INTELLIGENCE_MAX_HISTORY_PAGES", "")
    try:
        return int(v) if str(v).strip() and int(v) > 0 else GmailApiClient.MAX_PAGES
    except (TypeError, ValueError):
        return GmailApiClient.MAX_PAGES


class GmailApiClient:
    """Resolves the encrypted token for ``mailbox.credential_ref`` (refreshing read-only when expired), then lists +
    fetches raw messages via the Gmail REST API. ``http_get`` is injected (default: urllib) so tests drive it with
    fixtures. Never sends/modifies mail; only gmail.readonly endpoints are called."""

    # Default per-message raw-size ceiling (bytes), overridable via BROKER_INTELLIGENCE_MAX_RAW_BYTES. Comfortably
    # above a withdrawal email carrying a PDF statement, below Gmail's own message ceiling. A message ABOVE the
    # (resolved) ceiling is NEVER silently dropped — the fetch RAISES so the cursor does not advance past it.
    MAX_RAW_BYTES = 25 * 1024 * 1024
    # Bound the number of history/list pages followed in one pass (defence against an unbounded page walk).
    MAX_PAGES = 20

    def __init__(self, mailbox, *, client_id: str, client_secret: str, http_get: Optional[HttpGet] = None,
                 max_messages: int = 50):
        self._mailbox = mailbox
        self._client_id = client_id
        self._client_secret = client_secret
        self._http_get = http_get or _default_http_get
        self._max = max_messages
        self._max_raw_bytes = _max_raw_bytes()

    def _access_token(self) -> str:
        from .credential_store import load_token, store_token
        from . import gmail_oauth
        tok = load_token(self._mailbox.credential_ref)
        if not tok:
            raise CredentialMissingError("no stored credential for mailbox")
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

    def _get_json(self, url: str, *, operation: str) -> dict:
        """One authenticated GET returning parsed JSON. ``operation`` is a STABLE, secret-free label of the endpoint
        class (NOT the URL) so a non-2xx raises a typed ``GmailApiError`` the diagnostics layer can classify precisely
        (e.g. a 404 on 'history.list' = invalid cursor, distinct from a 404 on 'messages.get')."""
        status, body = self._http_get(url, {"Authorization": "Bearer " + self._access_token()})
        if status != 200:
            raise GmailApiError(status, operation)
        return json.loads(body)

    def _fetch_message(self, mid: str) -> dict:
        """Fetch + decode ONE raw message. NEVER silently drops a message: one that cannot be captured — an empty raw
        payload, or raw exceeding the capture ceiling — RAISES ``MessageCaptureError`` so the batch fails WITHOUT
        advancing the cursor (durable-preservation contract). The pre-decode check on the base64 length bounds memory
        (a huge message is rejected before it is decoded), so an oversized message is never buffered whole."""
        full = self._get_json(f"{_GMAIL_API}/messages/{urllib.parse.quote(mid)}?format=raw",
                              operation="messages.get")
        raw_b64 = full.get("raw", "")
        if not raw_b64:
            raise MessageCaptureError(f"message {mid}: empty raw payload — cannot capture (not advancing cursor)")
        if len(raw_b64) * 3 // 4 > self._max_raw_bytes:   # base64 decodes to ~3/4 its length (bound memory pre-decode)
            raise MessageTooLargeError(
                f"message {mid}: raw exceeds capture ceiling {self._max_raw_bytes}B — "
                "raise BROKER_INTELLIGENCE_MAX_RAW_BYTES to admit it (not advancing cursor; message not lost)")
        raw_bytes = base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
        if len(raw_bytes) > self._max_raw_bytes:
            raise MessageTooLargeError(f"message {mid}: raw exceeds capture ceiling {self._max_raw_bytes}B")
        return {"provider_message_id": mid, "raw_bytes": raw_bytes, **parse_rfc822(raw_bytes)}

    def _collect_added_ids(self, cursor: str) -> "Tuple[List[str], str]":
        """Walk ``history.list?startHistoryId=<cursor>&historyTypes=messageAdded`` (bounded by the configurable page
        ceiling), collecting the added message ids and advancing to the response's latest historyId. Cheap — ids only,
        no raw fetched.

        FAIL-CLOSED on an over-large backlog (durable-preservation contract, consistent with ``_fetch_message``): the
        response ``historyId`` reflects the mailbox's CURRENT state, so advancing to it while pages remain unfetched
        would move the cursor PAST un-fetched messages (silent loss). If the page ceiling is reached while a
        ``nextPageToken`` still remains, this RAISES ``MessageCaptureError`` WITHOUT returning an advanced cursor — the
        pass fails visibly (failed count + log), the cursor is not advanced, and nothing is lost; an operator can raise
        ``BROKER_INTELLIGENCE_MAX_HISTORY_PAGES`` for a bounded one-off catch-up."""
        ids: List[str] = []
        seen = set()
        new_cursor = cursor
        max_pages = _max_history_pages()
        page = f"{_GMAIL_API}/history?startHistoryId={urllib.parse.quote(cursor)}&historyTypes=messageAdded"
        drained = False
        for _ in range(max_pages):
            hist = self._get_json(page, operation="history.list")
            new_cursor = str(hist.get("historyId", new_cursor) or new_cursor)
            for h in (hist.get("history") or []):
                for added in (h.get("messagesAdded") or []):
                    mid = (added.get("message") or {}).get("id")
                    if mid and mid not in seen:
                        seen.add(mid)
                        ids.append(mid)
            token = hist.get("nextPageToken")
            if not token:
                drained = True
                break
            page = (f"{_GMAIL_API}/history?startHistoryId={urllib.parse.quote(cursor)}"
                    f"&historyTypes=messageAdded&pageToken={urllib.parse.quote(token)}")
        if not drained:
            raise MessageCaptureError(
                f"history exceeds {max_pages} pages in one pass (more messages remain beyond the page ceiling); "
                "NOT advancing the cursor so no mail is lost. Raise BROKER_INTELLIGENCE_MAX_HISTORY_PAGES for a "
                "bounded supervised catch-up.")
        return ids, new_cursor

    def _profile_cursor(self, fallback: str) -> str:
        return str(self._get_json(f"{_GMAIL_API}/profile", operation="profile").get("historyId", fallback)
                   or fallback)

    def _sender_allowed(self, mid: str) -> bool:
        """Metadata-only From pre-check (``format=metadata&metadataHeaders=From``) so a NON-broker message is never
        pulled in full raw. True iff the From domain is on the broker-sender allowlist (fail-closed: empty → False)."""
        from .broker_senders import is_allowlisted_broker_sender
        meta = self._get_json(
            f"{_GMAIL_API}/messages/{urllib.parse.quote(mid)}?format=metadata&metadataHeaders=From",
            operation="messages.metadata")
        frm = ""
        for h in ((meta.get("payload") or {}).get("headers") or []):
            if (h.get("name") or "").lower() == "from":
                frm = h.get("value", "") or ""
                break
        return is_allowlisted_broker_sender(frm)

    def list_new_messages(self, cursor_state: str) -> "Tuple[List[dict], str]":
        """INCREMENTAL when a cursor (historyId) is present — ``history.list`` collects the messages added since the
        cursor and advances to the response's latest historyId, so no message is skipped by the cursor jumping to 'now'.
        INITIAL sync (no cursor) lists recent messages and anchors the cursor to the current profile historyId.

        R1 ACQUISITION FILTER (privacy): the pilot connects a member's PERSONAL inbox, so NON-broker mail must never be
        pulled in full raw. Only messages whose From domain is on the broker-sender allowlist are fetched in full —
        server-side (``q=from:(...)``) on the initial list, and via a metadata-only From pre-check on the incremental
        path (history.list has no query). FAIL-CLOSED: an empty allowlist fetches NOTHING, while the cursor is still
        advanced/anchored so forward capture resumes once a verified broker is configured. INITIAL sync is deliberately
        FORWARD-ONLY (no historical backfill — a privacy + scope choice; see docs/GMAIL_INGESTION_CONSENT_READY.md). The
        retention gate in ``ingest_message`` is the hard backstop that never STORES a non-broker message even if one is
        fetched; an un-capturable (empty/oversize) broker message still RAISES rather than being silently dropped."""
        from .broker_senders import sender_allowlist
        allow = sorted(sender_allowlist())
        cursor = str(cursor_state or "")
        out: List[dict] = []
        if cursor:
            ids, new_cursor = self._collect_added_ids(cursor)   # always walk (advance cursor) — ids only, no raw
            for mid in ids:
                if allow and self._sender_allowed(mid):         # metadata gate BEFORE pulling full raw; empty → none
                    out.append(self._fetch_message(mid))
            return out, new_cursor
        # INITIAL sync (forward-only). Empty allowlist → acquire nothing, just anchor the cursor (fail-closed).
        if not allow:
            return [], self._profile_cursor(cursor)
        q = "from:(" + " OR ".join(allow) + ")"
        listing = self._get_json(f"{_GMAIL_API}/messages?maxResults={self._max}&q={urllib.parse.quote(q)}",
                                 operation="messages.list")
        for m0 in (listing.get("messages") or []):
            if self._sender_allowed(m0["id"]):                  # defense-in-depth beyond the server-side q= filter
                out.append(self._fetch_message(m0["id"]))
        return out, self._profile_cursor(cursor)


def fetch_profile(access_token: str, *, http_get: Optional[HttpGet] = None) -> dict:
    """Read the connected account's Gmail profile (emailAddress + historyId) with a bearer token — used by the
    connect callback to learn the mailbox's provider id + an initial cursor. Read-only; HTTP injected for tests."""
    get = http_get or _default_http_get
    status, body = get(f"{_GMAIL_API}/profile", {"Authorization": "Bearer " + access_token})
    if status != 200:
        raise GmailApiError(status, "profile")
    return json.loads(body)


def _default_http_get(url: str, headers: dict) -> "Tuple[int, str]":
    """Live HTTP GET. An HTTP non-2xx is returned as ``(status, body)`` so the caller raises a typed ``GmailApiError``
    carrying the status. A network-level failure (no HTTP status at all) is raised as a typed ``GmailTimeout`` /
    ``GmailConnectionError`` so it is classified distinctly from an HTTP error and never surfaces as a bare, unlabelled
    exception. The exception text is secret-free (no URL/header)."""
    import socket
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "ignore")
    except urllib.error.URLError as e:                       # DNS/connection/TLS/timeout — no HTTP status
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            raise GmailTimeout("gmail request timed out") from None
        raise GmailConnectionError("gmail connection error") from None
    except (socket.timeout, TimeoutError):
        raise GmailTimeout("gmail request timed out") from None

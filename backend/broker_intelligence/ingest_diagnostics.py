"""INGESTION_INCIDENT_R1 (§4) — controlled, secret-free failure classification for the mailbox ingestion worker.

The worker previously logged only ``type(exc).__name__`` for a failed mailbox pass (so a real 2026-10-14 outage
surfaced in production as the bare word "RuntimeError", with the HTTP status — which is NOT a secret — discarded).
This module is the authoritative mapping from a raised exception to a STABLE, secret-free diagnostic code.

DESIGN PRINCIPLES (Sponsor packet §4):
  * Classify by the exception TYPE and its STRUCTURED fields (``GmailApiError.status`` / ``.operation``,
    ``GmailOAuthError.status``), never by parsing a human message string. The Gmail-source layer now raises typed
    exceptions carrying those fields, so a "gmail api http 401 on history.list" is distinguishable from a
    "404 on messages.get" at the point of classification.
  * A Gmail ``history.list`` 404 means the stored history cursor is too old / invalid — materially different from a
    404 on ``messages.get`` (a single message gone). The two map to DIFFERENT codes (GMAIL_HISTORY_CURSOR_INVALID vs
    GMAIL_HTTP_404) so the operator response (§8) differs.
  * A generic ``RuntimeError`` that is NOT one of our typed exceptions is classified UNKNOWN_FAILURE — a precise
    condition is NEVER inferred from a bare RuntimeError.
  * The emitted ``safe_dict`` carries ONLY stable fields (code / http_status / operation / retryable / blocking /
    recovery). It NEVER carries the exception message, a URL, a Gmail message id, a mailbox address, a token, a
    header, or any raw Google error body — those can embed secrets or PII and are deliberately dropped.

Pure: no Django, no I/O, no flag. Import-safe (exception types imported defensively so a partial environment cannot
break classification)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


class ParserExecutionError(Exception):
    """Reserved typed error for a parser that raised during ``parse()``. Currently the ingestion pipeline ISOLATES a
    raising parser (treats it as 'declined' — evidence retained, no event, per the ingestion contract), so this does
    not propagate to the mailbox worker today; it exists so parser failures have a stable classification
    (MESSAGE_PARSE_FAILED) if a future change surfaces them structurally. The isolation contract is unchanged."""


class Code:
    """Stable diagnostic codes (the authoritative vocabulary). Strings, not an enum, so they serialise directly into a
    log line / health field and a new code can be added without a migration."""
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    CREDENTIAL_STORE_ERROR = "CREDENTIAL_STORE_ERROR"
    OAUTH_CLIENT_NOT_CONFIGURED = "OAUTH_CLIENT_NOT_CONFIGURED"   # the worker's own OAuth client id/secret is unset
    OAUTH_REFRESH_FAILED = "OAUTH_REFRESH_FAILED"
    OAUTH_PERMISSION_DENIED = "OAUTH_PERMISSION_DENIED"
    GMAIL_HTTP_400 = "GMAIL_HTTP_400"
    GMAIL_HTTP_401 = "GMAIL_HTTP_401"
    GMAIL_HTTP_403 = "GMAIL_HTTP_403"
    GMAIL_HTTP_404 = "GMAIL_HTTP_404"
    GMAIL_HTTP_429 = "GMAIL_HTTP_429"
    GMAIL_HTTP_5XX = "GMAIL_HTTP_5XX"
    GMAIL_HTTP_OTHER = "GMAIL_HTTP_OTHER"
    GMAIL_CONNECTION_ERROR = "GMAIL_CONNECTION_ERROR"
    GMAIL_TIMEOUT = "GMAIL_TIMEOUT"
    GMAIL_HISTORY_CURSOR_INVALID = "GMAIL_HISTORY_CURSOR_INVALID"
    MESSAGE_CAPTURE_FAILED = "MESSAGE_CAPTURE_FAILED"
    MESSAGE_TOO_LARGE = "MESSAGE_TOO_LARGE"
    MESSAGE_PARSE_FAILED = "MESSAGE_PARSE_FAILED"
    DATABASE_COMMIT_FAILED = "DATABASE_COMMIT_FAILED"
    UNKNOWN_FAILURE = "UNKNOWN_FAILURE"


# Recovery tokens — stable, secret-free hints consumed by the operator decision matrix (§8). NOT instructions the
# code acts on automatically (the worker never auto-reauths, never auto-anchors the cursor).
class Recovery:
    RETRY_BOUNDED = "RETRY_BOUNDED"               # transient; the existing 5-min cadence re-tries safely
    SPONSOR_REAUTH = "SPONSOR_REAUTH"             # likely consent/permission; needs Sponsor-approved re-auth
    ESCALATE_CURSOR = "ESCALATE_CURSOR"           # history cursor invalid; DO NOT auto-anchor — escalate (§6/§8)
    PRESERVE_AND_RETRY = "PRESERVE_AND_RETRY"     # durable write / capture failed; cursor held, idempotent re-try
    RAISE_SIZE_CEILING = "RAISE_SIZE_CEILING"     # a legitimately large email; raise MAX_RAW_BYTES deliberately
    OPERATOR_REVIEW = "OPERATOR_REVIEW"           # needs a human look; never silently recovered
    NONE_EVIDENCE_RETAINED = "NONE_EVIDENCE_RETAINED"   # non-fatal per-message; evidence kept, no event


# Codes that mean the mailbox cannot make progress without an out-of-band (operator/Sponsor) action. A mailbox whose
# last failure is one of these is RECOVERY_REQUIRED regardless of how recently it was attempted.
BLOCKING_CODES = frozenset({
    Code.CREDENTIAL_MISSING, Code.CREDENTIAL_STORE_ERROR, Code.OAUTH_CLIENT_NOT_CONFIGURED,
    Code.OAUTH_PERMISSION_DENIED, Code.GMAIL_HTTP_401, Code.GMAIL_HTTP_403, Code.GMAIL_HISTORY_CURSOR_INVALID,
})


@dataclass(frozen=True)
class IngestDiagnostic:
    """A classified failure. ``http_status``/``operation`` are present only when known (never fabricated)."""
    code: str
    http_status: Optional[int] = None
    operation: str = ""
    retryable: bool = False
    blocking: bool = False
    recovery: str = Recovery.OPERATOR_REVIEW

    def safe_dict(self) -> dict:
        """The ONLY representation that may be logged / persisted / returned. Stable fields only — no message text,
        URL, message id, address, token, header, or raw Google body."""
        return {
            "code": self.code,
            "http_status": self.http_status,
            "operation": self.operation or "",
            "retryable": self.retryable,
            "blocking": self.blocking,
            "recovery": self.recovery,
        }


def _http_code_for_status(status: Optional[int]) -> str:
    if status == 400:
        return Code.GMAIL_HTTP_400
    if status == 401:
        return Code.GMAIL_HTTP_401
    if status == 403:
        return Code.GMAIL_HTTP_403
    if status == 404:
        return Code.GMAIL_HTTP_404
    if status == 429:
        return Code.GMAIL_HTTP_429
    if status is not None and 500 <= status <= 599:
        return Code.GMAIL_HTTP_5XX
    return Code.GMAIL_HTTP_OTHER


def _diag_for_http(status: Optional[int], operation: str) -> IngestDiagnostic:
    # history.list 404 == the stored startHistoryId is too old/invalid (NOT a missing single message). Distinct code
    # + ESCALATE_CURSOR: the cursor must NOT be auto-anchored to the current profile historyId (§6/§8).
    if operation == "history.list" and status == 404:
        return IngestDiagnostic(Code.GMAIL_HISTORY_CURSOR_INVALID, http_status=404, operation=operation,
                                retryable=False, blocking=True, recovery=Recovery.ESCALATE_CURSOR)
    code = _http_code_for_status(status)
    if code in (Code.GMAIL_HTTP_401, Code.GMAIL_HTTP_403):
        return IngestDiagnostic(code, http_status=status, operation=operation,
                                retryable=False, blocking=True, recovery=Recovery.SPONSOR_REAUTH)
    if code in (Code.GMAIL_HTTP_429, Code.GMAIL_HTTP_5XX):
        return IngestDiagnostic(code, http_status=status, operation=operation,
                                retryable=True, blocking=False, recovery=Recovery.RETRY_BOUNDED)
    # 400 / 404-on-a-non-history-op / other: not safely retryable on its own and not a known blocking-reauth; review.
    return IngestDiagnostic(code, http_status=status, operation=operation,
                            retryable=False, blocking=False, recovery=Recovery.OPERATOR_REVIEW)


def classify_exception(exc: BaseException) -> IngestDiagnostic:
    """Map a raised exception to a stable, secret-free ``IngestDiagnostic``. Checks specific types before general ones;
    a bare ``RuntimeError`` that is none of the typed exceptions is UNKNOWN_FAILURE (never inferred to be an HTTP
    error). Import-safe: exception types are resolved defensively so a partial import cannot break classification."""
    # Resolve our typed exceptions lazily + defensively (a missing import degrades to isinstance-False, never raises).
    try:
        from .gmail_source import (GmailApiError, GmailConnectionError, GmailTimeout, CredentialMissingError,
                                   MessageCaptureError, MessageTooLargeError)
    except Exception:  # noqa: BLE001
        GmailApiError = GmailConnectionError = GmailTimeout = CredentialMissingError = ()  # type: ignore
        MessageCaptureError = MessageTooLargeError = ()  # type: ignore
    try:
        from .gmail_oauth import GmailOAuthError
    except Exception:  # noqa: BLE001
        GmailOAuthError = ()  # type: ignore
    try:
        from .credential_store import CredentialStoreError
    except Exception:  # noqa: BLE001
        CredentialStoreError = ()  # type: ignore
    try:
        from .evidence import EvidenceStoreError
    except Exception:  # noqa: BLE001
        EvidenceStoreError = ()  # type: ignore
    try:
        from django.db import Error as DjangoDbError
    except Exception:  # noqa: BLE001
        DjangoDbError = ()  # type: ignore

    def _is(e, t):
        return bool(t) and isinstance(e, t)

    # --- credential layer -------------------------------------------------------------------------------------------
    if _is(exc, CredentialMissingError):
        return IngestDiagnostic(Code.CREDENTIAL_MISSING, retryable=False, blocking=True,
                                recovery=Recovery.OPERATOR_REVIEW)
    if _is(exc, CredentialStoreError):
        return IngestDiagnostic(Code.CREDENTIAL_STORE_ERROR, retryable=False, blocking=True,
                                recovery=Recovery.OPERATOR_REVIEW)

    # --- OAuth refresh ----------------------------------------------------------------------------------------------
    if _is(exc, GmailOAuthError):
        status = getattr(exc, "status", None)
        if status in (400, 401, 403):     # invalid_grant / revoked / forbidden -> permission denied (needs re-auth)
            return IngestDiagnostic(Code.OAUTH_PERMISSION_DENIED, http_status=status, operation="oauth.refresh",
                                    retryable=False, blocking=True, recovery=Recovery.SPONSOR_REAUTH)
        return IngestDiagnostic(Code.OAUTH_REFRESH_FAILED, http_status=status, operation="oauth.refresh",
                                retryable=True, blocking=False, recovery=Recovery.RETRY_BOUNDED)

    # --- Gmail transport --------------------------------------------------------------------------------------------
    if _is(exc, GmailTimeout):
        return IngestDiagnostic(Code.GMAIL_TIMEOUT, retryable=True, blocking=False, recovery=Recovery.RETRY_BOUNDED)
    if _is(exc, GmailConnectionError):
        return IngestDiagnostic(Code.GMAIL_CONNECTION_ERROR, retryable=True, blocking=False,
                                recovery=Recovery.RETRY_BOUNDED)
    if _is(exc, GmailApiError):
        return _diag_for_http(getattr(exc, "status", None), getattr(exc, "operation", "") or "")

    # --- per-message capture (check the TooLarge subclass before its MessageCaptureError base) ----------------------
    if _is(exc, MessageTooLargeError):
        return IngestDiagnostic(Code.MESSAGE_TOO_LARGE, retryable=False, blocking=False,
                                recovery=Recovery.RAISE_SIZE_CEILING)
    if _is(exc, MessageCaptureError):
        return IngestDiagnostic(Code.MESSAGE_CAPTURE_FAILED, retryable=True, blocking=False,
                                recovery=Recovery.PRESERVE_AND_RETRY)

    # --- parse (reserved: parser errors are currently isolated inside ingest_message and do not reach the worker) ---
    if _is(exc, ParserExecutionError):
        return IngestDiagnostic(Code.MESSAGE_PARSE_FAILED, retryable=False, blocking=False,
                                recovery=Recovery.NONE_EVIDENCE_RETAINED)

    # --- durable persistence (evidence store is a RuntimeError subclass -> check BEFORE the RuntimeError fallback) --
    if _is(exc, EvidenceStoreError):
        return IngestDiagnostic(Code.DATABASE_COMMIT_FAILED, operation="evidence.store",
                                retryable=True, blocking=False, recovery=Recovery.PRESERVE_AND_RETRY)
    if _is(exc, DjangoDbError):
        return IngestDiagnostic(Code.DATABASE_COMMIT_FAILED, operation="db",
                                retryable=True, blocking=False, recovery=Recovery.PRESERVE_AND_RETRY)

    # --- unknown: NEVER inferred to be an HTTP error from a bare RuntimeError (§4) ----------------------------------
    return IngestDiagnostic(Code.UNKNOWN_FAILURE, retryable=False, blocking=False, recovery=Recovery.OPERATOR_REVIEW)

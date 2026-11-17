"""WITHDRAWAL-OAUTH-OBSERVABILITY (pkt-149, ADR-0113) — value-blind mailbox OAuth health + alert model.

WHY THIS EXISTS. Twice (2026-11-08 and again 2026-11-15) both pilot Gmail grants expired together — consistent with
the OAuth app being in Google "Testing" publishing status (a HIGH-CONFIDENCE INFERENCE pending Google Cloud Console
confirmation; Google's documented Testing-mode behaviour of expiring refresh tokens roughly every seven days is the
VERIFIED part — see WITHDRAWAL_V1_OAUTH_PRODUCTION_READINESS_CHECKLIST.md §1) — and the Withdrawal Intelligence
ingestion plane went blind for ~8 days while ``consecutive_failures`` silently climbed to 797.
Nothing distinguished "the owner must re-consent" from a transient Google blip, and no early signal fired before a
prolonged blindness. This module is the ADDITIVE observability layer that fixes exactly that: it classifies the health
read-model into explicit, operator-actionable statuses, assigns an alert severity against bounded thresholds, and
decides (purely, with dedup/cooldown) when to raise/clear an alert — WITHOUT ever touching Gmail, a token, a credential
or a cursor.

STRICTLY ADDITIVE / BEHAVIOUR-PRESERVING. It does not replace ``health.mailbox_health`` / ``customer_mailbox_status`` /
``service_health`` (the frontend + the existing command depend on their contracts). It layers on TOP of the secret-free
per-mailbox dicts those already produce — so it reads no model field they do not, and introduces no migration.

VALUE-BLIND BY CONSTRUCTION. Every input is already a secret-free ``mailbox_health`` dict (masked email, stable
diagnostic code, HTTP status, counts, booleans). This module NEVER reads or returns a token, refresh token,
authorization code, client secret, credential ref, raw cursor value, email subject/body, or a full email address — only
the established ``masked_email`` (first two local chars + full domain) is passed through. It mutates nothing and
performs no I/O beyond the read-only ``service_health`` aggregate.

STATUS VOCABULARY (the seven explicit operator statuses of the packet). A failure is classified from the stable
``last_error_code`` + the mailbox connection ``status`` + staleness — never by parsing a message string:

    HEALTHY                       a recent successful sync; no blocking code; counter at zero (incl. "no new mail")
    TRANSIENT_FAILURE             a retryable Google error (429 / 5xx / connect / timeout / refresh), not yet persisted
    DEGRADED                      a failure that has persisted (aged past the stale threshold or N consecutive), or a
                                  bounded/cursor/other error that blocks progress but is neither re-consent nor config
    RECONSENT_REQUIRED            the owner must re-authorize Gmail (status ERROR/REVOKED, or a re-auth code:
                                  invalid_grant / 401 / 403 → OAUTH_PERMISSION_DENIED). This is the 7-day-expiry case.
    SECURITY_CONFIGURATION_ERROR  a server-side OAuth client / credential posture fault (client unset, credential
                                  missing / unreadable) — an operator fix, never an owner reconnect
    SYNCHRONIZER_STALLED          the ingestion worker itself is not running (no attempt recorded across connected
                                  mailboxes for longer than the stall threshold) — distinct from any one mailbox failing
    UNKNOWN_FAIL_CLOSED           an unrecognised / bare failure — fail closed (treated as needing attention, never
                                  silently HEALTHY)

The crucial distinction the incident lacked — RECONSENT_REQUIRED vs TRANSIENT_FAILURE — is drawn here, structurally.
"""
from __future__ import annotations

from typing import Optional

from . import health as _health
from .ingest_diagnostics import Code
# REAUTH_CODES lives in health.py (the strict re-auth subset of BLOCKING_CODES). Importing it keeps the
# "which codes mean the OWNER must reconnect" decision defined in exactly one place.
from .health import REAUTH_CODES


# --- the seven explicit statuses ---------------------------------------------------------------------------------
HEALTHY = "HEALTHY"
TRANSIENT_FAILURE = "TRANSIENT_FAILURE"
DEGRADED = "DEGRADED"
RECONSENT_REQUIRED = "RECONSENT_REQUIRED"
SECURITY_CONFIGURATION_ERROR = "SECURITY_CONFIGURATION_ERROR"
SYNCHRONIZER_STALLED = "SYNCHRONIZER_STALLED"
UNKNOWN_FAIL_CLOSED = "UNKNOWN_FAIL_CLOSED"

ALL_STATUSES = (HEALTHY, TRANSIENT_FAILURE, DEGRADED, RECONSENT_REQUIRED,
                SECURITY_CONFIGURATION_ERROR, SYNCHRONIZER_STALLED, UNKNOWN_FAIL_CLOSED)

# --- alert severity vocabulary (worst-first) ---------------------------------------------------------------------
# Maps to the existing operational_events severities at the DELIVERY layer (WARNING→WARNING, DEGRADED→ERROR,
# CRITICAL→CRITICAL); kept as its own vocabulary so the health model does not import the event app.
SEV_OK = "OK"
SEV_INFO = "INFO"
SEV_WARNING = "WARNING"
SEV_DEGRADED = "DEGRADED"
SEV_CRITICAL = "CRITICAL"
_SEV_ORDER = {SEV_OK: 0, SEV_INFO: 1, SEV_WARNING: 2, SEV_DEGRADED: 3, SEV_CRITICAL: 4}


def _worse_sev(a: str, b: str) -> str:
    return a if _SEV_ORDER.get(a, 0) >= _SEV_ORDER.get(b, 0) else b


# --- fine-grained failure class (the Phase-1 taxonomy, coarser statuses above) -----------------------------------
FC_NONE = "none"
FC_REAUTH = "reauth"
FC_CLIENT_CONFIG = "client_config"
FC_CREDENTIAL = "credential"
FC_CURSOR_INVALID = "cursor_invalid"
FC_RATE_LIMIT = "rate_limit"
FC_SERVER_5XX = "google_5xx"
FC_NETWORK = "network"
FC_REFRESH = "refresh"
FC_MESSAGE = "message_bounded"
FC_PERSISTENCE = "persistence"
FC_HTTP_REVIEW = "http_review"
FC_STALE_NO_ERROR = "stale_no_error"
FC_SYNCHRONIZER = "synchronizer_stalled"
FC_UNKNOWN = "unknown"

# --- code groupings (derived from the authoritative ingest_diagnostics vocabulary) -------------------------------
# Server-side OAuth CLIENT / CREDENTIAL posture faults — an OPERATOR fix (never an owner reconnect).
_SECURITY_CONFIG_CODES = frozenset({
    Code.OAUTH_CLIENT_NOT_CONFIGURED, Code.CREDENTIAL_MISSING, Code.CREDENTIAL_STORE_ERROR,
})
# Retryable transport / refresh errors — the bounded 5-min cadence re-tries them safely.
_TRANSIENT_CODES = frozenset({
    Code.OAUTH_REFRESH_FAILED, Code.GMAIL_HTTP_429, Code.GMAIL_HTTP_5XX,
    Code.GMAIL_CONNECTION_ERROR, Code.GMAIL_TIMEOUT,
})
# Per-message / durable-write failures — bounded, do not block the whole mailbox, surfaced as DEGRADED if current.
_BOUNDED_MESSAGE_CODES = frozenset({
    Code.MESSAGE_CAPTURE_FAILED, Code.MESSAGE_TOO_LARGE, Code.MESSAGE_PARSE_FAILED, Code.DATABASE_COMMIT_FAILED,
})
# Recognised-but-needs-review HTTP codes (not safely retryable, not a known re-auth): surfaced as DEGRADED.
_HTTP_REVIEW_CODES = frozenset({Code.GMAIL_HTTP_400, Code.GMAIL_HTTP_404, Code.GMAIL_HTTP_OTHER})

_FAILURE_CLASS_FOR_CODE = {
    Code.OAUTH_PERMISSION_DENIED: FC_REAUTH, Code.GMAIL_HTTP_401: FC_REAUTH, Code.GMAIL_HTTP_403: FC_REAUTH,
    Code.OAUTH_CLIENT_NOT_CONFIGURED: FC_CLIENT_CONFIG,
    Code.CREDENTIAL_MISSING: FC_CREDENTIAL, Code.CREDENTIAL_STORE_ERROR: FC_CREDENTIAL,
    Code.GMAIL_HISTORY_CURSOR_INVALID: FC_CURSOR_INVALID,
    Code.GMAIL_HTTP_429: FC_RATE_LIMIT, Code.GMAIL_HTTP_5XX: FC_SERVER_5XX,
    Code.GMAIL_CONNECTION_ERROR: FC_NETWORK, Code.GMAIL_TIMEOUT: FC_NETWORK,
    Code.OAUTH_REFRESH_FAILED: FC_REFRESH,
    Code.MESSAGE_CAPTURE_FAILED: FC_MESSAGE, Code.MESSAGE_TOO_LARGE: FC_MESSAGE,
    Code.MESSAGE_PARSE_FAILED: FC_MESSAGE, Code.DATABASE_COMMIT_FAILED: FC_PERSISTENCE,
    Code.GMAIL_HTTP_400: FC_HTTP_REVIEW, Code.GMAIL_HTTP_404: FC_HTTP_REVIEW, Code.GMAIL_HTTP_OTHER: FC_HTTP_REVIEW,
    Code.UNKNOWN_FAILURE: FC_UNKNOWN,
}

# --- thresholds (Phase 3) ----------------------------------------------------------------------------------------
# "3 consecutive blocking failures → WARNING". Age thresholds reuse the health read-model's own stale (15 min →
# DEGRADED) and recovery (60 min → CRITICAL) tunables, so there is ONE source of truth for the cadence maths. The
# synchronizer-stall threshold is the recovery threshold (no attempt in an hour across connected mailboxes = the
# worker is not running). Alert de-dup cooldown is 30 min by default.
WARN_CONSECUTIVE = 3
_DEFAULT_ALERT_COOLDOWN_SECONDS = 30 * 60

_NON_HEALTHY_BASE = (_health.MAILBOX_SYNC_FAILED, _health.MAILBOX_STALE, _health.RECOVERY_REQUIRED)


def _persisted(consecutive: int, elapsed: Optional[int], degraded_age: int, has_error: bool) -> bool:
    """A failure has 'persisted' (escalate transient → DEGRADED) when it has recurred ``WARN_CONSECUTIVE`` times, OR
    the mailbox has not synced for longer than the stale threshold, OR it has never synced while an error is on record
    (``elapsed is None`` with a recorded failure is a mailbox that has been failing with no success to age against)."""
    if consecutive >= WARN_CONSECUTIVE:
        return True
    if elapsed is not None and elapsed > degraded_age:
        return True
    if elapsed is None and has_error:
        return True
    return False


def _severity_for(status: str, *, consecutive: int, elapsed: Optional[int],
                  degraded_age: int, critical_age: int) -> str:
    """Severity against the Phase-3 thresholds. The hard-actionable statuses floor at CRITICAL; everything else
    escalates by consecutive-failure count and by age-without-success, and the WORST contribution wins."""
    if status == HEALTHY:
        return SEV_OK
    if status in (RECONSENT_REQUIRED, SECURITY_CONFIGURATION_ERROR, SYNCHRONIZER_STALLED):
        return SEV_CRITICAL
    # TRANSIENT_FAILURE / DEGRADED / UNKNOWN_FAIL_CLOSED escalate by count + age.
    sev = SEV_INFO
    if status == UNKNOWN_FAIL_CLOSED:
        sev = SEV_WARNING                      # fail-closed: an unclassified failure is never merely informational
    if consecutive >= WARN_CONSECUTIVE:
        sev = _worse_sev(sev, SEV_WARNING)
    if elapsed is not None and elapsed > degraded_age:
        sev = _worse_sev(sev, SEV_DEGRADED)
    if elapsed is not None and elapsed > critical_age:
        sev = _worse_sev(sev, SEV_CRITICAL)
    if elapsed is None and status != TRANSIENT_FAILURE:
        # never synced and not a fresh transient = a standing problem
        sev = _worse_sev(sev, SEV_DEGRADED)
    return sev


_RECOMMENDED_ACTION = {
    HEALTHY: "",
    RECONSENT_REQUIRED: "Reconnect required — the mailbox owner must re-authorize Gmail (OAuth consent).",
    SECURITY_CONFIGURATION_ERROR: "Operator review — OAuth client / credential configuration fault; no owner action.",
    SYNCHRONIZER_STALLED: "Operator review — mailbox ingestion worker not running; restart the ingestion timer/service.",
    TRANSIENT_FAILURE: "No action — transient Google error; the next scheduled pass retries.",
    UNKNOWN_FAIL_CLOSED: "Operator review — unclassified ingestion failure (fail-closed).",
}


def _recommended_action(status: str, failure_class: str) -> str:
    if status == DEGRADED:
        if failure_class == FC_CURSOR_INVALID:
            return "Operator review — history cursor invalid; bounded resync required (do NOT auto-anchor)."
        return "Monitor — ingestion delayed; new withdrawal notifications may be delayed until it recovers."
    return _RECOMMENDED_ACTION.get(status, "Operator review.")


def classify_mailbox_oauth_health(base: dict, *, synchronizer_stalled: bool = False,
                                  degraded_age: Optional[int] = None,
                                  critical_age: Optional[int] = None) -> dict:
    """Classify ONE mailbox into the seven-status model from its secret-free ``health.mailbox_health`` dict.

    ``base`` MUST be the dict returned by ``health.mailbox_health`` (so this function reads no model field that the
    read-model does not already expose — value-blind by construction). ``synchronizer_stalled`` is the aggregate's
    determination that the worker itself is not running; when true, a mailbox that is merely stale with NO specific
    error is attributed to the dead worker (SYNCHRONIZER_STALLED) rather than blamed individually. Pure; no I/O."""
    degraded_age = base.get("stale_seconds") if degraded_age is None else degraded_age
    critical_age = base.get("recovery_seconds") if critical_age is None else critical_age

    code = base.get("last_error_code") or ""
    conn_status = base.get("status") or ""
    consecutive = int(base.get("consecutive_failures") or 0)
    elapsed = base.get("elapsed_since_sync_seconds")
    base_state = base.get("state")
    has_error = bool(code) or consecutive > 0

    if base_state == _health.MAILBOX_SYNC_SUCCESSFUL:
        # A recent successful pass — including the "no new mail, cursor did not advance" case (cursor non-advance is
        # NEVER a fault on its own). Healthy.
        status = HEALTHY
        failure_class = FC_NONE
    elif conn_status in ("REVOKED", "ERROR") or code in REAUTH_CODES:
        # Explicit re-auth evidence: the owner must reconnect Gmail. This is the 7-day Testing-mode expiry signature.
        status = RECONSENT_REQUIRED
        failure_class = FC_REAUTH
    elif code in _SECURITY_CONFIG_CODES:
        status = SECURITY_CONFIGURATION_ERROR
        failure_class = _FAILURE_CLASS_FOR_CODE.get(code, FC_CLIENT_CONFIG)
    elif code == Code.GMAIL_HISTORY_CURSOR_INVALID:
        status = DEGRADED
        failure_class = FC_CURSOR_INVALID
    elif code in _TRANSIENT_CODES:
        failure_class = _FAILURE_CLASS_FOR_CODE.get(code, FC_NETWORK)
        status = DEGRADED if _persisted(consecutive, elapsed, degraded_age, has_error) else TRANSIENT_FAILURE
    elif code in _BOUNDED_MESSAGE_CODES:
        status = DEGRADED
        failure_class = _FAILURE_CLASS_FOR_CODE.get(code, FC_MESSAGE)
    elif code in _HTTP_REVIEW_CODES:
        status = DEGRADED
        failure_class = FC_HTTP_REVIEW
    elif code == "" and base_state in (_health.MAILBOX_STALE, _health.RECOVERY_REQUIRED):
        # Stale / recovery-by-time with NO recorded error. If the worker is stalled fleet-wide, attribute it there;
        # otherwise it is an individually degraded mailbox.
        status = SYNCHRONIZER_STALLED if synchronizer_stalled else DEGRADED
        failure_class = FC_SYNCHRONIZER if synchronizer_stalled else FC_STALE_NO_ERROR
    elif code == Code.UNKNOWN_FAILURE:
        status = UNKNOWN_FAIL_CLOSED
        failure_class = FC_UNKNOWN
    else:
        # An error code we do not recognise in any bucket — fail closed (never silently HEALTHY).
        status = UNKNOWN_FAIL_CLOSED
        failure_class = FC_UNKNOWN

    severity = _severity_for(status, consecutive=consecutive, elapsed=elapsed,
                             degraded_age=degraded_age, critical_age=critical_age)
    recovery_required = status in (RECONSENT_REQUIRED, SECURITY_CONFIGURATION_ERROR, SYNCHRONIZER_STALLED)
    return {
        "mailbox_id": base.get("mailbox_id"),
        "provider": base.get("provider"),
        "masked_email": base.get("masked_email"),        # the only identity ever emitted (first 2 chars + domain)
        "connection_status": conn_status,
        "oauth_status": status,
        "failure_class": failure_class,
        "alert_severity": severity,
        "recovery_required": recovery_required,
        "reconsent_required": status == RECONSENT_REQUIRED,
        "recommended_action": _recommended_action(status, failure_class),
        "last_error_code": code,                          # stable diagnostic code (never a message/URL/token)
        "last_error_http_status": base.get("last_error_http_status"),
        "consecutive_failures": consecutive,
        "age_since_last_success_seconds": elapsed,
        "last_successful_sync": base.get("last_successful_sync"),
        "last_attempted_sync": base.get("last_attempted_sync"),
        "cursor_present": bool(base.get("cursor_present")),
        "last_pass_cursor_advanced": base.get("last_pass_cursor_advanced"),
    }


def _detect_synchronizer_stalled(mailbox_dicts, *, recovery_seconds: int, r1_columns_present: bool) -> bool:
    """True iff there is at least one CONNECTED mailbox but the WORKER appears not to be running: the newest
    ``last_attempted_sync`` across them is older than the stall threshold (the recovery window), or — on a MIGRATED
    schema — no attempt has ever been recorded for any of them (the worker has never run). Value-blind (timestamps
    only).

    UNMIGRATED-SAFE: when ``r1_columns_present`` is False the ``last_attempted_sync`` column does not exist, so every
    row's attempt age is None NOT because the worker is idle but because the field is absent. In that mode we can NEVER
    prove a stall (it would false-positive on perfectly healthy, actively-syncing mailboxes), so we assert none — the
    read-model's unmigrated contract: never invent a diagnosis that staleness alone cannot support."""
    connected = [m for m in mailbox_dicts if (m.get("status") or "") == "CONNECTED"]
    if not connected:
        return False
    # If ANY connected mailbox was attempted recently, the worker is running.
    newest_attempt_age = None
    for m in connected:
        age = m.get("elapsed_since_attempt_seconds")
        if age is not None:
            newest_attempt_age = age if newest_attempt_age is None else min(newest_attempt_age, age)
    if newest_attempt_age is not None:
        return newest_attempt_age > recovery_seconds
    # No attempt timestamp on ANY connected mailbox. On a migrated schema that is a worker that has never run (a real
    # stall); on an unmigrated schema the column is merely absent and we must not claim a stall.
    return bool(r1_columns_present)


def _augment_attempt_age(base_rows, *, now):
    """Add ``elapsed_since_attempt_seconds`` (int or None) to each ``mailbox_health`` dict without mutating the input.
    Derived from ``last_attempted_sync`` (an ISO string in the dict) — value-blind."""
    import datetime
    out = []
    for r in base_rows:
        r = dict(r)
        ts = r.get("last_attempted_sync")
        age = None
        if ts:
            try:
                dt = datetime.datetime.fromisoformat(ts)
                age = int((now - dt).total_seconds())
            except (TypeError, ValueError):
                age = None
        r["elapsed_since_attempt_seconds"] = age
        out.append(r)
    return out


def aggregate_oauth_health(*, now=None, include_all_statuses: bool = True) -> dict:
    """The read-only operator coverage projection: every mailbox classified into the seven-status model plus fleet
    counts and the synchronizer-stall determination. Reuses ``health.service_health`` (so it is unmigrated-safe and
    secret-free) and never reads the model directly. MUTATES NOTHING.

    Value-blind: emits masked identities, stable codes, counts and ages only — never a token/cursor/credential/body."""
    from django.utils import timezone
    now = now or timezone.now()
    svc = _health.service_health(now=now, include_all_statuses=include_all_statuses)
    rows = _augment_attempt_age(svc.get("mailboxes", []), now=now)
    recovery_seconds = int(svc.get("recovery_seconds") or _health._DEFAULT_RECOVERY_SECONDS)
    synchronizer_stalled = _detect_synchronizer_stalled(
        rows, recovery_seconds=recovery_seconds, r1_columns_present=bool(svc.get("r1_columns_present")))

    classified = [classify_mailbox_oauth_health(r, synchronizer_stalled=synchronizer_stalled) for r in rows]

    counts = {s: 0 for s in ALL_STATUSES}
    worst_sev = SEV_OK
    for c in classified:
        counts[c["oauth_status"]] = counts.get(c["oauth_status"], 0) + 1
        worst_sev = _worse_sev(worst_sev, c["alert_severity"])
    reconsent_n = counts[RECONSENT_REQUIRED]
    healthy_n = counts[HEALTHY]
    degraded_like = (counts[DEGRADED] + counts[TRANSIENT_FAILURE] + counts[SECURITY_CONFIGURATION_ERROR]
                     + counts[SYNCHRONIZER_STALLED] + counts[UNKNOWN_FAIL_CLOSED])

    return {
        "generated_at": now.isoformat(),
        "r1_columns_present": svc.get("r1_columns_present"),
        "stale_seconds": svc.get("stale_seconds"),
        "recovery_seconds": recovery_seconds,
        "monitored_mailbox_count": len(classified),
        "healthy_count": healthy_n,
        "degraded_count": degraded_like,
        "reconsent_required_count": reconsent_n,
        "synchronizer_stalled": synchronizer_stalled,
        "any_reconsent_required": reconsent_n > 0,
        "any_recovery_required": any(c["recovery_required"] for c in classified),
        "worst_alert_severity": worst_sev,
        "counts_by_status": counts,
        "mailboxes": classified,
        # A single value-blind FLEET summary line for the STAFF operator projection (never names a mailbox).
        "operator_notice": _fleet_notice(reconsent_n, degraded_like, synchronizer_stalled),
    }


def _fleet_notice(reconsent_n: int, degraded_n: int, synchronizer_stalled: bool) -> Optional[str]:
    """A truthful, value-blind one-line FLEET summary for the STAFF operator projection — or None when all healthy.

    This reflects FLEET-WIDE counts and is returned ONLY by the staff-gated operator aggregate; it is NOT a per-owner
    message and must never be shown to a member. The OWNER banner derives independently from the owner-scoped
    ``/mailboxes/status/`` payload (never from here), so fleet state cannot reach another owner through this field."""
    if reconsent_n > 0:
        return (f"{reconsent_n} connected mailbox(es) need owner re-consent; new withdrawal notifications are delayed "
                "for them until reconnected.")
    if synchronizer_stalled or degraded_n > 0:
        return "Email ingestion is degraded; new withdrawal notifications may be delayed until it recovers."
    return None


# =================================================================================================================
# Alert decision — PURE dedup / cooldown / recovery. No store, no I/O: the caller supplies the previously-emitted
# alert snapshot (``prev``) and ``now``; the delivery layer (the DARK systemd timer's OnFailure, or a future
# operational_events writer) persists state and acts on the decision. This is the Phase-3 alert gate, fully testable.
# =================================================================================================================
ALERT_NONE = "NONE"           # healthy, nothing was alerting → do nothing
ALERT_OPEN = "OPEN"           # a new alert condition (no prior alert, or the status changed)
ALERT_UPDATE = "UPDATE"       # same condition, but the severity changed (escalation / de-escalation)
ALERT_REMIND = "REMIND"       # same condition + severity, but the cooldown has elapsed → re-emit (anti-forget)
ALERT_SUPPRESS = "SUPPRESS"   # same condition + severity, within cooldown → stay quiet (anti-spam)
ALERT_RECOVER = "RECOVER"     # was alerting, now healthy → emit ONE recovery event and clear


def dedup_key(mailbox_id, status: str) -> str:
    """Stable, value-blind alert key. Status-scoped so a change of condition (e.g. TRANSIENT_FAILURE →
    RECONSENT_REQUIRED) is a NEW alert, not a suppressed duplicate. Never contains an address/token."""
    return f"mailbox-oauth:{mailbox_id}:{status}"


def decide_alert(prev: Optional[dict], current: dict, *, now, cooldown_seconds: Optional[int] = None) -> dict:
    """Decide whether to raise/clear an alert for one mailbox, with de-duplication and a cooldown.

    ``prev``  — the last EMITTED alert snapshot for this mailbox: ``{"dedup_key", "severity", "emitted_at"}`` (an ISO
                string or aware datetime for ``emitted_at``), or None if nothing is currently alerting.
    ``current`` — a classified record from ``classify_mailbox_oauth_health``.
    Returns ``{"action", "severity", "dedup_key", "oauth_status", "mailbox_id", "recommended_action"}``. Pure."""
    cooldown_seconds = _DEFAULT_ALERT_COOLDOWN_SECONDS if cooldown_seconds is None else cooldown_seconds
    status = current["oauth_status"]
    severity = current["alert_severity"]
    mid = current.get("mailbox_id")
    key = dedup_key(mid, status)

    def _out(action):
        return {
            "action": action,
            "severity": severity if action != ALERT_RECOVER else SEV_OK,
            "dedup_key": key,
            "oauth_status": status,
            "mailbox_id": mid,
            "recommended_action": current.get("recommended_action", ""),
        }

    if status == HEALTHY:
        # Healthy now. If something WAS alerting, emit exactly one recovery event; else nothing.
        return _out(ALERT_RECOVER) if prev else _out(ALERT_NONE)

    if not prev or prev.get("dedup_key") != key:
        return _out(ALERT_OPEN)                       # new condition (or condition changed)
    if prev.get("severity") != severity:
        return _out(ALERT_UPDATE)                      # same condition, severity moved
    # Same condition + severity: honour the cooldown.
    emitted_at = prev.get("emitted_at")
    elapsed = _elapsed_seconds(emitted_at, now)
    if elapsed is None:
        # The cooldown cannot be computed. If a timestamp WAS provided but is unusable (e.g. a delivery-layer
        # naive-vs-aware datetime mismatch), stay quiet — anti-spam — so a timestamp bug can never defeat the dedup.
        # Only re-emit when NO prior timestamp was recorded at all.
        return _out(ALERT_SUPPRESS if emitted_at is not None else ALERT_REMIND)
    if elapsed >= cooldown_seconds:
        return _out(ALERT_REMIND)
    return _out(ALERT_SUPPRESS)


def _elapsed_seconds(emitted_at, now) -> Optional[int]:
    import datetime
    if emitted_at is None:
        return None
    dt = emitted_at
    if isinstance(dt, str):
        try:
            dt = datetime.datetime.fromisoformat(dt)
        except (TypeError, ValueError):
            return None
    # Normalise awareness so a naive-vs-aware mismatch does not raise and silently defeat the cooldown: if exactly one
    # of the two is tz-aware, give the naive one the other's tzinfo (both are meant to be the same wall clock).
    try:
        now_aware = getattr(now, "tzinfo", None) is not None
        dt_aware = getattr(dt, "tzinfo", None) is not None
        if now_aware and not dt_aware:
            dt = dt.replace(tzinfo=now.tzinfo)
        elif dt_aware and not now_aware:
            now = now.replace(tzinfo=dt.tzinfo)
        return int((now - dt).total_seconds())
    except (TypeError, ValueError, AttributeError):
        return None

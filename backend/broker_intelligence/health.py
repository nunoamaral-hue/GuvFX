"""INGESTION_INCIDENT_R1 (§5) — DARK mailbox-level ingestion health read-model.

The 2026-10-14 outage was invisible at the service level: the systemd unit exited 0 every pass while the only
mailbox hard-failed. This module makes a failing mailbox identifiable WITHOUT decrypting a credential or calling
Gmail — purely from data the worker already records (``last_successful_sync``, ``status``, ``cursor_state``) plus the
secret-free diagnostic fields (R1 §4).

READ-ONLY + secret-free: it reads ConnectedMailbox metadata only, emits a masked email (never the full address), a
stable diagnostic code (never an exception message / URL / token). No Gmail call, no credential read, no write.

UNMIGRATED-SAFE (the whole point — the diagnostic must work in the exact incident state it reveals): before the
migration 0011 columns exist, Django's default ``SELECT *`` over ConnectedMailbox would list the not-yet-created
columns and raise ``ProgrammingError``. So ``service_health`` FIRST introspects whether the R1 columns exist; when
they do not it selects ONLY the pre-existing columns (``.only(...)``) and tells ``mailbox_health`` not to read the R1
attributes (which would trigger a deferred-column load and the same error). Staleness alone — from the long-standing
``last_successful_sync`` — then still flags a mailbox that has not synced for hours, so an operator can identify a dead
mailbox WITHOUT decrypting a credential and WITHOUT the migration applied. (The ingestion WORKER, by contrast, writes
these columns and so requires the migration; see the deployment note in docs/WITHDRAWAL_V1_INGESTION_INCIDENT_R1.md.)

A SERVICE-level success (the unit exited 0) is deliberately NOT one of the mailbox states — the whole point is that
it must not imply mailbox health. The caller reports that separately (see ``service_health``)."""
from __future__ import annotations

import os
from typing import Optional

from .ingest_diagnostics import BLOCKING_CODES, Code

# Mailbox health states (§5).
SERVICE_RUNNING = "SERVICE_RUNNING"                 # service/unit ran (reported at the service level, not per mailbox)
MAILBOX_SYNC_SUCCESSFUL = "MAILBOX_SYNC_SUCCESSFUL"
MAILBOX_SYNC_FAILED = "MAILBOX_SYNC_FAILED"
MAILBOX_STALE = "MAILBOX_STALE"
RECOVERY_REQUIRED = "RECOVERY_REQUIRED"

# Defaults: the timer cadence is 5 min, so 3 missed passes (15 min) with no success = STALE; an hour = RECOVERY.
_DEFAULT_STALE_SECONDS = 15 * 60
_DEFAULT_RECOVERY_SECONDS = 60 * 60


def _conf_int(name: str, default: int) -> int:
    from django.conf import settings
    v = getattr(settings, name, None)
    if v is None:
        v = os.getenv(name, "")
    try:
        return int(v) if str(v).strip() else default
    except (TypeError, ValueError):
        return default


def _stale_seconds() -> int:
    return _conf_int("BROKER_INTELLIGENCE_STALE_SECONDS", _DEFAULT_STALE_SECONDS)


def _recovery_seconds() -> int:
    return _conf_int("BROKER_INTELLIGENCE_RECOVERY_SECONDS", _DEFAULT_RECOVERY_SECONDS)


def _iso(dt) -> Optional[str]:
    return dt.isoformat() if dt else None


# Pre-existing ConnectedMailbox columns the health read-model needs — all present since well before migration 0011.
_STABLE_ONLY = ("id", "provider", "status", "primary_email", "cursor_state", "last_successful_sync")
# The customer status view additionally needs the provider-stable id (for the verified-binding canonical match). Still
# ALL pre-existing 0010 columns — never an R1 column — so it is a safe projection on the unmigrated production schema.
STATUS_STABLE_ONLY = _STABLE_ONLY + ("provider_mailbox_id",)
# One R1 column whose presence proves migration 0011 is applied.
_R1_PROBE_COLUMN = "consecutive_failures"


def r1_columns_present() -> bool:
    """True iff the migration-0011 R1 health columns exist IN THE DATABASE (not merely in the model). When they do
    not (unmigrated prod), a default ``SELECT *`` over ConnectedMailbox would reference a non-existent column and
    raise, so the read-model must avoid selecting/accessing them. Introspection failure is treated as 'present' so a
    normally-migrated environment is never degraded by a transient introspection hiccup."""
    from django.db import connection
    from .models import ConnectedMailbox
    try:
        with connection.cursor() as cur:
            cols = {d.name for d in connection.introspection.get_table_description(
                cur, ConnectedMailbox._meta.db_table)}
        return _R1_PROBE_COLUMN in cols
    except Exception:  # noqa: BLE001 — never let an introspection problem crash the read-model
        return True


def mailbox_health(mailbox, *, now=None, stale_seconds: Optional[int] = None,
                   recovery_seconds: Optional[int] = None, include_error_fields: bool = True) -> dict:
    """Classify ONE mailbox's health from its stored metadata. Pure w.r.t. the DB (reads only the passed instance);
    no Gmail/credential access. Secret-free output.

    ``include_error_fields`` MUST be False when the mailbox was loaded WITHOUT the R1 columns (unmigrated DB, selected
    via ``.only(...)``). In that mode the function reads ONLY pre-existing attributes and never touches the R1 ones —
    accessing a deferred/absent column would trigger a load of a column that does not exist. Staleness (from
    ``last_successful_sync``) still classifies the mailbox, so a long-dead mailbox is still flagged."""
    from django.utils import timezone
    now = now or timezone.now()
    stale_seconds = _stale_seconds() if stale_seconds is None else stale_seconds
    recovery_seconds = _recovery_seconds() if recovery_seconds is None else recovery_seconds

    status = getattr(mailbox, "status", "") or ""
    last_sync = getattr(mailbox, "last_successful_sync", None)
    cursor_present = bool(getattr(mailbox, "cursor_state", "") or "")
    if include_error_fields:
        last_error_code = getattr(mailbox, "last_error_code", "") or ""
        consecutive = int(getattr(mailbox, "consecutive_failures", 0) or 0)
        last_attempted = getattr(mailbox, "last_attempted_sync", None)
        last_error_http_status = getattr(mailbox, "last_error_http_status", None)
        last_error_at = getattr(mailbox, "last_error_at", None)
        last_pass_cursor_advanced = getattr(mailbox, "last_pass_cursor_advanced", None)
    else:   # unmigrated DB (R1 columns absent / deferred): never read them — classify from staleness alone.
        last_error_code = ""
        consecutive = 0
        last_attempted = last_error_http_status = last_error_at = last_pass_cursor_advanced = None

    elapsed = None
    if last_sync is not None:
        try:
            elapsed = (now - last_sync).total_seconds()
        except (TypeError, ValueError):
            elapsed = None

    # Precedence: hardest condition wins.
    revoked = status in ("REVOKED", "ERROR")
    blocking_err = last_error_code in BLOCKING_CODES
    too_stale = elapsed is not None and elapsed > recovery_seconds
    never_synced = last_sync is None
    if revoked or blocking_err or too_stale:
        state = RECOVERY_REQUIRED
    elif never_synced:
        # No success ever recorded. If the worker has run and failed, that's a recovery concern; otherwise just not
        # yet proven healthy — reported as STALE (never SUCCESSFUL on zero evidence of success).
        state = RECOVERY_REQUIRED if (last_error_code or consecutive) else MAILBOX_STALE
    elif elapsed is not None and elapsed > stale_seconds:
        state = MAILBOX_STALE
    elif consecutive > 0 or last_error_code:
        state = MAILBOX_SYNC_FAILED
    else:
        state = MAILBOX_SYNC_SUCCESSFUL

    masked = mailbox.masked_email() if hasattr(mailbox, "masked_email") else "***"
    return {
        "mailbox_id": getattr(mailbox, "id", None),
        "provider": getattr(mailbox, "provider", ""),
        "status": status,
        "masked_email": masked,
        "state": state,
        "healthy": state == MAILBOX_SYNC_SUCCESSFUL,
        "degraded": state != MAILBOX_SYNC_SUCCESSFUL,
        "recovery_required": state == RECOVERY_REQUIRED,
        "elapsed_since_sync_seconds": None if elapsed is None else int(elapsed),
        "last_successful_sync": _iso(last_sync),
        "last_attempted_sync": _iso(last_attempted),
        "last_error_code": last_error_code,
        "last_error_http_status": last_error_http_status,
        "last_error_at": _iso(last_error_at),
        "consecutive_failures": consecutive,
        "cursor_present": cursor_present,
        "last_pass_cursor_advanced": last_pass_cursor_advanced,
        "stale_seconds": stale_seconds,
        "recovery_seconds": recovery_seconds,
    }


# Customer-facing conceptual mailbox states (§6). Mapped from the operator health model + the mailbox status — NO new
# DB enum. HEALTHY requires ACTUAL last-success evidence (never merely status==CONNECTED).
CUSTOMER_NOT_CONNECTED = "NOT_CONNECTED"
CUSTOMER_CONNECTED = "CONNECTED"
CUSTOMER_HEALTHY = "HEALTHY"
CUSTOMER_DEGRADED = "DEGRADED"
CUSTOMER_RECONNECT_REQUIRED = "RECONNECT_REQUIRED"
CUSTOMER_REVOKED = "REVOKED"

# Failure codes whose recovery is a Google RE-AUTHORIZATION by the mailbox owner (Recovery.SPONSOR_REAUTH) — the ONLY
# codes that map to the customer-facing RECONNECT_REQUIRED, because reconnecting Gmail is the action that actually
# fixes them. Deliberately a STRICT SUBSET of BLOCKING_CODES: other blocking codes are NOT the member's to fix by
# reconnecting and must never trigger a false "re-authorize your Gmail" —
#   * GMAIL_HISTORY_CURSOR_INVALID → an operator cursor escalation (ESCALATE_CURSOR), not a consent problem;
#   * OAUTH_CLIENT_NOT_CONFIGURED / CREDENTIAL_MISSING / CREDENTIAL_STORE_ERROR → server-side configuration / store
#     faults (OPERATOR_REVIEW) a member cannot resolve by re-consenting.
# Those surface as DEGRADED (something is wrong, we are looking at it) rather than an incorrect reconnect instruction.
REAUTH_CODES = frozenset({Code.OAUTH_PERMISSION_DENIED, Code.GMAIL_HTTP_401, Code.GMAIL_HTTP_403})


def customer_mailbox_status(mailbox, *, now=None, has_verified_bindings: bool = False,
                            include_error_fields=None) -> dict:
    """A CUSTOMER-SAFE status for ONE mailbox: a conceptual state (mapped from the operator health model + actual
    last-success evidence) plus the minimal fields a member UI needs. It NEVER includes a token, credential ref, raw
    cursor, email body, or operator diagnostic code. Owner-scoping is the caller's responsibility.

    RECONNECT_REQUIRED (§4) is reported ONLY on EXPLICIT Google-authorization evidence — the mailbox ``status`` is
    ERROR, or the last recorded failure is a re-auth code (``REAUTH_CODES``). A mailbox that is merely STALE (a
    late/slow ingestion pass) is DEGRADED, never RECONNECT_REQUIRED: a member must not be told to re-authorize Gmail
    because a pass ran late. Unmigrated-safe: on schema 0010 the detailed failure code is unavailable
    (``include_error_fields`` resolves to False), so a non-ERROR mailbox can reach at most DEGRADED — an OAuth-specific
    diagnosis is never invented from staleness alone. ``include_error_fields`` defaults to auto-detecting whether the
    migration-0011 columns exist; the view passes the same value it used to build its ``.only(...)`` projection so an
    absent/deferred R1 column is never read."""
    if include_error_fields is None:
        include_error_fields = r1_columns_present()
    h = mailbox_health(mailbox, now=now, include_error_fields=include_error_fields)
    raw_status = getattr(mailbox, "status", "") or ""
    err_code = h["last_error_code"]          # "" when include_error_fields is False (schema 0010 / deferred column)
    # EXPLICIT re-auth evidence only — status ERROR (an operator/worker marked it so) or a recorded re-auth code.
    needs_reauth = (raw_status == "ERROR") or (err_code in REAUTH_CODES)
    if raw_status == "REVOKED":
        state = CUSTOMER_REVOKED
    elif needs_reauth:
        state = CUSTOMER_RECONNECT_REQUIRED
    elif h["healthy"]:
        state = CUSTOMER_HEALTHY
    elif h["last_successful_sync"] is None and not (err_code or h["consecutive_failures"]):
        # Connected, nothing proven yet, no failure recorded — NOT 'healthy' on zero evidence, but not degraded.
        state = CUSTOMER_CONNECTED
    else:
        # Stale, or failing WITHOUT re-auth evidence: something is wrong but reconnecting Gmail is not the known fix.
        state = CUSTOMER_DEGRADED
    return {
        "mailbox_id": h["mailbox_id"],
        "provider": h["provider"],
        "masked_email": h["masked_email"],
        "state": state,
        "connection_status": raw_status,
        "last_successful_sync": h["last_successful_sync"],
        "reconnect_required": state in (CUSTOMER_RECONNECT_REQUIRED, CUSTOMER_REVOKED),
        "has_verified_broker_bindings": bool(has_verified_bindings),
    }


def service_health(*, now=None, include_all_statuses: bool = False) -> dict:
    """Aggregate health across connected mailboxes (read-only). The ``service_running_note`` is explicit that a
    successful systemd unit exit does NOT imply mailbox health — the mailbox states are authoritative."""
    from django.utils import timezone
    from .models import ConnectedMailbox
    now = now or timezone.now()
    r1 = r1_columns_present()
    qs = ConnectedMailbox.objects.all().order_by("id")
    if not include_all_statuses:
        qs = qs.filter(status=ConnectedMailbox.Status.CONNECTED)
    if not r1:
        # Unmigrated DB: select ONLY pre-existing columns so the query never references a not-yet-created column, and
        # tell mailbox_health not to read the R1 attributes (a deferred-column load would hit the same missing column).
        qs = qs.only(*_STABLE_ONLY)
    rows = [mailbox_health(mb, now=now, include_error_fields=r1) for mb in qs]
    return {
        "generated_at": now.isoformat(),
        "service_running_note": ("A successful systemd unit exit does NOT imply any mailbox succeeded; the per-mailbox "
                                 "state below is authoritative."),
        "r1_columns_present": r1,
        "stale_seconds": _stale_seconds(),
        "recovery_seconds": _recovery_seconds(),
        "count": len(rows),
        "any_degraded": any(r["degraded"] for r in rows),
        "any_recovery_required": any(r["recovery_required"] for r in rows),
        "mailboxes": rows,
    }

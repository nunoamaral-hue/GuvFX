"""hosted_workspace.corroboration_diagnosis — WHY a Hosted Workspace has not reached CONNECTED (DARK, read-only).

Operations can see a workspace stuck at ``AUTHORIZED`` / ``WAITING_FOR_LOGIN`` with ``proj_connected=false`` but
has had NO way to see WHICH corroboration gate is failing. This module closes that gap with a PURE, READ-ONLY,
SECRET-FREE diagnosis. It is observability ONLY:

  * it NEVER contacts the host (it classifies a result the caller already has, or reads persisted fields);
  * it NEVER mutates workspace state (no save, no field write) and NEVER feeds the trust/observer decision;
  * it emits NO credential, NO full login, NO Windows username, NO runtime/terminal path — only stable
    lower_snake reason codes, booleans, enum codes and coarse ages.

Two complementary entry points, both pure:

  1. ``classify_corroboration_result(result, *, workspace, now)`` — a FAITHFUL classifier that re-walks the
     EXACT gate order of ``live_observe.corroboration_matches`` + ``live_observe.build_observation_from_host``
     over a raw ``OBSERVE_WORKSPACE`` result (tenant snapshot + LocalSystem corroboration), returning the FIRST
     failed gate's reason code. It imports the real pure gate primitives from ``live_observe`` / ``producer`` so
     it can never silently drift from the gates it explains.
  2. ``diagnose_workspace(workspace, *, now)`` — the OPERATIONAL diagnosis derived purely from the DURABLE
     persisted fields (``observation_version``, ``last_decision_at``, ``proj_*``, ``delivery_state``,
     ``canonical_*``). It makes no host call, so it can only report what persisted state can PROVE: never
     observed (``no_runtime_populated`` — e.g. the Account-49 incident), a lapsed heartbeat (``stale_heartbeat``),
     an unresolvable environment (``environment_unresolved``), or the connected/ipc/identity facts the last
     accepted observation left in the projection. A FAILED corroboration persists nothing, so a structural gate
     failure (wrong owner / runtime-root / process / observer-task) surfaces as "never populated / stale" from
     persisted state alone — those exact codes are produced (and tested) by the classifier above, which is where
     a live or sampled result is available.

NB on field provenance (verified against ``persistence.py``): ``last_observed_at`` and ``currently_attached_*``
are VESTIGIAL — no production writer advances them. The authoritative "an observation has been accepted" signal
is ``observation_version`` (``0`` ⇒ never) and the freshness key is ``last_decision_at`` (stamped atomically with
``proj_*`` under the row lock). This module keys on those and treats the legacy fields as best-effort only.
"""
from __future__ import annotations

import logging
import re

# The REAL pure gate primitives — imported so this diagnosis cannot drift from the gates it explains. None of
# these touch the host; they are string/number/path predicates + the server-derived identity derivation.
from hosted_workspace.live_observe import (
    _OBSERVATION_FRESHNESS_LIMIT_SECONDS,
    _int_or_none,
    _is_true,
    _network_active,
    _norm_path,
    _num_or_none,
    _server_derived_identity,
    _str_or_none,
)
from hosted_workspace.matching import (
    ExpectedAccount,
    WorkspaceObservation as MatchObservation,
    evaluate_active_account_match,
)
from hosted_workspace.producer import DEFAULT_CLOCK_TOLERANCE_SECONDS

logger = logging.getLogger("guvfx.hosted_workspace")

# ── Reason codes (stable, secret-free, lower_snake). The TEN required operational codes … ────────────────────
NO_RUNTIME_POPULATED = "no_runtime_populated"            # no corroborated runtime for this slot (never observed)
NO_SLOT_PROCESS_OR_SESSION = "no_slot_process_or_session"  # process absent / session not interactive (>0)
WRONG_SESSION_OWNER = "wrong_session_owner"              # terminal owned by != expected guvfx_u_<id>
RUNTIME_ROOT_MISMATCH = "runtime_root_mismatch"          # terminal not under the expected runtime root
IPC_NOT_READY = "ipc_not_ready"                          # attach did not succeed / MT5 IPC unavailable
IDENTITY_LOGIN_MISMATCH = "identity_login_mismatch"      # observed login != expected account_number
IDENTITY_SERVER_MISMATCH = "identity_server_mismatch"    # observed server != expected broker server
STALE_HEARTBEAT = "stale_heartbeat"                      # freshness anchor missing / lapsed / replayed
ENVIRONMENT_UNRESOLVED = "environment_unresolved"        # demo/live cannot be cleanly resolved (fail closed)
OBSERVER_TASK_NOT_SCHEDULED = "observer_task_not_scheduled"  # host OBSERVE ok:false / reason no_observer_task

# … plus benign / informational terminals (NOT faults — honest outcomes that are not corroboration failures).
CORROBORATED = "corroborated"                            # every gate passes: connected + matched + fresh
BROKER_NOT_CONNECTED = "broker_not_connected"            # structural gates pass; terminal not yet connected
IDENTITY_NOT_OBSERVABLE = "identity_not_observable"      # connected + IPC up, but the active account id is not yet readable
CONNECTION_NOT_CORROBORATED = "connection_not_corroborated"  # tenant claimed connected; LocalSystem saw no public endpoint
EXPECTED_IDENTITY_UNCONFIGURED = "expected_identity_unconfigured"  # customer has not declared the broker identity yet
OBSERVE_UNAVAILABLE = "observe_unavailable"              # host OBSERVE ok:false for another (ambiguous) reason
NO_ACCOUNT = "no_account"                                # workspace has no bound trading account
TRUST_GATE_OFF = "trust_gate_off"                        # isolation cert + supervised carve-out both off
OBSERVATION_DISABLED = "observation_disabled"            # HOSTED_MT5_OBSERVATION_ENABLED off
NOT_OBSERVABLE_STATE = "not_observable_state"            # canonical state is not one an observation can drive

# Codes that represent an actual stuck/failed corroboration (used for fleet health + "fault" counting). The
# benign terminals above are deliberately excluded so expected darkness is never counted as a fault.
FAULT_CODES = frozenset({
    NO_RUNTIME_POPULATED, NO_SLOT_PROCESS_OR_SESSION, WRONG_SESSION_OWNER, RUNTIME_ROOT_MISMATCH,
    IPC_NOT_READY, IDENTITY_LOGIN_MISMATCH, IDENTITY_SERVER_MISMATCH, STALE_HEARTBEAT,
    ENVIRONMENT_UNRESOLVED, OBSERVER_TASK_NOT_SCHEDULED,
})

# A human-readable, secret-free note per code (for the operator surface). Never interpolates any value.
REASON_NOTES = {
    NO_RUNTIME_POPULATED: "No corroborated slot runtime has ever been observed for this workspace.",
    NO_SLOT_PROCESS_OR_SESSION: "No expected terminal process in an interactive session was corroborated.",
    WRONG_SESSION_OWNER: "The corroborated terminal is not owned by the expected slot identity.",
    RUNTIME_ROOT_MISMATCH: "The corroborated terminal is not under the expected slot runtime root.",
    IPC_NOT_READY: "The guarded attach did not succeed / MT5 IPC was not available.",
    IDENTITY_LOGIN_MISMATCH: "The observed active login does not match the bound account.",
    IDENTITY_SERVER_MISMATCH: "The observed active broker server does not match the bound account.",
    STALE_HEARTBEAT: "The last corroboration is missing/stale — freshness has lapsed.",
    ENVIRONMENT_UNRESOLVED: "The account demo/live environment cannot be cleanly resolved (fail closed).",
    OBSERVER_TASK_NOT_SCHEDULED: "The host-side observer task is not scheduled / returned not-ok.",
    CORROBORATED: "Fully corroborated: connected, account matched, fresh.",
    BROKER_NOT_CONNECTED: "Slot corroborated but the broker terminal is not yet connected.",
    IDENTITY_NOT_OBSERVABLE: "Terminal connected but the active account identity is not yet readable.",
    CONNECTION_NOT_CORROBORATED: "A tenant 'connected' claim was not backed by an observed external connection.",
    EXPECTED_IDENTITY_UNCONFIGURED: "The customer has not yet declared the expected broker identity.",
    OBSERVE_UNAVAILABLE: "The host observe call returned not-ok (ambiguous).",
    NO_ACCOUNT: "The workspace has no bound trading account.",
    TRUST_GATE_OFF: "RemoteApp isolation is not certified and no supervised carve-out applies.",
    OBSERVATION_DISABLED: "Hosted MT5 observation is disabled.",
    NOT_OBSERVABLE_STATE: "The canonical state is not one an observation can drive.",
}

# Operational staleness threshold for the STATE-DERIVED diagnosis. DISTINCT from the 60s anti-replay freshness
# window the classifier applies to a live corroboration's ``collected_at``: here we ask "has an accepted
# observation landed RECENTLY" against the durable ``last_decision_at``, which the observation cron advances on
# each accepted cycle. Generous (a small multiple of the cron period) so a single missed cycle is not "stale".
STATE_STALE_AFTER_SECONDS = 600.0


# Host-provided reason strings are the executor's own fixed lower_snake codes; echo one ONLY when it matches
# that shape AND contains a letter (so a bare login-shaped number can never slip through) — defence-in-depth.
_SAFE_CODE_RE = re.compile(r"^(?=.*[a-z])[a-z0-9_]{1,64}$")
# A display sanitiser for the model's own secret-free code fields (enum reasons are UPPER_SNAKE). It preserves
# a stable code shape and blanks anything carrying whitespace / path separators / @ etc.
_SAFE_DISPLAY_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")


def _safe_code(value) -> str:
    s = _str_or_none(value) or ""
    return s if _SAFE_CODE_RE.match(s) else ""


def _safe_display_code(value) -> str:
    s = (_str_or_none(value) or "").strip()
    return s if _SAFE_DISPLAY_RE.match(s) else ""


def _account_of(workspace):
    return getattr(workspace, "trading_account", None)


def _account_id_of(workspace):
    acct = _account_of(workspace)
    aid = getattr(acct, "id", None)
    try:
        return acct, (int(aid) if aid is not None else None)
    except (TypeError, ValueError):
        return acct, None


def _environment_resolves(acct) -> bool:
    """True iff the account's DEMO/LIVE environment resolves cleanly (fail-closed on any integrity error). Mirrors
    the fail-closed policy the matcher relies on; an unresolvable environment is ``environment_unresolved``."""
    try:
        from trading.account_policy import account_environment
        account_environment(acct)
        return True
    except Exception:  # noqa: BLE001 — any resolution failure is "unresolved" (fail closed)
        return False


def _expected_account(acct) -> ExpectedAccount:
    """The bound identity to compare the observed active account against — constructed to EXACTLY mirror the
    production producer (``live_observe.build_observation_from_host`` + ``producer._account_match``), so the
    classifier can never disagree with the real matcher in either flag state:

      * default (``HOSTED_LIVE_MONITORING_ENABLED`` OFF) ⇒ DEMO-ONLY for every account
        (``is_demo=True, allow_live=False``) — a LIVE / env-inconsistent account is NOT matched, exactly as the
        producer's ``expected_is_demo=None`` demo default;
      * flag ON AND a CLEAN LIVE account ⇒ ``is_demo=False, allow_live=True`` (LIVE identity matching enabled).

    Reads the SAME flag + policy the producer reads; never a raw ``is_demo``."""
    from hosted_workspace.flags import hosted_live_monitoring_enabled
    from trading.account_policy import is_live_environment
    exp_is_demo, exp_allow_live = None, False
    if hosted_live_monitoring_enabled() and is_live_environment(acct):
        exp_is_demo, exp_allow_live = False, True
    server = getattr(acct, "broker_server", None)
    return ExpectedAccount(
        login=_str_or_none(getattr(acct, "account_number", None)),
        server=_str_or_none(getattr(server, "server_name", None)),
        is_demo=(True if exp_is_demo is None else bool(exp_is_demo)),
        allow_live=bool(exp_allow_live),
    )


def _within_freshness(collected_at, now) -> bool:
    """The anti-replay freshness window the certified producer applies: fresh iff ``-tol <= (now - collected) <=
    limit`` (60s limit / 5s clock tolerance). A missing/non-numeric anchor is NOT fresh (handled by the caller)."""
    age = now - collected_at
    return (-DEFAULT_CLOCK_TOLERANCE_SECONDS) <= age <= _OBSERVATION_FRESHNESS_LIMIT_SECONDS


def classify_corroboration_result(result, *, workspace, now):
    """Faithful classifier: re-walk the EXACT gate order of ``corroboration_matches`` +
    ``build_observation_from_host`` over a raw ``OBSERVE_WORKSPACE`` ``result`` and return
    ``(reason_code, evidence)`` for the FIRST failed gate (or ``CORROBORATED`` when all pass).

    PURE: ``result`` is a dict the caller already obtained; this NEVER calls ``executor.observe()`` and NEVER
    mutates anything. ``evidence`` is a secret-free dict of booleans/ages only — it NEVER echoes the corroborated
    owner_user, runtime_root, login, server or any path. ``workspace`` supplies the server-derived expected
    identity (never a value from the wire). ``now`` is a POSIX timestamp (float)."""
    # Gate 0 — the host observe call must be present and ``ok``. ``no_observer_task`` is the specific
    # "the observer task is not scheduled" case; any other ok:false is ambiguous (host/transport).
    if not isinstance(result, dict) or not result.get("ok"):
        reason = _str_or_none((result or {}).get("reason")) if isinstance(result, dict) else None
        if reason == "no_observer_task":
            return OBSERVER_TASK_NOT_SCHEDULED, {"observe_ok": False, "observe_reason": "no_observer_task"}
        return OBSERVE_UNAVAILABLE, {"observe_ok": False, "observe_reason": _safe_code(reason)}

    acct, acct_id = _account_id_of(workspace)
    if acct is None or acct_id is None:
        return NO_ACCOUNT, {}

    # Expected identity is SERVER-DERIVED (never from the wire) — this walk mirrors ``corroboration_matches``
    # gate order exactly: corr-dict (a) → expected-identity-derivable (b) → account_id (c) → process (d) → … .
    expected_user, expected_runtime = _server_derived_identity(acct_id)

    corr = result.get("corroboration")
    # Gate a — the LocalSystem corroboration block must exist (else the slot runtime was not corroborated).
    if not isinstance(corr, dict):
        return NO_RUNTIME_POPULATED, {"corroboration_present": False}
    # Gate b — the server-derived expected identity must itself be derivable (defensive; unreachable for a
    # real positive account id). An underivable expected identity is an environment/identity resolution fault.
    if expected_user is None:
        return ENVIRONMENT_UNRESOLVED, {"expected_identity_derivable": False}
    # Gate c — the corroboration block must be for THIS slot.
    if _int_or_none(corr.get("account_id")) != acct_id:
        return NO_RUNTIME_POPULATED, {"corroboration_present": True, "account_id_matches": False}
    # Gate d — the expected terminal process must be present.
    if not _is_true(corr.get("process_present")):
        return NO_SLOT_PROCESS_OR_SESSION, {"process_present": False}
    # Gate e — owned by the expected slot identity (compared, never echoed).
    if _str_or_none(corr.get("owner_user")) != expected_user:
        return WRONG_SESSION_OWNER, {"owner_matches": False}
    # Gate f — an interactive session (> 0).
    session_id = _int_or_none(corr.get("session_id"))
    if session_id is None or session_id <= 0:
        return NO_SLOT_PROCESS_OR_SESSION, {"process_present": True, "interactive_session": False}
    # Gate g — under the expected runtime root (normalised compare, never echoed).
    if _norm_path(corr.get("runtime_root")) != expected_runtime:
        return RUNTIME_ROOT_MISMATCH, {"runtime_root_matches": False}
    # Gate h — the freshness ANCHOR must exist (``corroboration_matches`` requires ``collected_at`` to be a
    # usable number; the anti-replay WINDOW is applied just below, mirroring the producer order where it is the
    # parallel ``fresh`` fact computed after the connection agreement, NOT an early corroboration_matches gate).
    collected_at = _num_or_none(corr.get("collected_at"))
    if collected_at is None:
        return STALE_HEARTBEAT, {"collected_at_present": False}

    # Structural corroboration passed. Now the tenant-attested facts that decide CONNECTED + matched.
    # Connected-claim agreement: a tenant ``terminal_connected`` is honoured ONLY with an observed public
    # endpoint (``build_observation_from_host`` returns None otherwise — a forged connected is refused).
    tenant_connected = _is_true(result.get("terminal_connected"))
    if tenant_connected and not _network_active(corr):
        return CONNECTION_NOT_CORROBORATED, {"terminal_connected_claimed": True, "external_connection": False}
    if not tenant_connected:
        return BROKER_NOT_CONNECTED, {"terminal_connected": False}
    # Freshness WINDOW — a connected-but-stale/replayed corroboration cannot drive advancement (producer ``fresh``).
    if not _within_freshness(collected_at, now):
        return STALE_HEARTBEAT, {"collected_at_present": True, "fresh": False,
                                 "age_seconds": round(now - collected_at, 1)}
    # IPC usable only when the guarded attach SUCCEEDED and IPC is reported (producer semantics).
    if not (_is_true(result.get("attach_succeeded")) and _is_true(result.get("ipc_available"))):
        return IPC_NOT_READY, {"attach_succeeded": _is_true(result.get("attach_succeeded")),
                               "ipc_available": _is_true(result.get("ipc_available"))}
    # Identity — reuse the certified matcher so login/server/classification decisions can never drift. Build a
    # readiness-neutral observation (process/ipc/connected/trade_allowed True) so ONLY identity decides, exactly
    # like ``producer._account_match``.
    observed = MatchObservation(
        process_running=True, ipc_available=True, connected=True, trade_allowed=True,
        login=_str_or_none(result.get("observed_login")), server=_str_or_none(result.get("observed_server")),
        trade_mode=_int_or_none(result.get("observed_trade_mode")))
    decision = evaluate_active_account_match(observed, _expected_account(acct))
    if not decision.ok:
        mapped = _IDENTITY_REASON_MAP.get(decision.reason)
        if mapped is not None:
            return mapped, {"account_match": False}
        # Remaining matcher reasons (login/server/trade_mode unavailable) mean the active account id is not yet
        # readable — the terminal IS connected + IPC up (both gates passed above), so this is NOT "not connected".
        return IDENTITY_NOT_OBSERVABLE, {"account_match": False, "identity_observable": False}
    return CORROBORATED, {"account_match": True}


# Matcher reason → operational code. Login/server mismatches map to the two required identity codes; a demo/live
# classification disagreement (or an un-authorised live match) is an environment problem; an unconfigured expected
# identity is the customer not having declared the broker account yet.
_IDENTITY_REASON_MAP = {
    "active_account_login_mismatch": IDENTITY_LOGIN_MISMATCH,
    "active_account_server_mismatch": IDENTITY_SERVER_MISMATCH,
    "classification_mismatch": ENVIRONMENT_UNRESOLVED,
    "live_execution_not_authorised": ENVIRONMENT_UNRESOLVED,
    "expected_login_unconfigured": EXPECTED_IDENTITY_UNCONFIGURED,
    "expected_server_unconfigured": EXPECTED_IDENTITY_UNCONFIGURED,
}


def _age_seconds(dt, now_dt):
    if dt is None or now_dt is None:
        return None
    try:
        return round((now_dt - dt).total_seconds(), 1)
    except Exception:  # noqa: BLE001
        return None


def diagnose_workspace(workspace, *, now=None) -> dict:
    """READ-ONLY operational diagnosis of ``workspace`` derived PURELY from durable persisted fields — no host
    contact, no mutation, no credential/login/path. Returns a secret-free dict:

        {reason, note, account_id, owner_id, canonical_state, canonical_reason, delivery_state, delivery_reason,
         observed (bool: an accepted observation has ever landed), observation_version, last_decision_age_seconds,
         proj_connected / proj_account_match / proj_process_running / proj_ipc_available / proj_trade_allowed,
         is_fault (bool)}

    Only reports what persisted state can PROVE: never observed ⇒ ``no_runtime_populated`` (the Account-49 case),
    accepted-but-lapsed ⇒ ``stale_heartbeat``, unresolvable env ⇒ ``environment_unresolved``, else the
    projection's connected/ipc/identity facts. Structural gate failures (wrong owner / runtime root / observer
    task) persist nothing, so from state alone they legitimately read as never-populated/stale — the classifier
    is where a live/sampled result resolves them to the exact structural code."""
    from django.utils import timezone
    now_dt = now or timezone.now()

    acct, acct_id = _account_id_of(workspace)
    owner_id = getattr(acct, "user_id", None) if acct is not None else None
    state = _str_or_none(getattr(workspace, "canonical_state", None)) or ""
    # ``canonical_reason`` is enum-bounded and ``delivery_reason`` is a free CharField documented secret-free;
    # both are passed through the display sanitiser (belt-and-braces) so only a stable code shape is ever echoed.
    reason_field = _safe_display_code(getattr(workspace, "canonical_reason", None))
    delivery_state = _str_or_none(getattr(workspace, "delivery_state", None)) or ""
    delivery_reason = _safe_display_code(getattr(workspace, "delivery_reason", None))
    obs_version = int(getattr(workspace, "observation_version", 0) or 0)
    last_decision_at = getattr(workspace, "last_decision_at", None)
    last_decision_age = _age_seconds(last_decision_at, now_dt)
    proj_connected = getattr(workspace, "proj_connected", None)
    proj_match = getattr(workspace, "proj_account_match", None)
    proj_proc = getattr(workspace, "proj_process_running", None)
    proj_ipc = getattr(workspace, "proj_ipc_available", None)
    proj_trade = getattr(workspace, "proj_trade_allowed", None)

    if acct is None or acct_id is None:
        reason = NO_ACCOUNT
    elif not _environment_resolves(acct):
        reason = ENVIRONMENT_UNRESOLVED
    elif obs_version <= 0:
        # The single authoritative writer has never applied an observation to this slot ⇒ nothing corroborated.
        reason = NO_RUNTIME_POPULATED
    elif last_decision_at is None or (last_decision_age is not None and last_decision_age > STATE_STALE_AFTER_SECONDS):
        reason = STALE_HEARTBEAT
    elif proj_proc is not True:
        reason = NO_SLOT_PROCESS_OR_SESSION
    elif proj_ipc is not True:
        reason = IPC_NOT_READY
    elif proj_connected is not True:
        reason = BROKER_NOT_CONNECTED
    elif proj_match is not True:
        reason = _state_identity_reason(workspace, acct)
    else:
        reason = CORROBORATED

    return {
        "reason": reason,
        "note": REASON_NOTES.get(reason, ""),
        "account_id": acct_id,
        "owner_id": owner_id,
        "canonical_state": state,
        "canonical_reason": reason_field,
        "delivery_state": delivery_state,
        "delivery_reason": delivery_reason,
        "observed": obs_version > 0,
        "observation_version": obs_version,
        "last_decision_age_seconds": last_decision_age,
        "proj_connected": proj_connected,
        "proj_account_match": proj_match,
        "proj_process_running": proj_proc,
        "proj_ipc_available": proj_ipc,
        "proj_trade_allowed": proj_trade,
        "is_fault": reason in FAULT_CODES,
    }


def _state_identity_reason(workspace, acct) -> str:
    """Connected-but-unmatched: try to distinguish a login vs server mismatch from the (best-effort, often
    empty) legacy ``currently_attached_*`` fields. These have no production writer, so when empty we honestly
    return the generic login-mismatch fault rather than guessing a server mismatch — never leaks a value."""
    attached_login = _str_or_none(getattr(workspace, "currently_attached_login", None))
    attached_server = _str_or_none(getattr(workspace, "currently_attached_server", None))
    expected_login = _str_or_none(getattr(acct, "account_number", None))
    server = getattr(acct, "broker_server", None)
    expected_server = _str_or_none(getattr(server, "server_name", None))
    if attached_server and expected_server and attached_server.strip().casefold() != expected_server.strip().casefold():
        return IDENTITY_SERVER_MISMATCH
    if attached_login and expected_login and str(attached_login) != str(expected_login):
        return IDENTITY_LOGIN_MISMATCH
    # No usable observed identity to attribute the mismatch — default to the login-identity fault (the common
    # "wrong active account" case), never a fabricated server mismatch.
    return IDENTITY_LOGIN_MISMATCH


# ── Fleet health metrics (fail-open; never raises) ───────────────────────────────────────────────────────────
def _health_state_for(stuck_faults: int, total: int, *, scan_truncated: bool = False) -> str:
    """Map the fleet diagnosis to the shared operational-health vocabulary WITHOUT modifying the core rollup.
    No workspaces ⇒ AWAITING_SPONSOR (expected darkness); any fault ⇒ DEGRADED; else HEALTHY — EXCEPT that a
    TRUNCATED scan can never claim HEALTHY (faults may hide in the unscanned tail), mirroring the
    operational-health principle that an unobserved subsystem never reports healthy."""
    from core.operational_health import HealthState
    if total == 0:
        return HealthState.AWAITING_SPONSOR
    if stuck_faults:
        return HealthState.DEGRADED
    return HealthState.DEGRADED if scan_truncated else HealthState.HEALTHY


def _snapshot_age_metrics(account_ids, now_dt):
    """Read-only cross-app read of ``analytics.AccountEquitySnapshot`` (max observed_at per account). Fail-open:
    any error (app/table unavailable) ⇒ ``None`` so the metric never raises into the operator surface."""
    try:
        from django.db.models import Max
        from analytics.models import AccountEquitySnapshot
        ids = [a for a in account_ids if a is not None]
        rows = (AccountEquitySnapshot.objects.filter(trading_account_id__in=ids)
                .values("trading_account_id").annotate(latest=Max("observed_at")))
        latest_by_acct = {r["trading_account_id"]: r["latest"] for r in rows}
        with_snapshot = sum(1 for a in ids if latest_by_acct.get(a) is not None)
        ages = [(_age_seconds(latest_by_acct[a], now_dt)) for a in ids if latest_by_acct.get(a) is not None]
        ages = [x for x in ages if x is not None]
        return {
            "accounts_total": len(ids),
            "accounts_with_snapshot": with_snapshot,
            "accounts_without_snapshot": len(ids) - with_snapshot,
            "max_snapshot_age_seconds": (max(ages) if ages else None),
            "min_snapshot_age_seconds": (min(ages) if ages else None),
        }
    except Exception:  # noqa: BLE001 — cross-app read is best-effort; never raise into the metric
        logger.warning("corroboration_diagnosis: snapshot-age metric unavailable", exc_info=True)
        return None


# Cap on the number of workspaces the reason-distribution scan materialises, so the metric is BOUNDED (never an
# unbounded fleet scan). Headline counts use DB-level aggregates and are exact regardless of this cap.
_METRICS_SCAN_LIMIT = 500


def observation_health_metrics(*, now=None) -> dict:
    """Fail-open, BOUNDED fleet health metrics for hosted-workspace observation/delivery. MUTATES NOTHING and
    NEVER RAISES (mirrors the ``core.operational_health`` fail-open contract without touching the core rollup).

    Headline counts (``workspace_total`` / ``never_observed`` / ``stuck_authorized`` / ``stuck_none`` /
    ``proj_connected_false``) come from DB-level ``count()`` aggregates — exact and bounded, no row
    materialisation, no N+1. The per-reason-code distribution + observation ages come from a SINGLE
    ``select_related`` query capped at ``_METRICS_SCAN_LIMIT`` (so the pure per-row classification is bounded and
    issues no extra per-row query). Snapshot age is one read-only aggregate over the scanned accounts."""
    from django.db.models import Q
    from django.utils import timezone
    now_dt = now or timezone.now()
    out: dict = {"generated_at": now_dt.isoformat()}
    try:
        from hosted_workspace.models import HostedMt5Workspace

        base = HostedMt5Workspace.objects
        # Exact, bounded headline counts via DB aggregates (no fleet materialisation, no N+1).
        total = base.count()
        never_observed = base.filter(observation_version=0).count()
        proj_connected_false = base.filter(~Q(proj_connected=True)).count()
        stuck_authorized = base.filter(delivery_state="AUTHORIZED").count()
        stuck_none = base.filter(delivery_state="NONE").count()

        # Bounded reason-distribution scan: ONE query, select_related, capped. The per-row diagnosis is pure
        # (reads only already-fetched fields) so this issues no extra per-row query.
        scan_rows = list(base.select_related("trading_account__broker_server").order_by("id")[:_METRICS_SCAN_LIMIT])
        scanned = len(scan_rows)
        stale = 0
        obs_ages = []
        reason_counts: dict = {}
        fault_count = 0
        diagnose_errors = 0
        scanned_account_ids = []
        for ws in scan_rows:
            scanned_account_ids.append(getattr(ws, "trading_account_id", None))
            try:
                d = diagnose_workspace(ws, now=now_dt)
            except Exception:  # noqa: BLE001 — a single bad row never collapses the whole fleet payload
                diagnose_errors += 1
                continue
            reason_counts[d["reason"]] = reason_counts.get(d["reason"], 0) + 1
            if d["is_fault"]:
                fault_count += 1
            if d["reason"] == STALE_HEARTBEAT:
                stale += 1
            if d["last_decision_age_seconds"] is not None:
                obs_ages.append(d["last_decision_age_seconds"])

        out.update({
            "workspace_total": total,
            "never_observed": never_observed,
            "proj_connected_false": proj_connected_false,
            "stuck_authorized": stuck_authorized,
            "stuck_none": stuck_none,
            # Fault count + reason distribution are over the bounded scan; ``scanned`` / ``scan_truncated`` make
            # the boundedness explicit so a reader never mistakes a capped scan for the whole fleet.
            "scanned": scanned,
            "scan_truncated": total > scanned,
            "fault_count": fault_count,
            "diagnose_errors": diagnose_errors,
            "observation_age": {
                "stale_count": stale,
                "stale_after_seconds": STATE_STALE_AFTER_SECONDS,
                "max_age_seconds": (max(obs_ages) if obs_ages else None),
            },
            "reason_code_counts": reason_counts,
            "snapshot_age": _snapshot_age_metrics(scanned_account_ids, now_dt),
            "health": _health_state_for(fault_count, total, scan_truncated=total > scanned),
        })
    except Exception:  # noqa: BLE001 — the whole metric is fail-open; a partial/empty payload beats a raise
        logger.warning("corroboration_diagnosis: observation_health_metrics degraded", exc_info=True)
        out.setdefault("workspace_total", None)
        try:
            from core.operational_health import HealthState
            out["health"] = HealthState.DEGRADED
        except Exception:  # noqa: BLE001
            out["health"] = "DEGRADED"
    return out

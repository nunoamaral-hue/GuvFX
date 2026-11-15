"""hosted_workspace.liveness_recovery — P0 governed zero-terminal liveness recovery (DARK, 2026-09-14).

THE PROBLEM (proven in prod 2026-09-14): a confirmed, execution-authorized (ARMED) hosted AUTO_DEMO
workspace whose ``terminal64`` EXITS — MetaTrader LiveUpdate (acct25, down ~24d from 2026-08-20), a crash, or
a session teardown (acct33, down ~11d from 2026-09-03) — is never automatically relaunched:

  * ``capability_recovery`` only targets a CONNECTED-but-capability-stuck terminal
    (``canonical_state == CONNECTED`` ∧ proj_connected ∧ matched ∧ ``trade_allowed == False``); a fully-down
    workspace is NOT CONNECTED, so it is never a candidate — it structurally cannot recover from ZERO; and
  * no watchdog watches ``terminal64`` liveness (the per-tenant bridge watchdog watches only the bridge).

So the terminal stays down; the observer cannot obtain a fresh canonical decision; ``last_decision_at`` goes
stale past ``WORKSPACE_OBSERVATION_FRESH_SECONDS``; readiness returns ``workspace_observation_stale``;
``hosted_execution_armed`` becomes False; and every PLACE_ORDER is refused pre-dispatch —
silently, for days.

THE FIX (liveness, NOT arming): for an ARMED (``execution_enabled`` ∧ ``execution_authorized_at``),
account-confirmed, previously-matched, demo, non-reserved workspace that a READ-ONLY observe reports has NO
running terminal, relaunch EXACTLY ONE terminal via the certified ``RELAUNCH_TERMINAL`` primitive
(single-instance, LiveUpdate-contained, fail-closed — it recovers from a fully-down state). The observer then
re-proves the SAME broker identity + trade_allowed on the next cycle and the freshness gate re-arms naturally.

SAFETY (the load-bearing invariants):
  * DARK: a no-op unless ``hosted_persistent_mt5_enabled()`` AND ``hosted_liveness_recovery_enabled()``.
  * Customer Zero (1) and account 18 are reserved and excluded (reuse capability_recovery._RESERVED_ACCOUNT_IDS)
    — the signed executor + the .ps1 refuse them again (defence in depth).
  * ONBOARDING-SAFE: only ARMED workspaces (``execution_enabled`` ∧ ``execution_authorized_at`` ∧
    ``workspace_confirmed_at``) are candidates, so a fresh/unconfirmed onboarding tenant is NEVER relaunched
    merely because observation is unavailable.
  * LIVENESS-PROVEN, never inferred: relaunch ONLY when a fresh read-only observe reports
    ``process_running is False``. An unavailable/ambiguous observe → SKIP (fail-closed). A started process is
    NOT inferred as connected — re-arm still requires the observer's fresh exact-match positive.
  * BOUNDED / LOOP-SAFE: each attempt is CLAIMED atomically (stamp ``liveness_recovery_at`` + increment
    ``liveness_recovery_count`` under select_for_update) BEFORE the host call; at most MAX_RECOVERY_ATTEMPTS per
    workspace, each behind RECOVERY_COOLDOWN_S; a persistently-failing relaunch backs off and the OPEN operator
    alert stays raised — MT5 is never restart-looped.
  * Capability untouched: identity/server pins, the observation-freshness gate, #378 isolation, AppLocker,
    LiveUpdate containment, and per-account sizing are all preserved. It NEVER logs in, changes the account,
    ARMS execution, or places an order.

OPERATOR VISIBILITY (Phase 7): a deduped ``reliability.AlertEvent`` (Component MT5_TERMINAL) is OPENed when an
armed workspace is found terminal-less (CRITICAL — "execution paused safely, recovery required") and RESOLVED
with an INFO recovery alert once the observer re-proves it healthy and re-armed. No broker secret is ever put
in an alert (the internal account id only).
"""
from __future__ import annotations

import json
import logging
import os

from django.db import transaction
from django.utils import timezone

from execution.readiness import PersistentWorkspaceProvider, _observation_fresh
from hosted_workspace.capability_recovery import _RESERVED_ACCOUNT_IDS
from hosted_workspace.flags import hosted_liveness_recovery_enabled, hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.slot_preparation import resolve_host_executor
from hosted_workspace.host_agent_dispatch import derive_slot

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.liveness_recovery"

# Bounds (deliberately conservative). At most 3 relaunch cycles per workspace, each behind a 5-minute cooldown,
# so a persistently-unrecoverable slot is relaunched at most 3 times over ~15 min, then the runner stops and the
# OPEN operator alert carries it to a human (fail-safe; never restart-looped).
MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_COOLDOWN_S = 300

_ALERT_PREFIX = "hosted_liveness_terminal_down"


def _alert_dedup_key(account_id) -> str:
    return f"{_ALERT_PREFIX}:account:{account_id}"


def _ok(res) -> bool:
    return bool(res) and bool(res.get("ok"))


def _live_recovery_enabled() -> bool:
    """D4e DARK gate for widening recovery to LIVE (import-local; fail-closed). OFF ⇒ recovery is demo-only,
    byte-identical to pre-D4d. Reads the DEDICATED ``HOSTED_LIVE_RECOVERY_ENABLED`` flag — independent of the D3
    ``HOSTED_LIVE_MONITORING_ENABLED`` gate, so LIVE monitoring ON does NOT imply LIVE infrastructure recovery ON
    (Sponsor D4e). LIVE recovery is an environment-agnostic INFRASTRUCTURE restore, never an execution grant."""
    try:
        from hosted_workspace.flags import hosted_live_recovery_enabled
        return hosted_live_recovery_enabled()
    except Exception:  # noqa: BLE001
        return False


def _is_live_env(account) -> bool:
    """Clean-LIVE classification (import-local; fail-closed)."""
    try:
        from trading.account_policy import is_live_environment
        return bool(is_live_environment(account))
    except Exception:  # noqa: BLE001
        return False


def _is_live_authorized(account) -> bool:
    """D4d defense-in-depth (import-local; fail-closed): a LIVE recovery candidate must ALSO currently hold a
    VALID §3 ``LiveExecutionAuthorization``. This mirrors readiness condition-11, the arm gate and the order-time
    bridge — all of which re-derive live authorization — so a LIVE account whose authorization is
    materially-invalidated (strategy reassigned/removed/resized, identity drift) is NOT relaunched merely because
    its durable arm bit persists. Recovery still only READS the authorization; it NEVER creates one. DEMO and the
    flag-off path never reach this (short-circuited), so both remain byte-identical to pre-D4d."""
    try:
        from execution.live_authz import is_live_execution_authorized
        return bool(is_live_execution_authorized(account))
    except Exception:  # noqa: BLE001
        return False


def _armed_and_matched(ws, account) -> bool:
    """The 'should be trading' predicate: an ARMED, account-confirmed, previously-matched workspace. This is what
    makes a terminal-less state a genuine outage (rather than a fresh/unconfirmed onboarding tenant).

    D4d: environment-agnostic INFRASTRUCTURE restore — a DEMO account (byte-identical) OR, when the D4d flag is
    on, a CLEAN LIVE account that STILL holds a valid §3 authorization. A LIVE candidate is still necessarily
    ARMED (execution_enabled + execution_authorized_at) — which for a LIVE account required the §3 ceremony — AND
    its authorization must still be valid now (``_is_live_authorized`` re-derives it exactly as condition-11 / the
    arm gate / the bridge do), so a revoked, unapproved, mismatched/UNKNOWN or materially-invalidated LIVE account
    is never a recovery candidate. Recovery relaunches the terminal ONLY; it NEVER creates an authorization and
    NEVER sets execution_enabled/execution_authorized_at — re-arm stays gated on the observe->readiness
    condition-11->arm chain (which fails LIVE closed without the flag + a valid §3 authorization)."""
    return (
        (getattr(account, "is_demo", False) is True
         or (_live_recovery_enabled() and _is_live_env(account) and _is_live_authorized(account)))
        and getattr(account, "workspace_confirmed_at", None) is not None
        and ws.execution_enabled is True
        and getattr(ws, "execution_authorized_at", None) is not None
        and ws.proj_account_match is True
    )


def _attempt_allowed(ws, now) -> bool:
    if (ws.liveness_recovery_count or 0) >= MAX_RECOVERY_ATTEMPTS:
        return False
    last = ws.liveness_recovery_at
    if last is not None and (now - last).total_seconds() < RECOVERY_COOLDOWN_S:
        return False
    return True


def _claim_attempt(pk, now) -> bool:
    """Atomically CLAIM one relaunch attempt: re-check (under a row lock) the workspace is still armed and still
    within the bound/cooldown, then stamp the attempt BEFORE any host call. False if concurrently consumed."""
    with transaction.atomic():
        ws = HostedMt5Workspace.objects.select_for_update().select_related("trading_account").get(pk=pk)
        account = getattr(ws, "trading_account", None)
        if account is None or not _armed_and_matched(ws, account) or not _attempt_allowed(ws, now):
            return False
        ws.liveness_recovery_at = now
        ws.liveness_recovery_count = (ws.liveness_recovery_count or 0) + 1
        ws.save(update_fields=["liveness_recovery_at", "liveness_recovery_count", "updated_at"])
        return True


def _safe_call(ex, method_name, *args, **kwargs) -> dict:
    fn = getattr(ex, method_name, None)
    if fn is None:
        return {"ok": False, "reason": "executor_incomplete"}
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — host errors are sanitised, never propagated or logged verbatim
        logger.warning("liveness_recovery: host step %s errored", method_name)
        return {"ok": False, "reason": "host_error"}


def _open_terminal_down_alert(account, *, exhausted: bool) -> None:
    """Raise (idempotently) ONE operator alert that this armed workspace is disarmed because its terminal is
    unavailable. Best-effort; never raises into the runner. No broker secret — the internal account id only."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = _alert_dedup_key(account.id)
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.CRITICAL,
            component=Component.MT5_TERMINAL,
            trading_account_id=account.id,
            title=f"[GuvFX execution] Workspace acct {account.id} disarmed: MT5 terminal unavailable",
            body=("[GuvFX execution] Workspace acct " + str(account.id) + " disarmed: MT5 terminal unavailable / "
                  "observation stale. Automated execution is paused safely. Recovery required."
                  + (" Automatic relaunch attempts exhausted — operator intervention needed." if exhausted else "")),
            dedup_key=dedup_key, status=AlertEvent.Status.OPEN,
            detail={"account_id": account.id, "reason": "workspace_observation_stale_terminal_down",
                    "exhausted": bool(exhausted)})
        logger.error("liveness_recovery: ALERT acct %s terminal down (execution paused)", account.id)
    except Exception:  # pragma: no cover — alerting is best-effort
        logger.exception("liveness_recovery: open-alert failed acct %s", getattr(account, "id", "?"))


def _resolve_terminal_down_alert(account) -> bool:
    """Resolve any OPEN terminal-down alert for this account and record ONE INFO recovery alert. Returns True if
    an OPEN alert was actually resolved (so the caller can count genuine recoveries). Best-effort."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = _alert_dedup_key(account.id)
        open_qs = AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN)
        n = open_qs.update(status=AlertEvent.Status.RESOLVED, resolved_at=timezone.now())
        if n:
            AlertEvent.objects.create(
                severity=AlertEvent.Severity.INFO,
                component=Component.MT5_TERMINAL,
                trading_account_id=account.id,
                title=f"[GuvFX execution] RECOVERED: workspace acct {account.id} re-armed",
                body=("[GuvFX execution] RECOVERED: workspace acct " + str(account.id) + " terminal restored, "
                      "account/server reverified, automated execution re-armed."),
                dedup_key=f"{_ALERT_PREFIX}_recovered:account:{account.id}:{int(timezone.now().timestamp())}",
                status=AlertEvent.Status.RESOLVED, detail={"account_id": account.id})
            logger.info("liveness_recovery: RECOVERED acct %s (terminal restored + re-armed)", account.id)
        return bool(n)
    except Exception:  # pragma: no cover — alerting is best-effort
        logger.exception("liveness_recovery: resolve-alert failed acct %s", getattr(account, "id", "?"))
        return False


def _resolve_recovered_alerts() -> int:
    """First pass: resolve OPEN terminal-down alerts whose workspace is now healthy AGAIN (armed + observation
    fresh + eligible). A recovered workspace is fresh, so it is excluded from the stale-candidate loop below;
    this is where its OPEN alert is closed + a recovery alert recorded. Returns the recovered count."""
    recovered = 0
    try:
        from reliability.models import AlertEvent
        open_alerts = (AlertEvent.objects.filter(dedup_key__startswith=f"{_ALERT_PREFIX}:account:",
                                                 status=AlertEvent.Status.OPEN)
                       .values_list("trading_account_id", flat=True).distinct())
        for acct_id in open_alerts:
            if acct_id is None or acct_id in _RESERVED_ACCOUNT_IDS:
                continue
            ws = (HostedMt5Workspace.objects.select_related("trading_account")
                  .filter(trading_account_id=acct_id).first())
            if ws is None:
                continue
            account = ws.trading_account
            if (_armed_and_matched(ws, account) and _observation_fresh(ws)
                    and PersistentWorkspaceProvider().evaluate(account).eligible):
                if _resolve_terminal_down_alert(account):
                    recovered += 1
    except Exception:  # pragma: no cover — best-effort
        logger.exception("liveness_recovery: recovered-scan failed")
    return recovered


# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
# ESTABLISHED-ACCOUNT-AUTO-RECOVERY (packet-109) — relaunch the DOWN terminal of an ESTABLISHED, identity-pinned,
# MONITORING-ONLY account (prior verified account_read evidence) that is NOT armed. This is the gap packet-108
# found: such an account was classified "onboarding" and never relaunched, so #50 could not self-heal. DARK unless
# HOSTED_ESTABLISHED_RECOVERY_ENABLED (ANDed with the master + liveness gates). Same relaunch primitive, bounds,
# cooldown, observe gate and fail-closed rules as the armed path; it NEVER arms, logs in, or writes a snapshot.
# ──────────────────────────────────────────────────────────────────────────────────────────────────────────────
_ESTABLISHED_ALERT_PREFIX = "hosted_established_terminal_down"


def _established_recovery_enabled() -> bool:
    """Third-level DARK gate (import-local; fail-closed). OFF ⇒ the liveness pass is byte-identical to before
    (ARMED candidates only). It is ANDed with the master + liveness gates by the caller."""
    try:
        from hosted_workspace.flags import hosted_established_recovery_enabled
        return hosted_established_recovery_enabled()
    except Exception:  # noqa: BLE001
        return False


# ── ESTABLISHED-RECOVERY HARDENING CONFIG (packet-118) ────────────────────────────────────────────────────────
# Three independently-governed, strictly-parsed, fail-closed controls that let the DARK feature progress from a
# one-account acceptance test toward a tightly-bounded #50-only pilot WITHOUT enabling anything. All read LIVE from
# settings-then-env (the same idiom as flags._flag), so none require a restart and none change production config.
# DEFAULTS preserve pre-118 behaviour byte-for-byte: the recovery threshold defaults to the 900s dashboard window,
# and the host-breaker caps default generously (a single-account relaunch is never blocked). The ALLOW-LIST is the
# one deliberately fail-closed-EMPTY control: when the established flag is on but no allow-list is configured, NO
# account is recovered (a pilot must opt accounts in explicitly). None of these grant authority or touch execution.
RECOVERY_STALE_DEFAULT_S = 900           # == the dashboard/portfolio freshness window (byte-identical default)
RECOVERY_STALE_MIN_S = 300               # < the ~5-min (300s) account_read collector cadence ⇒ UNSAFE (false positives)
RECOVERY_STALE_MAX_S = 86400
HOST_WINDOW_DEFAULT_S = 3600
HOST_MAX_RELAUNCHES_DEFAULT = 3
HOST_MAX_DOWN_DEFAULT = 2

# ── OPERATING-MODEL HARDENING (packet-127) ────────────────────────────────────────────────────────────────────
# The conservative steady-state operating model: recovery is default-OFF globally; production activation is
# ALLOW-LIST-only; the initial cohort is SMALL (<= STEADY_STATE_COHORT_MAX) with the breaker pinned to 1-down /
# 1-relaunch. An "approved steady-state" configuration is one that already matches that shape — so a configuration
# that enables recovery WITHOUT either an active governed lease OR this approved shape is almost certainly leftover
# bounded-acceptance configuration that a teardown failed to remove, and the cron warns about it every cycle.
STEADY_STATE_COHORT_MAX = 1              # initial cohort size cap (prepared, not enabled — allow-list defaults empty)


def _cfg_raw(name):
    """Live settings-then-env read (never raises)."""
    try:
        from django.conf import settings
        val = getattr(settings, name, None)
        if val is None:
            val = os.getenv(name, None)
        return val
    except Exception:  # noqa: BLE001
        return None


def _cfg_int_checked(name, default, min_v, max_v):
    """Strict int from settings-then-env. Returns ``(value, malformed)``:
      * MISSING/empty ⇒ ``(default, False)`` — an unset control is its safe default, NOT a misconfiguration.
      * present + valid-in-range ⇒ ``(value, False)``.
      * present + non-int / out-of-range ⇒ ``(default, True)`` — a REAL misconfiguration the caller fails closed on.
    """
    raw = _cfg_raw(name)
    if raw is None or (isinstance(raw, str) and str(raw).strip() == ""):
        return default, False
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("established_recovery: %s malformed (%r) → fail-closed to default %s", name, raw, default)
        return default, True
    if v < min_v or v > max_v:
        logger.warning("established_recovery: %s out of range [%s,%s] (%s) → fail-closed to default %s",
                       name, min_v, max_v, v, default)
        return default, True
    return v, False


def _established_recovery_stale_s() -> int:
    """(A) DEDICATED recovery staleness threshold — the SOLE recovery consumer; the dashboard/portfolio 900s windows
    (analytics.equity_snapshots.DEFAULT_FRESHNESS_S, execution.monitoring_enrolment.SNAPSHOT_FRESHNESS_S) are
    untouched. Default 900 (byte-identical). A malformed/zero/negative/unsafe (< the 300s collector cadence) value
    fails closed to the conservative 900 (slower = safe), never to a more-aggressive value."""
    value, _malformed = _cfg_int_checked(
        "HOSTED_ESTABLISHED_RECOVERY_SNAPSHOT_STALE_S", RECOVERY_STALE_DEFAULT_S, RECOVERY_STALE_MIN_S, RECOVERY_STALE_MAX_S)
    return value


def _established_recovery_allowlist() -> frozenset:
    """(B) FAIL-CLOSED recovery account allow-list — the set of account ids explicitly permitted to be recovery
    CANDIDATES. Parsed strictly as positive ints from a comma/space-separated list. FAIL-CLOSED: missing/empty ⇒
    EMPTY (no candidate); ANY malformed or non-positive entry ⇒ EMPTY (never silently keep the valid ids). Membership
    is purely ADDITIVE/NARROWING — it NEVER bypasses eligibility, identity, ownership, copier-exclusion, the LIVE
    gate, or any other requirement; it only restricts WHICH accounts may be considered. With the flag off this is
    never consulted (the pass short-circuits)."""
    raw = _cfg_raw("HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS")
    if raw is None:
        return frozenset()
    s = str(raw).strip()
    if not s:
        return frozenset()
    out = set()
    for tok in s.replace(",", " ").split():
        try:
            v = int(tok)
        except (TypeError, ValueError):
            logger.warning("established_recovery: allow-list malformed entry %r → FAIL-CLOSED (zero candidates)", tok)
            return frozenset()
        if v <= 0:
            logger.warning("established_recovery: allow-list non-positive entry %r → FAIL-CLOSED (zero candidates)", tok)
            return frozenset()
        out.add(v)
    return frozenset(out)


def _host_breaker_config():
    """(C) Host restart-storm circuit-breaker config → ``(window_s, max_relaunches, max_down, malformed)``. Any
    malformed/out-of-range value sets ``malformed=True`` so the breaker FAILS CLOSED (blocks). Missing ⇒ generous
    defaults (byte-identical: a lone relaunch is never blocked)."""
    window_s, m1 = _cfg_int_checked("HOSTED_ESTABLISHED_RECOVERY_HOST_WINDOW_S", HOST_WINDOW_DEFAULT_S, 1, 86400)
    max_relaunch, m2 = _cfg_int_checked("HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_RELAUNCHES", HOST_MAX_RELAUNCHES_DEFAULT, 1, 100000)
    max_down, m3 = _cfg_int_checked("HOSTED_ESTABLISHED_RECOVERY_HOST_MAX_DOWN", HOST_MAX_DOWN_DEFAULT, 1, 100000)
    return window_s, max_relaunch, max_down, (m1 or m2 or m3)


def _stale_malformed() -> bool:
    """True iff the dedicated stale threshold env is present-but-invalid (incl. < the 300s floor) → it fails closed
    to 900. Exposed for the config metadata/warnings so an operator sees a rejected-and-clamped value."""
    _v, malformed = _cfg_int_checked(
        "HOSTED_ESTABLISHED_RECOVERY_SNAPSHOT_STALE_S", RECOVERY_STALE_DEFAULT_S, RECOVERY_STALE_MIN_S, RECOVERY_STALE_MAX_S)
    return malformed


def _is_approved_steady_state_config() -> bool:
    """True iff the CURRENT recovery configuration matches the approved conservative steady-state shape:
    established recovery enabled, a NON-empty allow-list no larger than ``STEADY_STATE_COHORT_MAX``, the host breaker
    pinned to 1-down / 1-relaunch and not malformed, and a stale threshold that parsed cleanly (>= the 300s floor).
    This is the ONLY production shape that may run WITHOUT an active governed lease without warning."""
    try:
        if not _established_recovery_enabled():
            return False
        # A LIVE recovery configuration may NEVER qualify as an unleased "approved steady-state" — a bounded
        # acceptance arms exactly the LIVE allow-list-of-one / breaker-1/1 shape, so treating that shape as approved
        # would blind the leftover-config warning to a LIVE acceptance whose teardown failed. Only a DEMO-only cohort
        # may run unleased without warning; a LIVE cohort must run under an active governed lease (or be warned).
        if _live_recovery_enabled():
            return False
        allow = _established_recovery_allowlist()
        if not allow or len(allow) > STEADY_STATE_COHORT_MAX:
            return False
        _w, max_relaunch, max_down, breaker_malformed = _host_breaker_config()
        if breaker_malformed or max_down != 1 or max_relaunch != 1:
            return False
        if _stale_malformed():
            return False
        return True
    except Exception:  # noqa: BLE001 — unknown ⇒ not approved (warn)
        return False


def _active_governed_lease() -> bool:
    """True iff a live (ACTIVE) governed operational lease exists (tolerant; any error ⇒ treated as NO lease so the
    'enabled without lease' warning errs toward warning, never toward silence)."""
    try:
        from django.utils import timezone
        from hosted_workspace import recovery_lease
        status, _ = recovery_lease.read_lease(timezone.now())
        return status == recovery_lease.LeaseStatus.ACTIVE
    except Exception:  # noqa: BLE001
        return False


def recovery_enablement_warnings() -> list:
    """Operator-facing WARNINGS about the current recovery configuration (no secret, no env dump). Empty list when
    the configuration is safe/inert. Emitted every cron cycle by the established pass and surfaced by ``recovery_ops
    config`` so leftover bounded-acceptance configuration can never run unnoticed."""
    warnings = []
    try:
        established = _established_recovery_enabled()
        live = _live_recovery_enabled()
        allow = _established_recovery_allowlist()
        if established and not allow:
            warnings.append("established recovery ENABLED with EMPTY/malformed allow-list → recovers NOTHING "
                            "(fail-closed); set HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS or disable the flag.")
        if live and not allow:
            warnings.append("LIVE recovery enabled without an allow-list → no LIVE account can be a candidate "
                            "(fail-closed).")
        if live and not _active_governed_lease():
            warnings.append("LIVE recovery ENABLED without an ACTIVE governed lease → LIVE recovery must run only "
                            "within a bounded governed lease; if unexpected this is almost certainly LEFTOVER "
                            "bounded-acceptance configuration a teardown failed to remove — investigate NOW.")
        if established and not _active_governed_lease() and not _is_approved_steady_state_config():
            warnings.append("established recovery ENABLED without an active governed lease AND not an approved "
                            "steady-state cohort config (allow-list size<=%d, breaker 1-down/1-relaunch) → likely "
                            "LEFTOVER bounded-acceptance configuration; investigate/teardown immediately."
                            % STEADY_STATE_COHORT_MAX)
        _w, _mr, _md, breaker_malformed = _host_breaker_config()
        if breaker_malformed:
            warnings.append("host restart-storm breaker config MALFORMED → fail-closed (blocks all relaunches).")
        if _stale_malformed():
            warnings.append("recovery stale threshold MALFORMED or below the 300s floor → fail-closed to 900s.")
    except Exception:  # noqa: BLE001
        warnings.append("recovery configuration could not be evaluated → treat as unsafe (investigate).")
    return warnings


def recovery_config_metadata() -> dict:
    """Safe snapshot of the recovery operating-model configuration for observability: NO secret and NO raw
    environment dump — only the non-secret, operator-set names/ids/counts (allow-list ids, thresholds, breaker caps)
    and the governed-lease status. It deliberately echoes those non-secret config values; it never reads or emits a
    secret-bearing environment variable."""
    try:
        window_s, max_relaunch, max_down, breaker_malformed = _host_breaker_config()
        allow = sorted(_established_recovery_allowlist())
        meta = {
            "established_recovery_enabled": bool(_established_recovery_enabled()),
            "live_recovery_enabled": bool(_live_recovery_enabled()),
            "allowlist_ids": allow,
            "allowlist_size": len(allow),
            "stale_threshold_s": _established_recovery_stale_s(),
            "stale_threshold_malformed": _stale_malformed(),
            "host_breaker": {"window_s": window_s, "max_relaunches": max_relaunch, "max_down": max_down,
                             "malformed": bool(breaker_malformed)},
            "steady_state_cohort_max": STEADY_STATE_COHORT_MAX,
            "approved_steady_state_config": _is_approved_steady_state_config(),
            "warnings": recovery_enablement_warnings(),
        }
        try:
            from django.utils import timezone
            from hosted_workspace import recovery_lease
            meta["governed_lease"] = recovery_lease.status_metadata(timezone.now())
        except Exception:  # noqa: BLE001
            meta["governed_lease"] = {"status": "UNKNOWN"}
        return meta
    except Exception:  # noqa: BLE001
        return {"error": "recovery_config_metadata unavailable"}


def _resolve_rdp_host(ws) -> str:
    """The physical host (rdp_host) a workspace's terminal lives on, via its execution_node else the account's
    terminal_node. '' when underivable (fail-closed: such a candidate is skipped, never breaker-evaluated)."""
    account = getattr(ws, "trading_account", None)
    node = getattr(ws, "execution_node", None) or getattr(account, "terminal_node", None)
    return str(getattr(node, "rdp_host", "") or "").strip()


def _host_recent_relaunch_counts(now, window_s) -> dict:
    """Deterministic per-host count of recovery relaunches in the rolling ``window_s`` (from the durable
    ``liveness_recovery_at`` — reused, NO migration). Counts BOTH armed + established relaunches on the host (a
    restart-storm is host-wide). Raises on query failure so the caller FAILS CLOSED (blocks). No cross-host mixing."""
    from datetime import timedelta
    cutoff = now - timedelta(seconds=window_s)
    counts: dict = {}
    for ws in (HostedMt5Workspace.objects
               .filter(liveness_recovery_at__gte=cutoff)
               .select_related("execution_node", "trading_account", "trading_account__terminal_node")):
        host = _resolve_rdp_host(ws)
        if host:
            counts[host] = counts.get(host, 0) + 1
    return counts


def _record_recovery_latency_event(account_id, detected_at, requested_at, outcome_at, relaunch_ok) -> None:
    """(D) Truthful, event-level recovery-latency evidence (structured log). In-pass measurements are genuine
    timestamps; the cross-cycle ones (first identity-matched read / first genuine collector snapshot) are NOT yet
    available in this pass and are recorded as NULL (never 0) — they are filled truthfully at resolution time by
    ``_resolve_established_recovered_alerts`` from the durable ``liveness_recovery_at`` + the real snapshot time.
    No snapshot is ever fabricated. p50/p95 aggregation is deliberately NOT claimed here (see ADR-0107 addendum:
    percentiles need a durable metrics store / later migration; this packet records event-level evidence only)."""
    try:
        def _lat(a, b):
            return round((b - a).total_seconds(), 3) if (a is not None and b is not None) else None
        ev = {
            "event": "established_recovery_latency",
            "account_id": account_id,
            "eligibility_detected_at": detected_at.isoformat() if detected_at else None,
            "recovery_requested_at": requested_at.isoformat() if requested_at else None,
            "relaunch_outcome_at": outcome_at.isoformat() if outcome_at else None,
            "relaunch_ok": bool(relaunch_ok),
            "detection_to_request_s": _lat(detected_at, requested_at),
            "request_to_relaunch_s": _lat(requested_at, outcome_at),
            "first_verified_identity_at": None,      # cross-cycle — filled at resolution (never 0)
            "first_genuine_snapshot_at": None,       # cross-cycle — filled at resolution (never 0)
            "relaunch_to_identity_s": None,
            "relaunch_to_snapshot_s": None,
        }
        logger.info("established_recovery_latency %s", json.dumps(ev, sort_keys=True))
    except Exception:  # pragma: no cover — evidence logging is best-effort, never breaks recovery
        logger.exception("established_recovery: latency-event log failed acct %s", account_id)


def _recovered_latency_evidence(account_id, ws) -> dict:
    """(D) Truthful relaunch→fresh-identity-matched-read latency, computed at RESOLUTION from the DURABLE
    ``liveness_recovery_at`` + the REAL snapshot ``observed_at``. Unavailable components are null (never 0); no
    snapshot is fabricated. Percentiles are NOT claimed (documented aggregation gap)."""
    out = {"relaunch_outcome_at": None, "first_genuine_snapshot_at": None, "first_verified_identity_at": None,
           "relaunch_to_snapshot_s": None, "relaunch_to_identity_s": None,
           "latency_note": "event-level evidence only; p50/p95 aggregation not computed (no metrics store — ADR-0107 addendum)"}
    try:
        from analytics.models import AccountEquitySnapshot
        from execution.models import HostedExecutionEndpoint
        relaunch_at = getattr(ws, "liveness_recovery_at", None)
        expected_login = (HostedExecutionEndpoint.objects
                          .filter(trading_account_id=account_id)
                          .exclude(state=HostedExecutionEndpoint.State.RETIRED)
                          .order_by("-id").values_list("expected_login", flat=True).first())
        expected_login = (expected_login or "").strip()
        snap_at = None
        # The FIRST identity-matched read AT-OR-AFTER the relaunch (ascending) — so ``first_genuine_snapshot_at`` is
        # literally the first post-relaunch read and the latency is measured against THIS recovery's relaunch, never
        # a pre-relaunch read (review-118 LOW). Null when there is no current pin or no post-relaunch read yet.
        if expected_login and relaunch_at is not None:
            snap_at = (AccountEquitySnapshot.objects
                       .filter(trading_account_id=account_id, source="account_read", equity__isnull=False,
                               observed_login=expected_login, observed_at__gte=relaunch_at)
                       .order_by("observed_at").values_list("observed_at", flat=True).first())
        out["relaunch_outcome_at"] = relaunch_at.isoformat() if relaunch_at else None
        out["first_genuine_snapshot_at"] = snap_at.isoformat() if snap_at else None
        out["first_verified_identity_at"] = snap_at.isoformat() if snap_at else None
        if relaunch_at is not None and snap_at is not None and snap_at >= relaunch_at:
            lat = round((snap_at - relaunch_at).total_seconds(), 3)
            out["relaunch_to_snapshot_s"] = lat
            out["relaunch_to_identity_s"] = lat
    except Exception:  # pragma: no cover
        logger.exception("established_recovery: recovered-latency evidence failed acct %s", account_id)
    return out


def _monitoring_snapshot_fresh(account_id, now) -> bool:
    """True iff the latest EQUITY-BEARING ``account_read`` snapshot for this account, UNDER THE CURRENT IDENTITY
    PIN, is within the monitoring freshness window — the authoritative 'monitoring is flowing' signal for a
    bridge-monitored account (whose ``last_decision_at`` the hosted-observe writer does NOT touch, so
    ``_observation_fresh`` cannot judge it). Mirrors the enrolment/equity-curve freshness window so the dashboard
    and this pass never diverge.

    Pinned to the endpoint's ``expected_login`` (symmetric with the eligibility drift gate ``RC_IDENTITY_PIN_DRIFT``):
    a recent read under a DRIFTED login must NOT count as 'monitoring restored' and must NOT silently suppress an
    observe/relaunch or auto-resolve a recovery alert while the pinned identity is actually not being served.
    Fail-closed (False) when there is no current pin or on any error — a healthy account is at worst observed once
    (the read-only observe then confirms it is up and it is skipped), never wrongly relaunched.

    STALENESS THRESHOLD (packet-118-A): this is the SOLE consumer of the dedicated recovery threshold
    ``_established_recovery_stale_s()`` (default 900 — byte-identical to the dashboard window). Decoupled on purpose:
    an operator may shorten RECOVERY's reaction (never below the 300s collector cadence) without altering the
    dashboard/portfolio freshness semantics, which keep their own 900s constants."""
    try:
        from analytics.models import AccountEquitySnapshot
        from execution.models import HostedExecutionEndpoint
        stale_s = _established_recovery_stale_s()
        expected_login = (HostedExecutionEndpoint.objects
                          .filter(trading_account_id=account_id)
                          .exclude(state=HostedExecutionEndpoint.State.RETIRED)
                          .order_by("-id").values_list("expected_login", flat=True).first())
        expected_login = (expected_login or "").strip()
        if not expected_login:
            return False     # no current identity pin → cannot assert monitoring under the correct identity
        latest = (AccountEquitySnapshot.objects
                  .filter(trading_account_id=account_id, source="account_read", equity__isnull=False,
                          observed_login=expected_login)
                  .order_by("-observed_at").values_list("observed_at", flat=True).first())
        if latest is None:
            return False
        age = (now - latest).total_seconds()
        return 0 <= age <= stale_s
    except Exception:  # noqa: BLE001
        return False


def _established_alert_dedup_key(account_id) -> str:
    return f"{_ESTABLISHED_ALERT_PREFIX}:account:{account_id}"


def _open_established_down_alert(account, *, exhausted: bool, terminal_up: bool) -> None:
    """Idempotently OPEN one operator alert that an established MONITORING account has stopped producing data
    (its equity snapshots went stale). ``terminal_up`` distinguishes 'terminal restored but login required'
    (collector still not producing an identity-matched read) from 'terminal down'. Best-effort; no broker
    secret (internal account id only); never raises into the runner."""
    try:
        from reliability.constants import Component
        from reliability.models import AlertEvent
        dedup_key = _established_alert_dedup_key(account.id)
        if AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN).exists():
            return
        reason = ("monitoring_stale_terminal_up_login_required" if terminal_up
                  else "monitoring_stale_terminal_down")
        tail = (" Terminal is running but no identity-matched read — human MT5 login/identity review required."
                if terminal_up else
                (" Automatic relaunch attempts exhausted — operator intervention needed." if exhausted else ""))
        AlertEvent.objects.create(
            severity=AlertEvent.Severity.CRITICAL,
            component=Component.MT5_TERMINAL,
            trading_account_id=account.id,
            title=f"[GuvFX monitoring] Account {account.id} monitoring stale: MT5 data not flowing",
            body=("[GuvFX monitoring] Account " + str(account.id) + " monitoring stale: broker equity reads have "
                  "stopped. Automatic monitoring recovery engaged (terminal relaunch only; no login/arm)." + tail),
            dedup_key=dedup_key, status=AlertEvent.Status.OPEN,
            detail={"account_id": account.id, "reason": reason, "exhausted": bool(exhausted),
                    "terminal_up": bool(terminal_up)})
        logger.error("established_recovery: ALERT acct %s monitoring stale (terminal_up=%s)",
                     account.id, bool(terminal_up))
    except Exception:  # pragma: no cover — alerting is best-effort
        logger.exception("established_recovery: open-alert failed acct %s", getattr(account, "id", "?"))


def _resolve_established_recovered_alerts(now) -> int:
    """First pass: resolve OPEN established-monitoring alerts whose account is producing fresh identity-matched
    reads AGAIN AND is still eligible. Records ONE INFO recovery alert. Returns the recovered count."""
    recovered = 0
    try:
        from reliability.models import AlertEvent
        from hosted_workspace.recovery_eligibility import established_account_recovery_eligible
        open_alerts = (AlertEvent.objects.filter(dedup_key__startswith=f"{_ESTABLISHED_ALERT_PREFIX}:account:",
                                                 status=AlertEvent.Status.OPEN)
                       .values_list("trading_account_id", flat=True).distinct())
        for acct_id in open_alerts:
            if acct_id is None or acct_id in _RESERVED_ACCOUNT_IDS:
                continue
            ws = (HostedMt5Workspace.objects.select_related("trading_account")
                  .filter(trading_account_id=acct_id).first())
            if ws is None:
                continue
            account = ws.trading_account
            if (_monitoring_snapshot_fresh(acct_id, now)
                    and established_account_recovery_eligible(ws, account).eligible):
                dedup_key = _established_alert_dedup_key(acct_id)
                n = (AlertEvent.objects.filter(dedup_key=dedup_key, status=AlertEvent.Status.OPEN)
                     .update(status=AlertEvent.Status.RESOLVED, resolved_at=now))
                if n:
                    from reliability.constants import Component
                    # (D) truthful cross-cycle latency: relaunch→fresh-identity-matched read, from the DURABLE
                    # liveness_recovery_at (set atomically at relaunch) + the real snapshot time. The fresh read IS
                    # the first identity-verified read (the collector writes only verify_identity reads), so identity
                    # and snapshot latencies coincide. Unavailable components stay null (never 0); no fabrication.
                    lat = _recovered_latency_evidence(acct_id, ws)
                    AlertEvent.objects.create(
                        severity=AlertEvent.Severity.INFO, component=Component.MT5_TERMINAL,
                        trading_account_id=acct_id,
                        title=f"[GuvFX monitoring] RECOVERED: account {acct_id} monitoring restored",
                        body=("[GuvFX monitoring] RECOVERED: account " + str(acct_id) + " terminal restored and "
                              "the identity-firewalled collector is producing fresh reads again."),
                        dedup_key=f"{_ESTABLISHED_ALERT_PREFIX}_recovered:account:{acct_id}:{int(now.timestamp())}",
                        status=AlertEvent.Status.RESOLVED, detail=dict({"account_id": acct_id}, **lat))
                    recovered += 1
                    logger.info("established_recovery: RECOVERED acct %s (monitoring restored) %s",
                                acct_id, json.dumps(lat, sort_keys=True))
    except Exception:  # pragma: no cover — best-effort
        logger.exception("established_recovery: recovered-scan failed")
    return recovered


def _claim_established_attempt(pk, now) -> bool:
    """Atomically CLAIM one relaunch attempt for an ESTABLISHED monitoring workspace: re-check under a row lock
    that it is still established-eligible, still NOT armed (disjoint from the armed pass), and within the
    bound/cooldown, then stamp the attempt (reusing ``liveness_recovery_at/count``) BEFORE any host call. False
    if concurrently consumed/advanced."""
    from hosted_workspace.recovery_eligibility import established_account_recovery_eligible
    with transaction.atomic():
        ws = HostedMt5Workspace.objects.select_for_update().select_related("trading_account").get(pk=pk)
        account = getattr(ws, "trading_account", None)
        if account is None:
            return False
        # Disjoint from the armed pass: an armed workspace is handled there, never double-claimed here.
        if ws.execution_enabled is True and getattr(ws, "execution_authorized_at", None) is not None:
            return False
        # Allow-list re-check UNDER THE ROW LOCK (packet-118-B): membership can only NARROW, and is re-asserted here
        # so a concurrent config change cannot let a now-de-listed account be relaunched. Never bypasses eligibility.
        if getattr(account, "id", None) not in _established_recovery_allowlist():
            return False
        if not established_account_recovery_eligible(ws, account).eligible or not _attempt_allowed(ws, now):
            return False
        ws.liveness_recovery_at = now
        ws.liveness_recovery_count = (ws.liveness_recovery_count or 0) + 1
        ws.save(update_fields=["liveness_recovery_at", "liveness_recovery_count", "updated_at"])
        return True


def _run_established_monitoring_recovery(*, now, resolve_ex, observe_fn) -> dict:
    """One established-MONITORING liveness pass (additive, disjoint candidate class). DARK unless the established
    flag is on. Keyed off equity-snapshot staleness (the dedicated packet-118 recovery threshold). Fail-closed
    throughout; relaunch is the SAME account-scoped RELAUNCH_TERMINAL primitive, bounded + single-flight; arms
    nothing, writes no snapshot. Packet-118 hardening: a fail-closed account ALLOW-LIST (empty ⇒ zero candidates),
    a host-wide restart-storm CIRCUIT BREAKER (per-host recent-relaunch rate + concurrent-down caps, blocking
    BEFORE any host contact and failing closed on malformed config / unavailable cohort data), and truthful
    event-level recovery-latency evidence. Returns a secret-free sub-summary with a per-reason breakdown."""
    out = {"enabled": True, "candidates": 0, "eligible": 0, "ineligible": 0, "down": 0, "relaunched": 0,
           "recovered": 0, "terminal_up_login_required": 0, "skipped_healthy": 0, "skipped_cooldown": 0,
           "skipped_ambiguous": 0, "skipped_no_executor": 0, "skipped_allowlist": 0, "skipped_host_circuit": 0,
           "host_circuit_open": False, "errors": 0, "reasons": {}}
    from hosted_workspace.recovery_eligibility import established_account_recovery_eligible

    # (packet-127) Operating-model guard: warn LOUDLY every cycle if recovery is enabled in an unsafe/leftover shape
    # (enabled without an active governed lease and not an approved steady-state cohort, empty allow-list, malformed
    # breaker/threshold). This never changes behaviour — it makes a botched teardown or a careless enablement visible.
    for _w in recovery_enablement_warnings():
        logger.warning("established_recovery OPERATING-MODEL: %s", _w)

    # Alert resolution for recovered accounts runs first (observational; it clears a stale alert when monitoring
    # has returned, independent of the pilot allow-list).
    out["recovered"] = _resolve_established_recovered_alerts(now)

    # (B) FAIL-CLOSED ALLOW-LIST: with the established flag on but no allow-list configured, NO account is a
    # recovery candidate. A pilot opts accounts in explicitly; an empty/missing/malformed allow-list recovers none.
    allow = _established_recovery_allowlist()
    if not allow:
        out["reasons"]["allowlist_empty"] = out["reasons"].get("allowlist_empty", 0) + 1
        return out

    # (C) host-breaker config (fail-closed when malformed).
    window_s, host_max_relaunch, host_max_down, breaker_malformed = _host_breaker_config()

    # Candidate universe: non-tombstoned, non-reserved, NON-ARMED workspaces (armed ones are the armed pass's job —
    # disjoint). ``is_active`` is NOT a filter (ELIGIBILITY-FIX-112). The allow-list + per-candidate eligibility
    # (hosted monitoring subject, NOT a Copy-Trading participant, identity-pinned, prior-verified, recoverable env)
    # NARROW this inclusive pre-filter; both are RE-CHECKED under the row lock in ``_claim_established_attempt``.
    qs = (HostedMt5Workspace.objects
          .filter(trading_account__disconnected_at__isnull=True)
          .exclude(execution_enabled=True, execution_authorized_at__isnull=False)
          .exclude(trading_account_id__in=_RESERVED_ACCOUNT_IDS))
    if not _live_recovery_enabled():
        qs = qs.filter(trading_account__is_demo=True)
    qs = qs.select_related("trading_account", "execution_node", "trading_account__terminal_node").iterator()

    # ── Phase 1: gather eligible + ALLOW-LISTED + STALE candidates, grouped by physical host (rdp_host). ──
    candidates = []   # (ws, account, rdp_host)
    host_down: dict = {}
    for ws in qs:
        account = getattr(ws, "trading_account", None)
        if account is None:
            out["errors"] += 1
            continue
        # Allow-list NARROWS the candidate set — it NEVER bypasses any gate below.
        if getattr(account, "id", None) not in allow:
            out["skipped_allowlist"] += 1
            continue
        res = established_account_recovery_eligible(ws, account)
        if not res.eligible:
            out["ineligible"] += 1
            out["reasons"][res.reason] = out["reasons"].get(res.reason, 0) + 1
            continue
        out["eligible"] += 1
        if _monitoring_snapshot_fresh(account.id, now):
            out["skipped_healthy"] += 1
            continue
        rdp_host = _resolve_rdp_host(ws)
        candidates.append((ws, account, rdp_host))
        if rdp_host:
            host_down[rdp_host] = host_down.get(rdp_host, 0) + 1
    out["candidates"] = len(candidates)

    # ── Phase 2: per-host recent-relaunch counts from the durable liveness_recovery_at (fail-closed on failure). ──
    try:
        host_recent = _host_recent_relaunch_counts(now, window_s)
    except Exception:  # noqa: BLE001 — unavailable cohort data ⇒ breaker fails closed (blocks) below
        logger.exception("established_recovery: host circuit-breaker query failed → fail-closed (block all)")
        host_recent = None
    relaunched_on_host: dict = {}

    # ── Phase 3: act per candidate — the host circuit breaker gates BEFORE any observe/relaunch (mutation). ──
    for (ws, account, rdp_host) in candidates:
        if not rdp_host:
            out["skipped_no_executor"] += 1
            continue
        # (C) HOST CIRCUIT BREAKER — fail-closed. Block before any host contact when: config malformed; cohort data
        # unavailable; too many siblings on THIS host are simultaneously down (a host-wide event, not N isolated
        # deaths); or the host's recent-relaunch budget (prior window + this pass) is spent. Per-host, deterministic,
        # no cross-host contamination.
        if breaker_malformed or host_recent is None:
            out["skipped_host_circuit"] += 1
            out["host_circuit_open"] = True
            continue
        if host_down.get(rdp_host, 0) > host_max_down:
            out["skipped_host_circuit"] += 1
            out["host_circuit_open"] = True
            logger.error("established_recovery: host circuit OPEN (concurrent-down=%s > %s) — blocking host cohort",
                         host_down.get(rdp_host, 0), host_max_down)
            continue
        if host_recent.get(rdp_host, 0) + relaunched_on_host.get(rdp_host, 0) >= host_max_relaunch:
            out["skipped_host_circuit"] += 1
            out["host_circuit_open"] = True
            logger.error("established_recovery: host circuit OPEN (recent-relaunches=%s >= %s) — blocking",
                         host_recent.get(rdp_host, 0) + relaunched_on_host.get(rdp_host, 0), host_max_relaunch)
            continue
        ex = resolve_ex(account.id, rdp_host)
        if ex is None:                       # DARK / unarmed host executor — burn no attempt
            out["skipped_no_executor"] += 1
            continue
        # READ-ONLY liveness probe — never infer a down terminal from the stale projection.
        obs = observe_fn(ws) if observe_fn is not None else _safe_call(ex, "observe", rdp_host=rdp_host)
        proc = (obs or {}).get("process_running", None)
        if proc is True:
            out["terminal_up_login_required"] += 1
            _open_established_down_alert(account, exhausted=False, terminal_up=True)
            continue
        if proc is not False:               # ambiguous / unavailable → fail-closed (never relaunch)
            out["skipped_ambiguous"] += 1
            continue
        # Terminal confirmed DOWN for an established monitoring account: alert, then bounded relaunch.
        out["down"] += 1
        if not _attempt_allowed(ws, now):
            _open_established_down_alert(account, exhausted=True, terminal_up=False)
            out["skipped_cooldown"] += 1
            continue
        _open_established_down_alert(account, exhausted=False, terminal_up=False)
        detected_at = now
        requested_at = timezone.now()
        if not _claim_established_attempt(ws.pk, now):
            out["skipped_cooldown"] += 1
            continue
        # The CLAIM durably stamps ``liveness_recovery_at`` — exactly what the across-pass host breaker counts via
        # ``_host_recent_relaunch_counts`` — so the in-pass host budget is consumed per CLAIMED ATTEMPT (not per
        # success). This makes a burst of FAILING relaunches on one unhealthy host still stop at HOST_MAX_RELAUNCHES
        # within a single pass (review-118 LOW: attempt-based, consistent with the durable record).
        relaunched_on_host[rdp_host] = relaunched_on_host.get(rdp_host, 0) + 1
        slot = derive_slot(account.id)
        r = _safe_call(ex, "relaunch_terminal", slot["username"], slot["runtime_root"], rdp_host=rdp_host)
        _record_recovery_latency_event(account.id, detected_at, requested_at, timezone.now(), _ok(r))
        if _ok(r):
            out["relaunched"] += 1
            logger.info("established_recovery: relaunched terminal acct=%s (attempt %s)",
                        account.id, (ws.liveness_recovery_count or 0) + 1)
        else:
            out["errors"] += 1
    return out


def run_hosted_liveness_recovery(*, actor: str = SOURCE, executor_resolver=None, observe_fn=None) -> dict:
    """One liveness-recovery pass. DARK unless master + liveness flags on. Idempotent, loop-safe, fail-open per
    workspace. Arms nothing; places no order. Runs AFTER the observation pass so a workspace the observer just
    re-proved healthy is already FRESH (and excluded here). Returns a secret-free summary."""
    if not (hosted_persistent_mt5_enabled() and hosted_liveness_recovery_enabled()):
        return {"enabled": False, "recovered": 0, "candidates": 0, "down": 0, "relaunched": 0,
                "skipped_cooldown": 0, "skipped_healthy": 0, "skipped_ambiguous": 0,
                "skipped_no_executor": 0, "errors": 0, "established": {"enabled": False}}

    now = timezone.now()
    resolve_ex = executor_resolver or (lambda account_id, rdp_host: resolve_host_executor(
        account_id=account_id, rdp_host=rdp_host))

    recovered = _resolve_recovered_alerts()

    candidates = down = relaunched = 0
    skipped_cooldown = skipped_healthy = skipped_ambiguous = skipped_no_executor = errors = 0

    # Candidates: ARMED, confirmed, previously-matched, demo, NON-reserved workspaces whose last canonical
    # decision is STALE (a fresh workspace is trading fine — never observed/relaunched here). This is a small set
    # (armed AUTO_DEMO tenants only), so the per-candidate read-only observe adds negligible load.
    qs = (HostedMt5Workspace.objects
          .filter(execution_enabled=True, execution_authorized_at__isnull=False, proj_account_match=True,
                  trading_account__workspace_confirmed_at__isnull=False)
          # Never relaunch a REMOVED (tombstoned) account's terminal (removal already disarms execution_enabled, but
          # this is explicit defence in depth so a Stage-2 teardown is never fought by a relaunch).
          .filter(trading_account__disconnected_at__isnull=True)
          .exclude(trading_account_id__in=_RESERVED_ACCOUNT_IDS))
    # D4d: demo-only pre-filter when the LIVE-recovery flag is OFF (byte-identical); when ON, include LIVE and let
    # the per-candidate _armed_and_matched env gate decide (clean LIVE + armed only).
    if not _live_recovery_enabled():
        qs = qs.filter(trading_account__is_demo=True)
    qs = qs.select_related("trading_account", "execution_node").iterator()

    for ws in qs:
        account = getattr(ws, "trading_account", None)
        if account is None:
            errors += 1
            continue
        if _observation_fresh(ws):
            skipped_healthy += 1          # trading fine — do not observe/relaunch
            continue
        candidates += 1
        node = getattr(ws, "execution_node", None) or getattr(account, "terminal_node", None)
        rdp_host = str(getattr(node, "rdp_host", "") or "").strip()
        if not rdp_host:
            skipped_no_executor += 1
            continue
        ex = resolve_ex(account.id, rdp_host)
        if ex is None:                    # DARK / unarmed host executor — burn no attempt
            skipped_no_executor += 1
            continue
        # READ-ONLY liveness probe (never infer from the stale projection).
        obs = observe_fn(ws) if observe_fn is not None else _safe_call(ex, "observe", rdp_host=rdp_host)
        proc = (obs or {}).get("process_running", None)
        if proc is not False:
            # Terminal is up (or the observe was ambiguous/unavailable). Up-but-not-connected is capability
            # recovery's job; ambiguous is fail-closed (never relaunch a possibly-running terminal → no duplicate).
            if proc is True:
                skipped_healthy += 1
            else:
                skipped_ambiguous += 1
            continue
        # Terminal confirmed DOWN for an armed workspace: raise the operator alert, then relaunch (bounded).
        down += 1
        if not _attempt_allowed(ws, now):
            _open_terminal_down_alert(account, exhausted=True)
            skipped_cooldown += 1
            continue
        _open_terminal_down_alert(account, exhausted=False)
        if not _claim_attempt(ws.pk, now):
            skipped_cooldown += 1
            continue
        slot = derive_slot(account.id)
        r = _safe_call(ex, "relaunch_terminal", slot["username"], slot["runtime_root"], rdp_host=rdp_host)
        if _ok(r):
            relaunched += 1
            logger.info("liveness_recovery: relaunched terminal acct=%s (attempt %s)",
                        account.id, (ws.liveness_recovery_count or 0) + 1)
        else:
            errors += 1

    # ESTABLISHED-ACCOUNT-AUTO-RECOVERY (packet-109): additive, disjoint MONITORING-only candidate class. DARK
    # unless the established flag is on; when off this is byte-identical to the armed-only pass above.
    established = (_run_established_monitoring_recovery(now=now, resolve_ex=resolve_ex, observe_fn=observe_fn)
                   if _established_recovery_enabled() else {"enabled": False})

    return {"enabled": True, "recovered": recovered, "candidates": candidates, "down": down,
            "relaunched": relaunched, "skipped_cooldown": skipped_cooldown, "skipped_healthy": skipped_healthy,
            "skipped_ambiguous": skipped_ambiguous, "skipped_no_executor": skipped_no_executor, "errors": errors,
            "established": established}

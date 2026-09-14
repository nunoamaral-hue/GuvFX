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

import logging

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


def _armed_and_matched(ws, account) -> bool:
    """The 'should be trading' predicate: an ARMED, account-confirmed, previously-matched demo workspace. This
    is what makes a terminal-less state a genuine outage (rather than a fresh/unconfirmed onboarding tenant)."""
    return (
        getattr(account, "is_demo", False) is True
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


def run_hosted_liveness_recovery(*, actor: str = SOURCE, executor_resolver=None, observe_fn=None) -> dict:
    """One liveness-recovery pass. DARK unless master + liveness flags on. Idempotent, loop-safe, fail-open per
    workspace. Arms nothing; places no order. Runs AFTER the observation pass so a workspace the observer just
    re-proved healthy is already FRESH (and excluded here). Returns a secret-free summary."""
    if not (hosted_persistent_mt5_enabled() and hosted_liveness_recovery_enabled()):
        return {"enabled": False, "recovered": 0, "candidates": 0, "down": 0, "relaunched": 0,
                "skipped_cooldown": 0, "skipped_healthy": 0, "skipped_ambiguous": 0,
                "skipped_no_executor": 0, "errors": 0}

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
                  trading_account__is_demo=True, trading_account__workspace_confirmed_at__isnull=False)
          .exclude(trading_account_id__in=_RESERVED_ACCOUNT_IDS)
          .select_related("trading_account", "execution_node")
          .iterator())

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

    return {"enabled": True, "recovered": recovered, "candidates": candidates, "down": down,
            "relaunched": relaunched, "skipped_cooldown": skipped_cooldown, "skipped_healthy": skipped_healthy,
            "skipped_ambiguous": skipped_ambiguous, "skipped_no_executor": skipped_no_executor, "errors": errors}

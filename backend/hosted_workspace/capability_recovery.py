"""hosted_workspace.capability_recovery — AJ#6.3 Shape-3 post-login MT5 automation-capability recovery (DARK).

THE PROBLEM (AJ#6.2, proven on the host): MetaTrader disables algo trading on the broker-login account-change
("automated trading is disabled because the account has been changed"), reverting config\\common.ini [Experts]
Enabled=1 -> 0. So a hosted workspace can be CONNECTED + the expected account matched, yet stuck at
trade_allowed=False — never reaching EXECUTION_READY.

THE FIX (capability, NOT consent): for such a stuck workspace, re-assert the certified automation config
(AllowLiveTrading=1 / Enabled=1) AFTER login, then gracefully RELAUNCH the tenant's OWN terminal (via the
signed RELAUNCH_TERMINAL primitive) so MT5 re-reads Enabled=1 with the account already connected (no
account-change event on relaunch). The observer then re-proves the SAME broker identity + trade_allowed=True on
the next cycle, and canonical state may naturally become EXECUTION_READY.

SAFETY (the load-bearing invariants):
  * DARK: a no-op unless BOTH hosted_persistent_mt5_enabled() AND hosted_capability_recovery_enabled().
  * Customer Zero is excluded from the candidate set AND refused again by the signed executor + the .ps1.
  * Capability ONLY: it re-writes common.ini and relaunches the tenant's own MT5. It NEVER logs in, changes the
    broker account, ARMS execution (execution_enabled stays whatever it was — arming needs the ADR-0047
    customer authorization), or authorises/places an order (the live order-time bridge gate remains authority).
  * LOOP-SAFE / BOUNDED: each attempt is CLAIMED atomically (stamp capability_recovery_at + increment
    capability_recovery_count under select_for_update) BEFORE any host call, so a hung/failed host call still
    backs off. At most MAX_RECOVERY_ATTEMPTS per workspace, each behind a RECOVERY_COOLDOWN_S cooldown; once the
    observer proves trade_allowed=True the workspace leaves the stuck state and is no longer a candidate — MT5
    is never repeatedly restarted once capability is recovered.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from execution.readiness import _observation_fresh
from hosted_workspace.flags import hosted_capability_recovery_enabled, hosted_persistent_mt5_enabled
from hosted_workspace.models import HostedMt5Workspace
from hosted_workspace.slot_preparation import resolve_host_executor
from hosted_workspace.state_machine import WorkspaceLifecycleState as S

logger = logging.getLogger("guvfx.hosted_workspace")

SOURCE = "hosted_workspace.capability_recovery"

# Bounds (module constants — deliberately conservative). At most 3 recovery cycles per workspace, each behind a
# 5-minute cooldown, so MT5 is restarted at most 3 times over ~15 min for a persistently-stuck slot, then the
# runner stops attempting (fail-safe: the slot stays at CONNECTED and is surfaced, never restart-looped).
MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_COOLDOWN_S = 300

# Customer Zero — never a candidate here (defence in depth; the signed executor + the .ps1 also refuse it).
_RESERVED_ACCOUNT_IDS = frozenset({1})


def _ok(res) -> bool:
    return bool(res) and bool(res.get("ok"))


def _attempt_allowed(ws, now) -> bool:
    """Loop-safety predicate: under the per-workspace cap AND past the cooldown since the last attempt."""
    if (ws.capability_recovery_count or 0) >= MAX_RECOVERY_ATTEMPTS:
        return False
    last = ws.capability_recovery_at
    if last is not None and (now - last).total_seconds() < RECOVERY_COOLDOWN_S:
        return False
    return True


def _claim_attempt(pk, now) -> bool:
    """Atomically CLAIM one recovery attempt for the workspace: re-verify (under a row lock) it is still the
    stuck CONNECTED+matched+trade_allowed=False state and still within the bound/cooldown, then stamp the
    attempt (timestamp + count) BEFORE any host call. Returns False if it was concurrently advanced/consumed."""
    with transaction.atomic():
        ws = HostedMt5Workspace.objects.select_for_update().get(pk=pk)
        if (str(ws.canonical_state) != str(S.CONNECTED)
                or ws.proj_connected is not True
                or ws.proj_account_match is not True
                or ws.proj_trade_allowed is not False
                or not _attempt_allowed(ws, now)):
            return False
        ws.capability_recovery_at = now
        ws.capability_recovery_count = (ws.capability_recovery_count or 0) + 1
        ws.save(update_fields=["capability_recovery_at", "capability_recovery_count", "updated_at"])
        return True


def _safe_call(ex, method_name, *args, **kwargs) -> dict:
    """Invoke one host step; a missing method or any exception is a sanitised failure (never leaks/raises)."""
    fn = getattr(ex, method_name, None)
    if fn is None:
        return {"ok": False, "reason": "executor_incomplete"}
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 — host errors are sanitised, never propagated or logged verbatim
        logger.warning("capability_recovery: host step %s errored", method_name)
        return {"ok": False, "reason": "host_error"}


def run_hosted_capability_recovery(*, actor: str = SOURCE, executor_resolver=None) -> dict:
    """One capability-recovery pass over every stuck workspace. DARK unless master + recovery flags on.
    Idempotent + loop-safe + fail-open per workspace. Returns a secret-free summary. Arms nothing; places no
    order. Runs AFTER the observation pass (so it sees the current CONNECTED+matched+trade_allowed=False state)
    and does NOT itself advance canonical state — the observer re-proves trade_allowed on the next cycle."""
    if not (hosted_persistent_mt5_enabled() and hosted_capability_recovery_enabled()):
        return {"enabled": False, "candidates": 0, "attempted": 0, "config_reasserted": 0, "relaunched": 0,
                "skipped_cooldown": 0, "skipped_not_ready": 0, "skipped_no_executor": 0, "errors": 0}

    now = timezone.now()
    resolve = executor_resolver or (lambda account_id, rdp_host: resolve_host_executor(
        account_id=account_id, rdp_host=rdp_host))

    candidates = attempted = config_reasserted = relaunched = 0
    skipped_cooldown = skipped_not_ready = skipped_no_executor = errors = 0

    qs = (HostedMt5Workspace.objects
          .filter(canonical_state=str(S.CONNECTED), proj_connected=True, proj_account_match=True,
                  proj_trade_allowed=False)
          .exclude(trading_account_id__in=_RESERVED_ACCOUNT_IDS)
          .select_related("trading_account", "execution_node")
          .iterator())
    for ws in qs:
        candidates += 1
        account = getattr(ws, "trading_account", None)
        if account is None:
            errors += 1
            continue
        # Demo-only wall + freshness (a stale projection is not acted on — the observer must be current).
        if getattr(account, "is_demo", False) is not True or not _observation_fresh(ws):
            skipped_not_ready += 1
            continue
        # Loop-safety pre-filter (cheap check before locking).
        if not _attempt_allowed(ws, now):
            skipped_cooldown += 1
            continue
        # Need a delivering node (rdp_host) to reach the host executor.
        node = getattr(ws, "execution_node", None) or getattr(account, "terminal_node", None)
        rdp_host = str(getattr(node, "rdp_host", "") or "").strip()
        if not rdp_host:
            skipped_not_ready += 1
            continue
        ex = resolve(account.id, rdp_host)
        if ex is None:                                 # DARK / unarmed host executor — burn no attempt
            skipped_no_executor += 1
            continue
        # Server-derived identity + paths (DB-only, idempotent).
        from terminal_provisioning import services as prov_services
        try:
            prov = prov_services.provision(account, actor=None)
        except Exception:  # noqa: BLE001
            errors += 1
            continue
        username, runtime_root = prov.windows_username, prov.runtime_root
        # CLAIM the attempt atomically (stamp + increment) BEFORE the host I/O — the loop-safety anchor.
        if not _claim_attempt(ws.pk, now):
            skipped_cooldown += 1
            continue
        attempted += 1
        # 1) Re-assert the certified automation config (AllowLiveTrading=1 / Enabled=1). Capability only.
        r1 = _safe_call(ex, "apply_autotrading_config", runtime_root, rdp_host=rdp_host)
        if not _ok(r1):
            errors += 1
            continue                                   # do NOT relaunch if the config re-assert failed
        config_reasserted += 1
        # 2) Graceful, tenant-only relaunch so MT5 re-reads Enabled=1 with the account already connected.
        r2 = _safe_call(ex, "relaunch_terminal", username, runtime_root, rdp_host=rdp_host)
        if _ok(r2):
            relaunched += 1
            logger.info("capability_recovery: reasserted config + relaunched account=%s (attempt %s)",
                        account.id, ws.capability_recovery_count)
        else:
            errors += 1

    return {"enabled": True, "candidates": candidates, "attempted": attempted,
            "config_reasserted": config_reasserted, "relaunched": relaunched,
            "skipped_cooldown": skipped_cooldown, "skipped_not_ready": skipped_not_ready,
            "skipped_no_executor": skipped_no_executor, "errors": errors}

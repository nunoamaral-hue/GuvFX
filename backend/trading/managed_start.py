"""trading.managed_start — the managed "Start Trading" lifecycle for a hosted (Provider-B) account (DARK).

Today "Start Trading" is a bare ``TradingAccount.is_active=True`` flip that checks only runtime-process
readiness — so an account can present as started while its MT5 cannot auto-trade (trade_allowed=False). This
module makes Start a truthful, fail-closed orchestration that reuses the EXISTING mechanisms:

  validate (owner / demo / runtime / connected / matched / margin / strategy / sizing)
  → product-driven promote the assignment to its approved execution template (TEST/MANUAL → LIVE/AUTO_DEMO,
     signal_source from the PRODUCT, never the client)
  → ensure automated-trading capability (existing account-scoped ``recover_capability_for_account`` when
     trade_allowed=False — NO second recovery mechanism)
  → arm (existing ``arm_hosted_workspace_execution``; only succeeds once genuinely ready)
  → commit ``is_active`` (with the existing STANDARD/CONCURRENT entitlement semantics)
  → report the TRUTHFUL state (never a false "Trading").

DARK: nothing here runs unless ``managed_start_trading_enabled()``; flag OFF ⇒ the caller keeps the legacy
flip byte-for-byte.

GOVERNANCE (ADR-0047, security rule "no unrestricted LLM live-trading authority — explicit human-gated
control path"): execution AUTHORIZATION (``execution_authorized_at``) and the onboarding CONFIRM
(``workspace_confirmed_at``) are explicit customer acts. By DEFAULT this orchestrator REQUIRES them (fail
closed with an actionable reason) and never creates them. Whether the Start click may ITSELF stand in for
those two explicit acts is a distinct, separately-gated decision (``managed_start_authorizes_execution()``,
DEFAULT OFF) — it stays dark until the Sponsor approves it (ADR). Even when on, it is only ever a DEMO,
human-initiated Start; it never places, sizes, or approves an order (the live bridge gate remains authority).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger("guvfx.trading")

_TRUTHY = ("1", "true", "yes", "on")


def _flag(name: str, default: str = "") -> bool:
    val = getattr(settings, name, None)
    if val is None:
        val = os.getenv(name, default)
    return str(val).strip().lower() in _TRUTHY


def managed_start_trading_enabled() -> bool:
    """Master gate for the managed Start-Trading lifecycle. DEFAULT OFF ⇒ the caller keeps the legacy flip."""
    return _flag("MANAGED_START_TRADING_ENABLED")


def managed_start_authorizes_execution() -> bool:
    """Whether the Start click may ITSELF perform the ADR-0047 authorization + onboarding confirm (the
    "one-click journey"). DEFAULT OFF ⇒ Start REQUIRES prior explicit confirm + authorization. Only turn on
    with an approved decision (it changes when the explicit human execution-authority gate is satisfied)."""
    return _flag("MANAGED_START_AUTHORIZES_EXECUTION")


@dataclass
class StartResult:
    """Outcome of a managed Start attempt. ``applies`` False ⇒ the caller should use the legacy path (flag off
    or not a hosted account). ``ok`` gates whether ``is_active`` was committed. ``reason``/``detail`` are stable
    + member-facing. ``trading_state`` is the authoritative post-state to return to the UI."""
    applies: bool
    ok: bool = False
    reason: str = ""
    detail: str = ""
    trading_state: dict = field(default_factory=dict)
    armed: bool = False
    recovery: dict = field(default_factory=dict)
    promoted_assignment_ids: list = field(default_factory=list)


def _fail(reason: str, detail: str) -> StartResult:
    return StartResult(applies=True, ok=False, reason=reason, detail=detail)


# Margin mode the Wayond/hedging products require (2 == HEDGING; 0 == NETTING).
_HEDGING = 2


def _marketplace_product_template(strategy):
    """Derive the PRODUCT's approved execution template from the strategy definition — never from the client.
    Returns (execution_mode, signal_source) or None when the strategy carries no product signal-source binding.
    Only ``AUTO_DEMO`` is ever returned in this DEMO programme (AUTO_LIVE is never derived here)."""
    filters = getattr(strategy, "filters", None) or {}
    source = str(filters.get("signal_source") or "").strip()
    if not source:
        return None
    return ("AUTO_DEMO", source)


def _promote_product_assignments(account, active_assignments) -> list:
    """Product-driven promotion: for each active assignment whose strategy is a MARKETPLACE product with an
    approved execution template, set stage=LIVE + execution_mode=AUTO_DEMO + signal_source=<product source>,
    server-side, idempotently, under a row lock. Returns the promoted assignment ids. A non-marketplace or
    template-less assignment is left untouched (the auto-router simply won't route it)."""
    from strategies.models import StrategyAssignment
    promoted = []
    for a in active_assignments:
        strat = a.strategy
        if not getattr(strat, "is_marketplace", False):
            continue
        tmpl = _marketplace_product_template(strat)
        if tmpl is None:
            continue
        mode, source = tmpl
        with transaction.atomic():
            locked = StrategyAssignment.objects.select_for_update().get(pk=a.pk)
            fields = []
            if locked.stage != "LIVE":
                locked.stage = "LIVE"; fields.append("stage")
            if locked.execution_mode != mode:
                locked.execution_mode = mode; fields.append("execution_mode")
            if locked.signal_source != source:
                locked.signal_source = source; fields.append("signal_source")
            if locked.is_active is not True:
                locked.is_active = True; fields.append("is_active")
            if fields:
                locked.save(update_fields=fields + ["updated_at"])
                promoted.append(locked.id)
    return promoted


class _StartAbort(Exception):
    """Internal sentinel: a GOVERNED refusal (confirm/authorize) inside the activation transaction. Raising it
    rolls the whole transaction back (nothing confirmed/authorized/activated) and is translated to a fail-closed
    StartResult by the caller. (A CONCURRENT-limit breach raises DRFValidationError instead, which likewise rolls
    the transaction back and is surfaced by the view as 409.)"""
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


def _activate_with_authorization(account, user, ws, *, authorizes, actor, request) -> None:
    """Entitlement-gated activation + (managed) confirm + authorization as ONE atomic unit under the user-row
    lock, so a CONCURRENT-limit breach or a governed refusal rolls back EVERYTHING (is_active, confirm,
    authorization). This closes the hole where ``confirm_broker_account`` (which sets is_active itself) could
    activate + authorize an account that the entitlement check would then reject — leaving a rejected account for
    the per-minute auto_arm to arm. Raises DRFValidationError (breach) or ``_StartAbort`` (governed refusal);
    both roll back. Does NO host I/O (capability_recovery/arm run AFTER, outside the lock)."""
    from trading.account_entitlement import enforcement_enabled
    from trading.models import TradingAccount
    from hosted_workspace.provisioning import confirm_broker_account, authorize_workspace_execution
    with transaction.atomic():
        # 1) Entitlement-gated activation FIRST, under the user-row lock (same semantics as the legacy path).
        if enforcement_enabled(user):
            from billing.entitlements import AccountMode, resolve_effective_entitlements
            from trading.account_entitlement import check_can_activate
            type(user).objects.select_for_update().get(pk=user.pk)
            ent = resolve_effective_entitlements(user)
            if str(getattr(ent, "account_mode", AccountMode.STANDARD)) == AccountMode.CONCURRENT:
                check_can_activate(user, exclude_account_id=account.id)   # raises DRFValidationError -> rollback
            else:  # STANDARD — deactivate siblings atomically with this activation (no transient two-active window)
                (TradingAccount.objects.filter(user=user, is_active=True, disconnected_at__isnull=True)
                 .exclude(id=account.id).update(is_active=False, updated_at=timezone.now()))
        # 2) Managed one-click confirm + authorization, in the SAME transaction (governed refusal -> rollback).
        if authorizes:
            if getattr(account, "workspace_confirmed_at", None) is None:
                cr = confirm_broker_account(user, ws, actor=actor or "managed_start", request=request)
                if not cr.ok:
                    raise _StartAbort("not_confirmed",
                                      "We couldn't confirm your broker account yet. Please try again shortly.")
                account.refresh_from_db()
            if getattr(ws, "execution_authorized_at", None) is None:
                ar = authorize_workspace_execution(user, ws, actor=actor or "managed_start", request=request,
                                                   allow_pre_ready=True)
                if not ar.ok:
                    raise _StartAbort("not_authorized",
                                      "We couldn't enable automated trading yet. Please try again shortly.")
                ws.refresh_from_db()
        # 3) Commit is_active (confirm may already have set it; ensure it + covers the flag-off path).
        account.refresh_from_db()
        if account.is_active is not True:
            account.is_active = True
            account.save(update_fields=["is_active", "updated_at"])


_RUNTIME_FAIL_MESSAGES = {
    "workspace_missing": "Your trading terminal isn't ready yet. Please try again shortly.",
    "broker_not_connected": "Open MT5 and log in to your broker account to start trading.",
    "account_mismatch": "The terminal is logged into a different account. Log in to this broker account.",
    "observation_stale": "We're re-checking your terminal. Please try again in a moment.",
    "terminal_not_ready": "This account isn't connected to a trading terminal yet.",
}


def _runtime_ready_for_start(account, ws):
    """Provider-aware Start preflight: prove the terminal/runtime is up and the broker is connected + the RIGHT
    account is logged in — deliberately NOT ``trade_allowed`` (which is EXPECTED False before capability_recovery;
    requiring it here would make the lifecycle circular). Returns ``(ok, reason_code)``.

    For a Provider-B hosted account (``persistent_workspace``) the runtime IS the tenant's own MT5 terminal,
    tracked by the certified ``HostedMt5Workspace`` observer projection — ``AccountRuntime`` is legitimately None
    for these accounts (Accounts 25 and 35 are fully execution-ready with NO AccountRuntime), so the previous
    gate on ``_account_runtime_ready`` (AccountRuntime RUNNING+heartbeat+BETA) wrongly rejected a genuinely
    connected hosted account. The authoritative signal is the M3c projection the readiness provider and the
    truthful trading-state already use: workspace present + ``proj_connected`` + ``proj_account_match`` + a fresh
    observation. Legacy (non-Provider-B) accounts defer to the AccountRuntime readiness unchanged."""
    # A tombstoned/disconnected account is never startable — match the certified readiness provider
    # (execution/readiness.py) and the arm gate (execution/hosted_provisioning.py), which both fail closed on
    # a non-null disconnected_at. Applied to BOTH branches so a stale-but-fresh disconnected row can't pass.
    if getattr(account, "disconnected_at", None) is not None:
        return False, "broker_not_connected"
    provider = str(getattr(account, "readiness_provider", "") or "")
    if provider == "persistent_workspace" and not getattr(account, "mt5_instance_id", None):
        if ws is None:
            return False, "workspace_missing"
        if ws.proj_connected is not True:
            return False, "broker_not_connected"
        if ws.proj_account_match is not True:
            return False, "account_mismatch"
        from execution.readiness import _observation_fresh
        if not _observation_fresh(ws):
            return False, "observation_stale"
        return True, "ok"
    from trading.views import _account_runtime_ready
    return (True, "ok") if _account_runtime_ready(account) else (False, "terminal_not_ready")


def managed_start_trading(account, user, *, actor: str = "", request=None) -> StartResult:
    """Run the managed Start-Trading lifecycle for ``account``. Fail-closed: on any hard-precondition failure
    the account is NOT made active and an actionable reason is returned. Reuses capability_recovery + arm; never
    places an order. See module docstring for the governance posture."""
    if not managed_start_trading_enabled():
        return StartResult(applies=False)
    if getattr(account, "mt5_instance_id", None):
        return StartResult(applies=False)             # legacy shared-instance account — caller uses legacy path
    from trading.trading_state import resolve_trading_state
    ws = getattr(account, "hosted_workspace", None)

    # 1) DEMO-only programme.
    if getattr(account, "is_demo", False) is not True:
        return _fail("demo_only", "Automated trading is available for demo accounts in this programme.")
    # 2) runtime/broker connected + right account logged in (Provider-aware; NOT trade_allowed). This proves the
    # tenant's terminal is up and connected to the correct broker account WITHOUT gating on AccountRuntime (which
    # is legitimately None for Provider-B hosted accounts — the previous defect). trade_allowed is deliberately
    # NOT required here; it is expected False before capability_recovery (step 8).
    ok, reason = _runtime_ready_for_start(account, ws)
    if not ok:
        return _fail(reason, _RUNTIME_FAIL_MESSAGES.get(reason, "This account isn't ready to start trading yet."))
    if ws is None:  # defensive — a managed hosted Start needs a workspace for the remaining checks/arm
        return _fail("workspace_missing", _RUNTIME_FAIL_MESSAGES["workspace_missing"])
    # 3) margin mode capability (hedging required by the product). FAIL-CLOSED: an UNKNOWN/unobserved margin
    # (proj_margin_mode is None) is NOT proof of hedging, so it must block — matching the B1 canonical rule
    # that only proven HEDGING supports independent same-symbol legs (NETTING/UNKNOWN destroy magic ownership).
    try:
        margin_is_hedging = ws.proj_margin_mode is not None and int(ws.proj_margin_mode) == _HEDGING
    except (TypeError, ValueError):
        margin_is_hedging = False
    if not margin_is_hedging:
        return _fail("margin_mode", "This strategy requires a hedging account. Please use a hedging demo account.")
    # 5) at least one active, correctly-sized strategy.
    from strategies.models import StrategyAssignment, effective_lot_per_leg
    active_assignments = list(StrategyAssignment.objects.select_related("strategy")
                              .filter(account=account, is_active=True))
    if not active_assignments:
        return _fail("no_strategy", "Add a strategy to this account before starting trading.")
    for a in active_assignments:
        try:
            if effective_lot_per_leg(a) <= 0:
                return _fail("invalid_sizing", "This strategy's trade size isn't set correctly.")
        except Exception:  # noqa: BLE001
            return _fail("invalid_sizing", "This strategy's trade size isn't set correctly.")

    # 6) explicit human execution authority (ADR-0047) + onboarding confirm + entitlement-gated activation, as
    # ONE atomic unit (see _activate_with_authorization). capability_recovery is ASYNC, so at click the workspace
    # is usually CONNECTED (not yet EXECUTION_READY); the managed one-click flow (Sponsor-approved) records
    # confirm + authorization NOW (authorize with allow_pre_ready) so the per-minute observation cron completes
    # the arm (auto_arm) once EXECUTION_READY. is_active alone never authorizes an order — the claim/bridge gates
    # still require the full readiness — so committing while capability is being restored is honest, not a false
    # "Trading" (the read-model reports PREPARING until genuinely ready). Default (flag off): confirm +
    # authorization must ALREADY exist (checked read-only BEFORE any mutation).
    authorizes = managed_start_authorizes_execution()
    if not authorizes:
        if getattr(account, "workspace_confirmed_at", None) is None:
            return _fail("not_confirmed", "Please confirm this is your broker account before starting trading.")
        if getattr(ws, "execution_authorized_at", None) is None:
            return _fail("not_authorized", "Please enable automated trading for this account first.")
    try:
        _activate_with_authorization(account, user, ws, authorizes=authorizes, actor=actor, request=request)
    except _StartAbort as exc:
        return _fail(exc.reason, exc.detail)   # governed refusal — nothing activated/confirmed/authorized

    # 7) ensure automated-trading capability (Algo). Account-scoped; reuses the existing recovery mechanism.
    recovery = {}
    ws.refresh_from_db()
    if ws.proj_trade_allowed is not True:
        from hosted_workspace.capability_recovery import recover_capability_for_account
        recovery = recover_capability_for_account(account.id, actor=actor or "managed_start",
                                                  bypass_onboarding_gate=True)
        ws.refresh_from_db()

    # 9) product-driven promotion (server-derived; safe modes only) — after commit so it never runs on a
    # concurrency-rejected Start.
    promoted = _promote_product_assignments(account, active_assignments)

    # 10) arm (existing gate; only succeeds when genuinely ready — is_active + trade_allowed + EXECUTION_READY +
    # authorized). Runs AFTER commit so a genuinely-ready fresh Start arms synchronously and reads TRADING.
    account.refresh_from_db()
    from execution.hosted_provisioning import arm_hosted_workspace_execution
    arm = arm_hosted_workspace_execution(account, actor=actor or "managed_start", request=request)

    account.refresh_from_db()
    state = resolve_trading_state(account)
    ready = state.get("state") == "TRADING"
    return StartResult(applies=True, ok=True, reason="started" if ready else "preparing",
                       detail=state.get("detail", ""), trading_state=state, armed=bool(arm.ok),
                       recovery=recovery, promoted_assignment_ids=promoted)

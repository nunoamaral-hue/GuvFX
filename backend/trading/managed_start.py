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


def _commit_active(account, user) -> None:
    """Flip is_active=True with the EXISTING STANDARD/CONCURRENT entitlement semantics (reuses the same
    enforcement helpers as the legacy set_active path — no new concurrency rule)."""
    from trading.account_entitlement import enforcement_enabled
    if not enforcement_enabled(user):
        account.is_active = True
        account.save(update_fields=["is_active", "updated_at"])
        return
    from billing.entitlements import AccountMode, resolve_effective_entitlements
    from trading.account_entitlement import check_can_activate
    from trading.models import TradingAccount
    with transaction.atomic():
        type(user).objects.select_for_update().get(pk=user.pk)
        ent = resolve_effective_entitlements(user)
        if str(getattr(ent, "account_mode", AccountMode.STANDARD)) == AccountMode.CONCURRENT:
            check_can_activate(user, exclude_account_id=account.id)   # raises DRF ValidationError on breach
        else:
            (TradingAccount.objects.filter(user=user, is_active=True, disconnected_at__isnull=True)
             .exclude(id=account.id).update(is_active=False, updated_at=timezone.now()))
        account.is_active = True
        account.save(update_fields=["is_active", "updated_at"])


def managed_start_trading(account, user, *, actor: str = "", request=None) -> StartResult:
    """Run the managed Start-Trading lifecycle for ``account``. Fail-closed: on any hard-precondition failure
    the account is NOT made active and an actionable reason is returned. Reuses capability_recovery + arm; never
    places an order. See module docstring for the governance posture."""
    if not managed_start_trading_enabled():
        return StartResult(applies=False)
    if getattr(account, "mt5_instance_id", None):
        return StartResult(applies=False)             # legacy shared-instance account — caller uses legacy path
    from trading.trading_state import resolve_trading_state

    # 1) runtime process ready (existing broker-independent readiness).
    from trading.views import _account_runtime_ready
    if not _account_runtime_ready(account):
        return _fail("terminal_not_ready",
                     "This account isn't connected to a trading terminal yet.")
    # 2) DEMO-only programme.
    if getattr(account, "is_demo", False) is not True:
        return _fail("demo_only", "Automated trading is available for demo accounts in this programme.")
    ws = getattr(account, "hosted_workspace", None)
    if ws is None:
        return _fail("workspace_missing", "Your trading terminal isn't ready yet. Please try again shortly.")
    # 3) broker connected + right account logged in.
    if ws.proj_connected is not True:
        return _fail("broker_not_connected", "Open MT5 and log in to your broker account to start trading.")
    if ws.proj_account_match is not True:
        return _fail("account_mismatch",
                     "The terminal is logged into a different account. Log in to this broker account.")
    # 4) margin mode capability (hedging required by the product). FAIL-CLOSED: an UNKNOWN/unobserved margin
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

    # 6) explicit human execution authority (ADR-0047) + onboarding confirm. By default REQUIRED; only when the
    # separately-gated decision is on does the Start click itself stand in for them (DEMO, human-initiated).
    if managed_start_authorizes_execution():
        _fields = []
        if getattr(account, "workspace_confirmed_at", None) is None:
            account.workspace_confirmed_at = timezone.now(); _fields.append("workspace_confirmed_at")
        if _fields:
            account.save(update_fields=_fields + ["updated_at"])
        if getattr(ws, "execution_authorized_at", None) is None:
            from hosted_workspace.provisioning import authorize_workspace_execution
            authorize_workspace_execution(user, ws, actor=actor or "managed_start", request=request)
            ws.refresh_from_db(fields=["execution_authorized_at"])
    else:
        if getattr(account, "workspace_confirmed_at", None) is None:
            return _fail("not_confirmed", "Please confirm this is your broker account before starting trading.")
        if getattr(ws, "execution_authorized_at", None) is None:
            return _fail("not_authorized", "Please enable automated trading for this account first.")

    # 7) COMMIT is_active (intent) with the existing STANDARD/CONCURRENT entitlement semantics — FIRST, so a
    # concurrency-limit breach fails closed here (before any promotion) and so the subsequent arm sees
    # is_active=True (arm_preconditions requires it). is_active alone never authorizes an order — the
    # claim/bridge gates still require the full readiness — so committing while capability is still being
    # restored is honest, not a false "Trading" (the read-model reports PREPARING until genuinely ready).
    # Ordering (commit → promote → arm) also makes promotion atomic w.r.t. a successful activation: a Start
    # that fails the concurrency check never promotes an assignment to LIVE/AUTO_DEMO.
    _commit_active(account, user)

    # 8) ensure automated-trading capability (Algo). Account-scoped; reuses the existing recovery mechanism.
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

"""trading.trading_state — the ONE authoritative customer-facing trading-state projection.

WHY (Objective E / the Pepperstone inconsistency): the Broker Accounts card used to show "Trading"
whenever ``TradingAccount.is_active`` was True. But ``is_active`` only means "the customer pressed Start /
this account is ELIGIBLE for automated dispatch"; it is NOT proof that the hosted MT5 can actually
auto-trade. An account can sit at ``is_active=True`` while its workspace is ``canonical_state=CONNECTED``
with ``proj_trade_allowed=False`` (the AJ#6.2 post-login algo-off state) — so the card lied.

This module derives a SINGLE truthful state from the SAME authority the order-time gate uses
(``execution.readiness.PersistentWorkspaceProvider.evaluate`` — the full 14-condition conjunction), so the
UI can never claim "Trading" where the server would refuse an order. It is a PURE READ: it never mutates,
never contacts the host, and never arms. The frontend renders ``label``/``detail`` verbatim (the PX-7A
"Trading is never computed in the frontend" precedent) — no readiness logic is duplicated client-side.

Customer-facing states (stable ``state`` codes; ``label``/``detail`` are member language):
  BROKER_LOGIN_REQUIRED  — the terminal/broker isn't connected or the wrong account is logged in.
  BROKER_CONNECTED       — connected + matched, stopped, no active strategy yet.
  TRADING_STOPPED        — connected, has an active strategy, but the customer has Stopped trading.
  PREPARING              — the customer pressed Start and GuvFX is still establishing automated-trading
                           capability (restoring MT5 algo / finishing arm) — not yet order-capable.
  TRADING                — genuinely execution-ready: an order dispatched now would be accepted.
  ATTENTION              — a blocking condition the customer must resolve (e.g. a real account where only
                           demo is supported), or a persistent failure.
"""
from __future__ import annotations

# Stable state codes (contract with the frontend).
BROKER_LOGIN_REQUIRED = "BROKER_LOGIN_REQUIRED"
BROKER_CONNECTED = "BROKER_CONNECTED"
TRADING_STOPPED = "TRADING_STOPPED"
PREPARING = "PREPARING"
TRADING = "TRADING"
ATTENTION = "ATTENTION"

_LABELS = {
    BROKER_LOGIN_REQUIRED: "Broker login required",
    BROKER_CONNECTED: "Broker connected",
    TRADING_STOPPED: "Trading stopped",
    PREPARING: "Preparing automated trading",
    TRADING: "Trading",
    ATTENTION: "Action required",
}


def _has_active_strategy(account) -> bool:
    """True when the account has at least one ACTIVE StrategyAssignment (prefetch-aware, no N+1 when the
    viewset prefetched ``strategy_assignments``)."""
    cache = getattr(account, "_prefetched_objects_cache", {}) or {}
    if "strategy_assignments" in cache:
        return any(getattr(a, "is_active", False) for a in account.strategy_assignments.all())
    try:
        return account.strategy_assignments.filter(is_active=True).exists()
    except Exception:  # noqa: BLE001 — a read-model must never raise into the serializer
        return False


def _recovery_in_progress(ws) -> bool:
    """True when a bounded capability-recovery attempt has been claimed for this workspace but the observer
    has not yet re-proved trade_allowed — i.e. GuvFX is actively restoring automated-trading capability."""
    return bool(getattr(ws, "capability_recovery_count", 0)) and getattr(ws, "proj_trade_allowed", None) is not True


def resolve_trading_state(account) -> dict:
    """Return ``{"state", "label", "detail"}`` for ``account`` — the authoritative customer-facing trading
    state. Pure read; never raises. Legacy (shared-instance / non-persistent) accounts keep the simple
    is_active mapping so their display is unchanged."""
    is_active = bool(getattr(account, "is_active", False))
    provider = str(getattr(account, "readiness_provider", "") or "")

    # Legacy / non-hosted accounts: preserve the pre-existing is_active-derived display exactly.
    if provider != "persistent_workspace" or getattr(account, "mt5_instance_id", None):
        code = TRADING if is_active else TRADING_STOPPED
        return {"state": code, "label": _LABELS[code], "detail": ""}

    ws = getattr(account, "hosted_workspace", None)

    # TRADING is decided by the SAME authorities the order path uses, so the UI can never claim "Trading"
    # where the server would refuse an order: the creation-gate readiness conjunction AND the final-dispatch
    # gate (broker health/pause). evaluate_dispatch_gate is TRANSPARENT (allowed=True) while its flag is off,
    # so this reduces to the readiness provider in the current posture and additionally honours health/pause
    # once that gate is armed. Any error fails closed to not-eligible (never a false "Trading").
    try:
        from execution.readiness import PersistentWorkspaceProvider
        eligible = bool(PersistentWorkspaceProvider().evaluate(account).eligible)
        if eligible:
            from execution.broker_gate import evaluate_dispatch_gate
            eligible = bool(evaluate_dispatch_gate(account).allowed)
    except Exception:  # noqa: BLE001
        eligible = False

    if is_active and eligible:
        return {"state": TRADING, "label": _LABELS[TRADING],
                "detail": "Your account is trading automatically."}

    connected = bool(getattr(ws, "proj_connected", False)) if ws is not None else False
    matched = bool(getattr(ws, "proj_account_match", False)) if ws is not None else False
    margin = getattr(ws, "proj_margin_mode", None) if ws is not None else None
    is_demo = getattr(account, "is_demo", False) is True

    if is_active:
        # Started, but not order-capable. The readiness reason CANNOT be used here (evaluate short-circuits at
        # execution_enabled, condition 8, before the connected/matched checks), so read the projection directly.
        if not is_demo:
            return {"state": ATTENTION, "label": _LABELS[ATTENTION],
                    "detail": "Automated trading is available for demo accounts in this programme."}
        if ws is None or not connected:
            return {"state": BROKER_LOGIN_REQUIRED, "label": _LABELS[BROKER_LOGIN_REQUIRED],
                    "detail": "Open MT5 and log in to your broker account to start trading."}
        if not matched:
            return {"state": ATTENTION, "label": _LABELS[ATTENTION],
                    "detail": "The terminal is logged into a different account. Log in to this broker account."}
        if margin is not None and int(margin) != 2:
            return {"state": ATTENTION, "label": _LABELS[ATTENTION],
                    "detail": "This strategy requires a hedging account."}
        # Connected + matched + demo + hedging, but capability/arm still being established (trade_allowed
        # restoring, or not yet armed/authorized/confirmed, or a stale observation): PREPARING, not trading.
        return {"state": PREPARING, "label": _LABELS[PREPARING],
                "detail": ("Restoring automated-trading capability…" if (ws is not None and _recovery_in_progress(ws))
                           else "Getting your account ready to trade automatically…")}

    # Stopped (is_active=False).
    if ws is not None and connected and matched:
        if _has_active_strategy(account):
            return {"state": TRADING_STOPPED, "label": _LABELS[TRADING_STOPPED],
                    "detail": "Automated trading is stopped. Press Start trading to resume."}
        return {"state": BROKER_CONNECTED, "label": _LABELS[BROKER_CONNECTED],
                "detail": "Add a strategy, then press Start trading."}
    # Stopped and not connected/matched.
    return {"state": BROKER_LOGIN_REQUIRED, "label": _LABELS[BROKER_LOGIN_REQUIRED],
            "detail": "Open MT5 and log in to your broker account."}

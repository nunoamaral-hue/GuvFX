"""Operational status summary — a single READ-ONLY aggregate for the /operations status page.

Pulls together the existing reliability signals (ComponentHealth, Heartbeat, AlertEvent) with the
business/operational picture (armed strategies, source-aware activity metrics, open positions/plans/
candidates, dispatch state, broker account metrics). It NEVER places an order, mutates a plan/trade/
strategy, or changes any control state — it only reads. Broker metrics are best-effort (a short,
fail-safe bridge order_check); everything else is a plain DB read.
"""
from __future__ import annotations

import json
import os
import urllib.request
from decimal import Decimal

from django.utils import timezone

from intelligence.display_labels import source_display_label

# Health rollup ordering (worst wins).
_RANK = {"CRITICAL": 3, "WARNING": 2, "HEALTHY": 1, "DISABLED": 0, "UNKNOWN": 0}
# Heartbeat sources that are operationally CRITICAL (stale → critical, not just warning).
_CRITICAL_HEARTBEATS = {"monitor_chain", "ingest_worker"}
# How many "expected intervals" late before a heartbeat is stale / critical.
_STALE_FACTOR = 2.0
_CRITICAL_FACTOR = 4.0


def _age_s(dt, now):
    return None if dt is None else max(0.0, (now - dt).total_seconds())


def _hb_state(age_s, interval_s, critical):
    if age_s is None:
        return "UNKNOWN"
    if age_s > interval_s * _CRITICAL_FACTOR:
        return "CRITICAL" if critical else "WARNING"
    if age_s > interval_s * _STALE_FACTOR:
        return "WARNING"
    return "HEALTHY"


def _today_start(now):
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _broker_metrics(account):
    """Best-effort live account metrics via the bridge order_check (NO order placed). Fail-safe:
    any error → ``{"reachable": False}`` so the summary never breaks on a bridge hiccup."""
    base = (os.getenv("GUVFX_WINDOWS_AGENT_BASE_URL") or os.getenv("GUVFX_AGENT_URL")
            or os.getenv("WINDOWS_AGENT_BASE") or "").rstrip("/")
    token = (os.getenv("WINDOWS_AGENT_TOKEN") or os.getenv("GUVFX_WINDOWS_AGENT_TOKEN")
             or os.getenv("GUVFX_AGENT_TOKEN") or "").strip().strip('"')
    if not base:
        return {"reachable": False, "reason": "no_bridge_configured"}
    uname = None
    if getattr(account, "mt5_instance_id", None):
        uname = getattr(account.mt5_instance, "windows_username", None)
    body = {"username": uname, "symbol": "XAUUSD", "side": "BUY", "lots": 0.01,
            "sl_price": "1000", "tp_price": "10000", "max_lot": 0.01,
            "signal_source": "__ops_probe__", "execution_mode": "SHADOW", "comment": "OPSPROBE"}
    try:
        req = urllib.request.Request(
            f"{base}/mt5/order_check", data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"X-GuvFX-Agent-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            resp = json.loads((r.read() or b"{}").decode("utf-8") or "{}")
        return {
            "reachable": True,
            "balance": resp.get("balance"),
            "equity": resp.get("equity"),
            "free_margin": resp.get("free_margin"),
            "margin_level": resp.get("margin_level"),
        }
    except Exception as exc:  # fail-safe — the summary must never break on the bridge
        return {"reachable": False, "reason": type(exc).__name__}


def _strategy_metrics(now):
    """Source-aware activity metrics (today) for each armed/known source. Never combined."""
    from execution.models import (
        SignalExecutionPlan, TradeOutcomeRecord, NotificationCandidate, SignalSourceConfig,
    )
    from strategies.models import StrategyAssignment
    from trading.models import Trade

    tstart = _today_start(now)
    armed = {a.signal_source: a for a in StrategyAssignment.objects.filter(is_active=True)
             if a.signal_source}
    cfgs = {c.source: c for c in SignalSourceConfig.objects.all()}
    sources = sorted(set(armed) | set(cfgs) | {"wayond", "ti_signals"})
    out = []
    for src in sources:
        plans_today = SignalExecutionPlan.objects.filter(source=src, created_at__gte=tstart)
        rejected = plans_today.filter(status__in=("VOIDED", "HELD")).count()
        accepted = plans_today.filter(
            status__in=("PLANNED", "PROMOTED", "CLOSED")).count()
        outcomes = TradeOutcomeRecord.objects.filter(signal_source=src, created_at__gte=tstart)
        pnl = sum((o.net_pnl for o in outcomes), Decimal("0"))
        wins = outcomes.filter(outcome="WIN").count()
        losses = outcomes.filter(outcome="LOSS").count()
        bes = outcomes.filter(outcome="BREAKEVEN").count()
        cards = NotificationCandidate.objects.filter(
            signal_source=src, status="SENT", created_at__gte=tstart).count()
        last_plan = SignalExecutionPlan.objects.filter(source=src).order_by("-created_at").first()
        cfg = cfgs.get(src)
        out.append({
            "key": src,
            "source_label": source_display_label(src),
            "strategy": (getattr(armed.get(src), "strategy", None).name
                         if armed.get(src) and armed[src].strategy_id else source_display_label(src)),
            "armed": bool(armed.get(src)) and bool(cfg and cfg.auto_demo_execution_enabled),
            "signals_today": plans_today.count(),
            "accepted": accepted,
            "rejected": rejected,
            "wins": wins, "losses": losses, "breakevens": bes,
            "realised_pnl": str(pnl),
            "cards_sent": cards,
            "last_signal_at": last_plan.created_at.isoformat() if last_plan else None,
            "per_leg_lot": str(cfg.max_lot_per_leg) if cfg else None,
        })
    return out


def build_operations_summary() -> dict:
    """The full read-only operational summary for the status page (and alert enrichment)."""
    from reliability.models import ComponentHealth, Heartbeat, AlertEvent
    from execution.models import (
        ExecutionControl, SignalExecutionPlan, NotificationCandidate, NotificationDelivery,
    )
    from trading.models import Trade, TradingAccount

    now = timezone.now()
    states = ["HEALTHY"]

    ec = ExecutionControl.objects.first()
    control = {
        "auto_execution": bool(getattr(ec, "auto_execution_enabled", False)),
        "mode": getattr(ec, "signal_execution_mode", "UNKNOWN"),
        "kill_switch": bool(getattr(ec, "kill_switch_engaged", False)),
    }

    heartbeats = []
    for hb in Heartbeat.objects.all().order_by("source"):
        age = _age_s(hb.last_beat_at, now)
        st = _hb_state(age, hb.expected_interval_s, hb.source in _CRITICAL_HEARTBEATS)
        states.append(st)
        heartbeats.append({"source": hb.source, "age_s": None if age is None else round(age),
                           "interval_s": hb.expected_interval_s, "state": st})

    components = []
    for c in ComponentHealth.objects.all().order_by("component"):
        components.append({"component": c.component, "status": c.status,
                           "since": c.since.isoformat() if c.since else None,
                           "consecutive_failures": c.consecutive_failures})
        if c.status in ("FAILED",):
            states.append("CRITICAL")
        elif c.status in ("STALE", "DEGRADED"):
            states.append("WARNING")

    strategies = _strategy_metrics(now)

    open_positions = Trade.objects.filter(close_time__isnull=True).count()
    promoted = SignalExecutionPlan.objects.filter(status="PROMOTED").count()
    pending_cand = NotificationCandidate.objects.filter(status__in=("PENDING", "PROCESSING")).count()
    failed_cand = NotificationCandidate.objects.filter(status="FAILED").count()
    if failed_cand:
        states.append("WARNING")

    last_delivery = (NotificationDelivery.objects.filter(transmitted=True)
                     .order_by("-created_at").first())
    dispatch = {
        "enabled": os.getenv("NOTIFICATION_DISPATCH_ENABLED", "").strip().lower()
        in ("1", "true", "yes", "on"),
        "transport": os.getenv("NOTIFICATION_DISPATCH_TRANSPORT", "").strip() or "dry-run",
        "last_delivery_at": last_delivery.created_at.isoformat() if last_delivery else None,
    }

    # Primary account = the one the armed AUTO_DEMO strategies run on (else the first active demo).
    acct = None
    try:
        from strategies.models import StrategyAssignment
        asn = (StrategyAssignment.objects.filter(is_active=True, account__isnull=False)
               .select_related("account").order_by("account_id").first())
        acct = asn.account if asn else None
    except Exception:
        acct = None
    if acct is None:
        acct = TradingAccount.objects.filter(is_active=True, is_demo=True).order_by("id").first()
    broker = {"account": acct.public_label() if acct else None}
    broker.update(_broker_metrics(acct) if acct else {"reachable": False})
    if broker.get("reachable") is False:
        states.append("WARNING")

    alerts = []
    for a in AlertEvent.objects.filter(status=AlertEvent.Status.OPEN).order_by("-created_at")[:25]:
        sev = a.severity
        alerts.append({
            "severity": sev, "component": a.component,
            "detail": (a.title or a.body or "")[:200],
            "first_seen": a.created_at.isoformat() if a.created_at else None,
            "acknowledged": a.acknowledged_at is not None,
            "dedup_key": a.dedup_key,
        })
        states.append("CRITICAL" if sev == AlertEvent.Severity.CRITICAL else "WARNING")

    overall = max(states, key=lambda s: _RANK.get(s, 0))
    return {
        "generated_at": now.isoformat(),
        "overall": overall,
        "control": control,
        "components": components,
        "heartbeats": heartbeats,
        "strategies": strategies,
        "positions": {"open": open_positions, "promoted_plans": promoted,
                      "pending_candidates": pending_cand, "failed_candidates": failed_cand},
        "dispatch": dispatch,
        "broker": broker,
        "alerts": alerts,
    }

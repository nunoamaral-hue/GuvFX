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
    """Source-aware activity metrics (today) for each armed/known source. Never combined (D1)."""
    from django.db.models import Count
    from execution.models import (
        SignalExecutionPlan, TradeOutcomeRecord, NotificationCandidate, NotificationDelivery,
        SignalSourceConfig, PromotionAuditEvent,
    )
    from strategies.models import StrategyAssignment

    tstart = _today_start(now)
    armed = {a.signal_source: a for a in StrategyAssignment.objects.select_related("strategy")
             .filter(is_active=True) if a.signal_source}
    cfgs = {c.source: c for c in SignalSourceConfig.objects.all()}
    sources = sorted(set(armed) | set(cfgs) | {"wayond", "ti_signals"})
    out = []
    for src in sources:
        asn = armed.get(src)
        cfg = cfgs.get(src)
        plans_today = SignalExecutionPlan.objects.filter(source=src, created_at__gte=tstart)
        rejected_qs = plans_today.filter(status__in=("VOIDED", "HELD"))
        accepted = plans_today.filter(status__in=("PLANNED", "PROMOTED", "CLOSED")).count()
        promoted_today = plans_today.filter(status__in=("PROMOTED", "CLOSED")).count()
        outcomes = TradeOutcomeRecord.objects.filter(signal_source=src, created_at__gte=tstart)
        pnl = sum((o.net_pnl for o in outcomes), Decimal("0"))
        # rejection reasons breakdown (D1 + WS-C visibility). Includes plan-stage HELD/VOIDED
        # hold_reasons AND promotion-stage rejections (e.g. account_exposure_exceeded) which leave the
        # plan PLANNED with no hold_reason — their durable reason lives in a PromotionAuditEvent.
        reasons = dict(rejected_qs.exclude(hold_reason="").values_list("hold_reason")
                       .annotate(n=Count("id")).values_list("hold_reason", "n"))
        for d in PromotionAuditEvent.objects.filter(
                plan__source=src, plan__created_at__gte=tstart,
                event="PROMOTION_REJECTED").values_list("detail", flat=True):
            code = (d or {}).get("code", "promotion_rejected")
            reasons[code] = reasons.get(code, 0) + 1
        last_plan = SignalExecutionPlan.objects.filter(source=src).order_by("-created_at").first()
        last_promoted = (SignalExecutionPlan.objects.filter(source=src, status__in=("PROMOTED", "CLOSED"))
                         .order_by("-signal_timestamp").first())
        last_delivery = (NotificationDelivery.objects.filter(candidate__signal_source=src, transmitted=True)
                         .order_by("-created_at").first())
        cards_delivered = NotificationDelivery.objects.filter(
            candidate__signal_source=src, transmitted=True, created_at__gte=tstart).count()
        daily_cap = None if (cfg is None or cfg.daily_group_cap == 0) else cfg.daily_group_cap
        out.append({
            "key": src,
            "source_label": source_display_label(src),
            "strategy": (asn.strategy.name if asn and asn.strategy_id else source_display_label(src)),
            # split the old conflated `armed` into the two independent gates (D1):
            "provider_enabled": bool(cfg and cfg.auto_demo_execution_enabled),
            "assignment_active": bool(asn),
            "assignment_id": asn.id if asn else None,
            "mode": (asn.execution_mode if asn else None),
            "armed": bool(asn) and bool(cfg and cfg.auto_demo_execution_enabled),  # kept for back-compat
            "per_leg_lot": str(cfg.max_lot_per_leg) if cfg else None,
            "total_lot": str(cfg.max_total_lot) if cfg else None,
            "daily_cap": "unlimited" if daily_cap is None else daily_cap,
            "signals_today": plans_today.count(),
            "accepted": accepted,
            "rejected": rejected_qs.count(),
            "plans_promoted": promoted_today,
            "trades_closed": outcomes.count(),
            "wins": outcomes.filter(outcome="WIN").count(),
            "losses": outcomes.filter(outcome="LOSS").count(),
            "breakevens": outcomes.filter(outcome="BREAKEVEN").count(),
            "realised_pnl": str(pnl),
            "cards_delivered": cards_delivered,
            "cards_sent": NotificationCandidate.objects.filter(
                signal_source=src, status="SENT", created_at__gte=tstart).count(),
            "rejection_reasons": reasons,
            "last_signal_at": last_plan.created_at.isoformat() if last_plan else None,
            "last_execution_at": (last_promoted.signal_timestamp.isoformat()
                                  if last_promoted and last_promoted.signal_timestamp else None),
            "last_notification_at": last_delivery.created_at.isoformat() if last_delivery else None,
        })
    return out


def _infra_block(now, broker, dispatch, heartbeats):
    """D2 — derived infrastructure liveness map. Producers that emit heartbeats are surfaced as-is
    elsewhere; here we DERIVE the rest, marking anything with no producer UNKNOWN (no-assumption
    rule) rather than fabricating HEALTHY. ``reliability_core_enabled`` lets the UI explain why so
    much may read UNKNOWN in prod (the supervisor is dormant by default)."""
    from reliability.constants import RELIABILITY_CORE_ENABLED
    hb = {h["source"]: h for h in heartbeats}

    def hb_state(source):
        return hb.get(source, {}).get("state", "UNKNOWN")

    # broker-registry freshness
    registry = {"status": "UNKNOWN", "reason": "no_rows"}
    try:
        from execution.models import BrokerInstrument
        latest = BrokerInstrument.objects.order_by("-synced_at").values_list("synced_at", flat=True).first()
        if latest:
            age = _age_s(latest, now)
            registry = {"status": "HEALTHY" if age < 48 * 3600 else "WARNING",
                        "age_s": round(age), "synced_at": latest.isoformat()}
    except Exception:
        registry = {"status": "UNKNOWN", "reason": "unavailable"}

    # redis (configured only if REDIS_URL set)
    redis = {"status": "UNKNOWN", "configured": bool(os.getenv("REDIS_URL"))}

    last_deliv = dispatch.get("last_delivery_at")
    return {
        "reliability_core_enabled": bool(RELIABILITY_CORE_ENABLED),
        "postgres": {"status": "HEALTHY"},  # implicit: this summary query succeeded
        "backend": {"status": "HEALTHY"},
        "ingest_worker": {"status": hb_state("ingest_worker")},
        "monitor_chain": {"status": hb_state("monitor_chain")},
        "bridge": {"status": "HEALTHY" if broker.get("reachable") else "WARNING",
                   "reachable": bool(broker.get("reachable"))},
        "broker_registry": registry,
        "telegram_transport": {"status": "HEALTHY" if last_deliv else "UNKNOWN",
                               "enabled": dispatch.get("enabled"),
                               "transport": dispatch.get("transport"),
                               "last_delivery_at": last_deliv},
        "redis": redis,
        # No heartbeat producers today → honest UNKNOWN, not fabricated HEALTHY.
        "listener": {"status": "UNKNOWN", "reason": "no_heartbeat_producer"},
        "shadow_worker": {"status": "UNKNOWN", "reason": "no_heartbeat_producer"},
    }


def _protection_block(now):
    """WS-K — incremental TP-protection visibility: per-source enablement, live protection stages of
    promoted plans, and MODIFY_POSITION job health. Read-only."""
    from django.db.models import Count
    from execution.models import ExecutionJob, ProposedOrderLeg, SignalSourceConfig
    tstart = _today_start(now)
    by_source = {c.source: bool(c.incremental_protection_enabled)
                 for c in SignalSourceConfig.objects.all()}
    mp = ExecutionJob.objects.filter(job_type="MODIFY_POSITION")
    stages = dict(
        ProposedOrderLeg.objects.filter(plan__status="PROMOTED")
        .exclude(protection_stage="INITIAL").values_list("protection_stage")
        .annotate(n=Count("id")).values_list("protection_stage", "n"))
    last = mp.filter(status="SUCCESS").order_by("-finished_at").first()
    last_result = (last.result or {}) if last else {}
    failed_today = mp.filter(status="FAILED", created_at__gte=tstart)
    # A retryable failure is a benign broker stops/freeze-band deferral (self-heals on a later sweep),
    # NOT a real protection failure — surface it separately so it doesn't read as an incident.
    deferred_today = failed_today.filter(result__retryable=True).count()
    # A SUPERSEDED breakeven is an obsolete entry-SL modify retired the moment TP2 locked in
    # (TP2-always-wins) — an intentional cancel, not a failure. Surface separately too.
    superseded_today = failed_today.filter(result__superseded_by="TP2_LOCKED").count()
    return {
        "incremental_by_source": by_source,
        "modify_jobs": {
            "pending": mp.filter(status="PENDING").count(),
            "running": mp.filter(status="RUNNING").count(),
            "success_today": mp.filter(status="SUCCESS", created_at__gte=tstart).count(),
            "failed_today": failed_today.count() - deferred_today - superseded_today,
            "deferred_today": deferred_today,
            "superseded_today": superseded_today,
        },
        "leg_stages_active": stages,
        "last_protection": {
            "plan_id": (last.payload or {}).get("plan_id") if last else None,
            "stage": (last.payload or {}).get("protection_stage") if last else None,
            "prior_sl": last_result.get("prior_sl"),
            "requested_sl": last_result.get("requested_sl"),
            "verified_sl": last_result.get("verified_sl"),
            "at": last.finished_at.isoformat() if last and last.finished_at else None,
        },
    }


def _execution_jobs_block(now):
    """Order-job health so a partial/stuck execution is visible on the dashboard. ``orphaned_running``
    is a lease-expired RUNNING PLACE_ORDER (a worker recycled mid-place) — reconciled against the
    broker by execution_health, never silently left. Read-only."""
    from execution.models import ExecutionJob
    po = ExecutionJob.objects.filter(job_type="PLACE_ORDER")
    running = po.filter(status="RUNNING")
    return {
        "place_order": {
            "pending": po.filter(status="PENDING").count(),
            "running": running.count(),
            "orphaned_running": running.filter(lease_expires_at__lt=now).count(),
            "failed_today": po.filter(status="FAILED", created_at__gte=_today_start(now)).count(),
        },
    }


def _signal_disposition_block(now):
    """GFX-PKT-TI-SIGNALS-NON-EXECUTION-INCIDENT — durable-disposition visibility. Every tradeable
    APPROVED signal must reach a plan or a durable AUTO_ROUTE_DEFERRED reason. Surfaces, per tradeable
    source over the last 24h: how many APPROVED signals planned, how many were durably deferred (with
    the reason breakdown), and how many are UNPLANNED WITHOUT a durable reason — the silent-loss count
    that must stay 0. Read-only."""
    from datetime import timedelta
    from collections import Counter
    from signal_intake.models import PendingSignalApproval, SignalAuditEvent
    from execution.models import SignalExecutionPlan, SignalSourceConfig
    from execution.execution_health import UNPLANNED_SIGNAL_ALERT_SECONDS
    tradeable = list(SignalSourceConfig.objects.filter(auto_demo_execution_enabled=True)
                     .values_list("source", flat=True))
    since = now - timedelta(hours=24)
    # Planning commits in a SEPARATE transaction a moment AFTER the approval commits, so a
    # just-approved signal legitimately has neither a plan nor a deferral for a brief window. Mirror
    # the detector's grace (UNPLANNED_SIGNAL_ALERT_SECONDS) so an in-flight signal is counted as such,
    # never as a silent loss — silent_loss_total must reflect only settled, genuinely-lost signals.
    settle_cutoff = now - timedelta(seconds=UNPLANNED_SIGNAL_ALERT_SECONDS)
    by_source = {}
    silent_total = 0
    for src in tradeable:
        base = PendingSignalApproval.objects.filter(
            source=src, status=PendingSignalApproval.Status.APPROVED, created_at__gte=since)
        total = base.count()
        # ``execution_plan`` is the OneToOne reverse relation → count planned in ONE query, then only
        # loop over the (normally ~empty) UNPLANNED set to classify deferred vs silent. No N+1 over
        # the healthy backlog.
        planned = base.filter(execution_plan__isnull=False).count()
        unplanned = base.filter(execution_plan__isnull=True)
        # In-flight: approved within the planning-settle window and not yet planned — not a loss.
        in_flight = unplanned.filter(created_at__gte=settle_cutoff).count()
        deferred = silent = 0
        reasons = Counter()
        for a in unplanned.filter(created_at__lt=settle_cutoff):
            rev = (SignalAuditEvent.objects.filter(
                approval=a, event=SignalAuditEvent.Event.AUTO_ROUTE_DEFERRED)
                .order_by("-id").first())
            if rev is not None:
                deferred += 1
                reasons[(rev.detail or {}).get("reason", "unknown")] += 1
            else:
                silent += 1
        silent_total += silent
        by_source[src] = {"approved_24h": total, "planned": planned, "in_flight": in_flight,
                          "deferred": deferred, "unplanned_no_reason": silent,
                          "deferral_reasons": dict(reasons)}
    return {"window_hours": 24, "by_source": by_source, "silent_loss_total": silent_total}


def _notification_reconciliation_block(now):
    """WS-E — exactly-once notification chain, reconciled per source over 24h:
    WIN outcomes → NotificationCandidate → SENT → NotificationDelivery(transmitted). A healthy chain
    has win == candidates == sent == transmitted, with no candidate stuck PENDING/PROCESSING and no
    duplicate delivery. ``mismatch`` flags any per-source break so the dashboard (and the monitor-chain
    alert) can surface a notification loss the moment it appears. Read-only."""
    from datetime import timedelta
    from execution.models import (TradeOutcomeRecord, NotificationCandidate, NotificationDelivery)
    since = now - timedelta(hours=24)
    # A WIN's candidate + card delivery commit a moment AFTER the outcome (async, up to ~a monitor
    # cycle later). Reconcile only the SETTLED portion of the window so an in-flight just-won trade
    # is never miscounted as a notification loss (it would flap the dashboard WARNING every close).
    settle = now - timedelta(seconds=180)
    sources = sorted(set(TradeOutcomeRecord.objects.filter(created_at__gte=since)
                         .values_list("signal_source", flat=True)) | {"ti_signals", "wayond"})
    by_source, any_mismatch = {}, False
    for src in sources:
        wins = TradeOutcomeRecord.objects.filter(
            signal_source=src, outcome="WIN", created_at__gte=since, created_at__lt=settle).count()
        cands = NotificationCandidate.objects.filter(
            signal_source=src, created_at__gte=since, created_at__lt=settle)
        sent = cands.filter(status="SENT").count()
        stuck = cands.filter(status__in=("PENDING", "PROCESSING")).count()
        failed = cands.filter(status="FAILED").count()
        dels = NotificationDelivery.objects.filter(
            candidate__signal_source=src, created_at__gte=since, created_at__lt=settle)
        transmitted = dels.filter(transmitted=True).count()
        # Exactly-once: one transmitted delivery per distinct candidate (no duplicate sends).
        distinct_deliv = dels.filter(transmitted=True).values("candidate_id").distinct().count()
        duplicates = transmitted - distinct_deliv
        mismatch = not (wins == cands.count() == sent == transmitted) or stuck or failed or duplicates
        if wins or cands.count():
            any_mismatch = any_mismatch or bool(mismatch)
            by_source[src] = {
                "win_outcomes": wins, "candidates": cands.count(), "sent": sent,
                "transmitted": transmitted, "stuck": stuck, "failed": failed,
                "duplicates": duplicates, "exactly_once": not bool(mismatch),
            }
    return {"window_hours": 24, "by_source": by_source, "any_mismatch": bool(any_mismatch)}


def _risk_state_block(now):
    """WS-F — why a valid signal may be blocked: the runtime risk gates' live state. Surfaces the
    per-account daily-drawdown position (today's realised PnL vs the configured limit, and whether the
    circuit-breaker is currently tripped) so a ``daily_drawdown_hit`` promotion-reject is explainable
    on the dashboard rather than looking like a silent loss. Read-only."""
    from execution import risk_controls as rc
    from strategies.models import StrategyAssignment
    accts = list({a.account for a in StrategyAssignment.objects.filter(
        is_active=True, account__isnull=False).select_related("account")})
    limit = rc.MAX_DAILY_DRAWDOWN_ABS
    out = []
    for acct in accts:
        try:
            pnl = rc._today_realized_pnl(acct.id)
            out.append({
                "account": acct.public_label(),
                "today_realised_pnl": str(pnl),
                "daily_drawdown_limit": str(limit),
                "drawdown_tripped": bool(pnl <= -limit),
                "exposure_lots": str(rc._open_position_lots(acct.id)
                                     + rc._active_signal_lots(acct.id)),
                "exposure_cap": str(rc.MAX_ACCOUNT_EXPOSURE_LOT),
            })
        except Exception:  # pragma: no cover - read-only best-effort
            out.append({"account": acct.public_label(), "error": "unavailable"})
    return {"accounts": out, "daily_drawdown_limit": str(limit)}


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
    for a in (AlertEvent.objects.filter(status=AlertEvent.Status.OPEN)
              .select_related("acknowledged_by").order_by("-created_at")[:25]):
        sev = a.severity
        alerts.append({
            "id": a.id,                                # D3/D4: required to acknowledge from the page
            "severity": sev, "component": a.component,
            "status": a.status,
            "title": (a.title or "")[:200],
            "detail": (a.title or a.body or "")[:200],  # kept for back-compat
            "first_seen": a.created_at.isoformat() if a.created_at else None,
            "acknowledged": a.acknowledged_at is not None,
            "acknowledged_by_username": (a.acknowledged_by.get_username()
                                         if a.acknowledged_by_id else None),
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            "dedup_key": a.dedup_key,
        })
        states.append("CRITICAL" if sev == AlertEvent.Severity.CRITICAL else "WARNING")

    infra = _infra_block(now, broker, dispatch, heartbeats)
    protection = _protection_block(now)
    signal_dispositions = _signal_disposition_block(now)
    execution_jobs = _execution_jobs_block(now)
    notification_reconciliation = _notification_reconciliation_block(now)
    if notification_reconciliation.get("any_mismatch"):
        states.append("WARNING")
    risk_state = _risk_state_block(now)

    overall = max(states, key=lambda s: _RANK.get(s, 0))
    return {
        "generated_at": now.isoformat(),
        "overall": overall,
        "control": control,
        "components": components,
        "heartbeats": heartbeats,
        "infra": infra,
        "protection": protection,
        "signal_dispositions": signal_dispositions,
        "execution_jobs": execution_jobs,
        "notification_reconciliation": notification_reconciliation,
        "risk_state": risk_state,
        "strategies": strategies,
        "positions": {"open": open_positions, "promoted_plans": promoted,
                      "pending_candidates": pending_cand, "failed_candidates": failed_cand},
        "dispatch": dispatch,
        "broker": broker,
        "alerts": alerts,
    }

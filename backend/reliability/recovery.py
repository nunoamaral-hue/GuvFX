"""
RX-2G Automated Recovery — Phase 0 (SHADOW-ONLY).

Builds the framework: MarketStateService, circuit breaker, the 7 recovery
policies, and the engine. The engine RECORDS what each policy WOULD do
(RecoveryAttempt, shadow=True) but contains NO code path that executes a live
recovery action. Live execution is intentionally absent in Phase 0.
"""
import os
from dataclasses import dataclass, field

from django.utils import timezone

from .constants import (
    Component, HealthStatus, MarketState, RecoveryOutcome, RecoveryActionType,
    MT5_TICK_STALE_SECONDS, RECOVERY_COOLDOWN_S,
    auto_recovery_frozen, policy_enabled, policy_shadow,
    circuit_threshold, circuit_window_s,
)
from .models import ComponentHealth, RecoveryAttempt, CircuitBreakerState, AlertEvent, RecoveryRecommendation


# ─── Shared market-state service (single source for ALL policies) ───
class MarketStateService:
    @staticmethod
    def current(rows=None):
        override = os.getenv("RX2G_MARKET_STATE_OVERRIDE", "").strip()
        if override in (MarketState.OPEN, MarketState.CLOSED, MarketState.UNKNOWN):
            return override
        tick_age = None
        for r in (rows or []):
            if r.component == Component.SNAPSHOT_FEED:
                tick_age = (r.detail or {}).get("last_tick_age_s")
                break
        now = timezone.now()
        wd, hr = now.weekday(), now.hour  # UTC; FX closed Fri 22:00 -> Sun 22:00
        fx_closed = (wd == 5) or (wd == 6 and hr < 22) or (wd == 4 and hr >= 22)
        if tick_age is not None and tick_age <= MT5_TICK_STALE_SECONDS:
            return MarketState.OPEN
        if fx_closed:
            return MarketState.CLOSED
        if tick_age is None:
            return MarketState.UNKNOWN
        return MarketState.OPEN


# ─── Circuit breaker ───
def get_breaker(now):
    b, _ = CircuitBreakerState.objects.get_or_create(key="global")
    b.threshold = circuit_threshold()
    b.window_s = circuit_window_s()
    if b.window_started_at is None or (now - b.window_started_at).total_seconds() > b.window_s:
        b.window_started_at = now
        b.action_count = 0  # rolling window resets; OPEN stays until MANUAL reset
    b.save()
    return b


def _trip_breaker(b, now):
    if b.state == CircuitBreakerState.State.CLOSED:
        b.state = CircuitBreakerState.State.OPEN
        b.tripped_at = now
        b.save()
        AlertEvent.objects.get_or_create(
            dedup_key="RECOVERY_CIRCUIT:global", status=AlertEvent.Status.OPEN,
            defaults={"severity": AlertEvent.Severity.CRITICAL, "component": Component.EXECUTION_PIPELINE,
                      "title": "Recovery circuit breaker tripped",
                      "body": f"{b.action_count} recovery actions in {b.window_s}s exceeded threshold {b.threshold}; auto-recovery suppressed pending manual reset."},
        )
        RecoveryRecommendation.objects.get_or_create(
            dedup_key="RECOVERY_CIRCUIT:reset", status=RecoveryRecommendation.Status.OPEN,
            defaults={"component": Component.EXECUTION_PIPELINE, "recommended_action": RecoveryRecommendation.Action.INVESTIGATE_SNAPSHOT,
                      "target_ref": "circuit:global", "rationale": "Recovery circuit breaker is OPEN — investigate the failure storm, then manually reset.",
                      "severity": "CRITICAL"},
        )


def reset_breaker():
    now = timezone.now()
    b, _ = CircuitBreakerState.objects.get_or_create(key="global")
    b.state = CircuitBreakerState.State.CLOSED
    b.action_count = 0
    b.window_started_at = now
    b.reset_at = now
    b.save()
    AlertEvent.objects.filter(dedup_key="RECOVERY_CIRCUIT:global").exclude(status=AlertEvent.Status.RESOLVED).update(
        status=AlertEvent.Status.RESOLVED, resolved_at=now)
    RecoveryRecommendation.objects.filter(dedup_key="RECOVERY_CIRCUIT:reset", status=RecoveryRecommendation.Status.OPEN).update(
        status=RecoveryRecommendation.Status.COMPLETED)
    return b


# ─── Planned-attempt descriptor ───
@dataclass
class Planned:
    policy: str
    action: str
    component: str
    scope: str
    pre_state: str
    market_sensitive: bool = False
    detail: dict = field(default_factory=dict)
    row: object = None


def _scope_key(r):
    return f"{r.component}:t{r.terminal_node_id or 0}:a{r.trading_account_id or 0}"


def collect_planned(rows):
    """Map current unhealthy ComponentHealth into the recovery actions that the
    live policies WOULD take. Pure read; returns Planned descriptors."""
    planned = []
    for r in rows:
        if r.status == HealthStatus.OK or r.status == HealthStatus.UNKNOWN:
            # UNKNOWN (e.g. bridge unreachable) handled below for bridge only
            pass
        c, s = r.component, r.status
        scope = _scope_key(r)

        # 7.1 / 7.2 MT5 logout vs disconnect (MT5_BROKER FAILED)
        if c == Component.MT5_BROKER and s == HealthStatus.FAILED:
            login = (r.detail or {}).get("login")
            if login in (None, "", 0):
                planned.append(Planned("mt5_logout", RecoveryActionType.MT5_RELOGIN, c, scope, s,
                                       market_sensitive=True, detail={"signature": "logout", "note": "would re-login pinned account; reconcile-safe"}, row=r))
            else:
                planned.append(Planned("mt5_disconnect", RecoveryActionType.MT5_RELOGIN, c, scope, s,
                                       market_sensitive=True, detail={"signature": "disconnect", "note": "passive-first then bounded re-login"}, row=r))

        # 7.3 Bridge failure (MT5_* UNKNOWN = probe unreachable). Single owner = watchdog.
        elif c == Component.MT5_TERMINAL and s == HealthStatus.UNKNOWN:
            planned.append(Planned("bridge_failure", RecoveryActionType.RESTART_BRIDGE, c, scope, s,
                                   detail={"owner": "bridge_watchdog.ps1", "note": "RX-2G observes; would NOT add a second restarter"}, row=r))

        # 7.4 Worker failure
        elif c in (Component.INGEST_WORKER, Component.VALIDATE_WORKER) and s in (HealthStatus.FAILED, HealthStatus.STALE):
            planned.append(Planned("worker_failure", RecoveryActionType.RESTART_WORKER, c, scope, s,
                                   detail={"note": "would restart only this worker if hung"}, row=r))

        # 7.5 Scheduler failure
        elif c in (Component.SCHEDULER_H1, Component.SCHEDULER_H4, Component.SCHEDULER_M5) and s in (HealthStatus.FAILED, HealthStatus.STALE):
            planned.append(Planned("scheduler_failure", RecoveryActionType.REPAIR_SCHEDULER, c, scope, s,
                                   detail={"note": "would verify/repair cron mechanism; dry-run only, never places trades"}, row=r))

        # 7.6 Orphan execution jobs (category-aware)
        elif c == Component.EXECUTION_PIPELINE and s in (HealthStatus.FAILED, HealthStatus.DEGRADED):
            cats = (r.detail or {}).get("by_category", {})
            te = cats.get("trade_exec") or []
            others = (cats.get("sync") or []) + (cats.get("validation") or []) + (cats.get("unknown") or [])
            if te:
                planned.append(Planned("orphan_jobs", RecoveryActionType.RECONCILE_JOB, c, scope, s,
                                       detail={"trade_exec_jobs": te, "note": "would RECONCILE (read broker) only; NO record correction, NO close, NO re-place; ambiguous->escalate"}, row=r))
            if others:
                planned.append(Planned("orphan_jobs", RecoveryActionType.FORCE_FAIL_JOB, c, scope + ":sync", s,
                                       detail={"jobs": others, "note": "would finalize stuck sync/validation jobs (idempotent)"}, row=r))

        # 7.7 Stale telemetry
        elif c == Component.SNAPSHOT_FEED and s in (HealthStatus.STALE, HealthStatus.FAILED):
            planned.append(Planned("stale_telemetry", RecoveryActionType.TELEMETRY_REPROBE, c, scope, s,
                                   market_sensitive=True, detail={"note": "would re-probe; never fabricates ticks; root cause deferred to MT5/bridge"}, row=r))
    return planned


def _cooldown_window(policy, now):
    cd = RECOVERY_COOLDOWN_S.get(policy, 300)
    return f"{policy}:{int(now.timestamp() // cd)}"


def run_shadow(now=None):
    """Phase 0 engine: evaluate policies and RECORD shadow attempts. Executes
    NOTHING. Returns a summary dict. Honors freeze > circuit > market > policy."""
    now = now or timezone.now()
    rows = list(ComponentHealth.objects.all())
    market = MarketStateService.current(rows)
    breaker = get_breaker(now)
    frozen = auto_recovery_frozen()

    summary = {"market_state": market, "frozen": frozen, "circuit": breaker.state,
               "shadow_planned": 0, "suppressed": 0, "deduped": 0}

    for p in collect_planned(rows):
        window = _cooldown_window(p.policy, now)
        # dedup: at most one attempt per (policy, scope, cooldown window)
        if RecoveryAttempt.objects.filter(policy=p.policy, scope=p.scope, cooldown_window=window).exists():
            summary["deduped"] += 1
            continue

        suppressed_reason = None
        if frozen:
            suppressed_reason = "frozen"
        elif breaker.state == CircuitBreakerState.State.OPEN:
            suppressed_reason = "circuit_open"
        elif p.market_sensitive and market == MarketState.CLOSED:
            suppressed_reason = "market_closed"
        elif not policy_shadow(p.policy) and not policy_enabled(p.policy):
            suppressed_reason = "policy_disabled"

        outcome = RecoveryOutcome.SUPPRESSED if suppressed_reason else RecoveryOutcome.SHADOW_PLANNED
        det = dict(p.detail)
        det["phase"] = "shadow"
        if suppressed_reason:
            det["suppressed_reason"] = suppressed_reason
        if p.market_sensitive and market == MarketState.UNKNOWN and not suppressed_reason:
            det["market_note"] = "MARKET_UNKNOWN -> live policy would treat as AMBIGUOUS and escalate (no destructive action)"

        link_alert = AlertEvent.objects.filter(dedup_key=f"{p.component}:{getattr(p.row,'terminal_node_id',0) or 0}:{getattr(p.row,'trading_account_id',0) or 0}").exclude(status=AlertEvent.Status.RESOLVED).first()

        RecoveryAttempt.objects.create(
            policy=p.policy, action=p.action, scope=p.scope, component=p.component,
            terminal_node_id=getattr(p.row, "terminal_node_id", None),
            trading_account_id=getattr(p.row, "trading_account_id", None),
            pre_state=p.pre_state, post_state=p.pre_state,  # shadow: no change
            outcome=outcome, shadow=True, attempt_number=1,
            cooldown_window=window, market_state=market,
            linked_alert=link_alert, detail=det,
        )
        if outcome == RecoveryOutcome.SHADOW_PLANNED:
            # A would-act counts toward the circuit breaker (storm detection).
            breaker.action_count += 1
            if breaker.action_count >= breaker.threshold:
                _trip_breaker(breaker, now)
            else:
                breaker.save()
            summary["shadow_planned"] += 1
            summary["circuit"] = breaker.state
        else:
            summary["suppressed"] += 1
    return summary

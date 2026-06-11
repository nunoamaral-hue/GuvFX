"""RX-2A — Trading Health Engine (aggregation).

Rolls ComponentHealth up into TradingHealthSnapshot at GLOBAL / per-terminal /
per-account scope. Trading health, not process health: the bridge process can
be UP while a logged-out account is DOWN.
"""
from ..constants import (
    Component, HealthStatus, TradingState, Scope, CRITICAL_COMPONENTS,
)
from ..models import ComponentHealth, TradingHealthSnapshot


def _human(r):
    """Human reason for a non-OK ComponentHealth row (uses detail for job types)."""
    component, status = r.component, r.status
    label = dict(Component.CHOICES).get(component, component)
    if component == Component.MT5_BROKER and status == HealthStatus.FAILED:
        return "MT5 is not connected/logged in to the broker"
    if component == Component.MT5_TERMINAL and status == HealthStatus.FAILED:
        return "MT5 terminal is not responding"
    if component == Component.SNAPSHOT_FEED and status in (HealthStatus.STALE, HealthStatus.FAILED):
        return "Market data feed is stale"
    if component == Component.EXECUTION_PIPELINE and status != HealthStatus.OK:
        cats = (r.detail or {}).get("by_category", {})
        te = cats.get("trade_exec") or []
        if te:
            return f"Execution pipeline: {len(te)} stale trade-execution job(s) {te} — trading impaired (critical)"
        sy = cats.get("sync") or []
        va = cats.get("validation") or []
        un = cats.get("unknown") or []
        parts = []
        if sy:
            parts.append(f"{len(sy)} stale sync job(s) {sy}")
        if va:
            parts.append(f"{len(va)} stale validation job(s) {va}")
        if un:
            parts.append(f"{len(un)} stale job(s) of unknown type {un}")
        return "Execution pipeline: " + ("; ".join(parts) or "stale jobs") + " (degraded — not blocking trading)"
    return f"{label}: {status.lower()}"


def _state_from(rows):
    """rows: iterable of ComponentHealth. Returns (state, can_trade, reasons, components_map).

    Severity model: a CRITICAL component FAILED -> DOWN; CRITICAL STALE -> IMPAIRED;
    CRITICAL DEGRADED -> DEGRADED (does NOT block can_trade — e.g. a stale SYNC job).
    """
    comp_map = {}
    reasons = []
    crit_failed = crit_stale = supporting_bad = any_known = False
    for r in rows:
        comp_map[r.component] = r.status
        if r.status != HealthStatus.UNKNOWN:
            any_known = True
        if r.status == HealthStatus.OK:
            continue
        reasons.append(_human(r))
        critical = r.component in CRITICAL_COMPONENTS
        if r.status == HealthStatus.FAILED:
            crit_failed = crit_failed or critical
            supporting_bad = supporting_bad or (not critical)
        elif r.status == HealthStatus.STALE:
            crit_stale = crit_stale or critical
            supporting_bad = supporting_bad or (not critical)
        elif r.status == HealthStatus.DEGRADED:
            # DEGRADED never blocks trading — even for a critical component.
            supporting_bad = True

    if not any_known:
        return TradingState.UNKNOWN, False, reasons or ["No health signals yet"], comp_map
    if crit_failed:
        state = TradingState.DOWN
    elif crit_stale:
        state = TradingState.IMPAIRED
    elif supporting_bad:
        state = TradingState.DEGRADED
    else:
        state = TradingState.HEALTHY
    can_trade = state in (TradingState.HEALTHY, TradingState.DEGRADED)
    return state, can_trade, reasons, comp_map


def _persist(now, scope, rows, terminal_node=None, mt5_instance=None, trading_account=None):
    state, can_trade, reasons, comp_map = _state_from(rows)
    return TradingHealthSnapshot.objects.create(
        scope=scope, terminal_node=terminal_node, mt5_instance=mt5_instance, trading_account=trading_account,
        state=state, can_trade=can_trade, reasons=reasons, components=comp_map, computed_at=now,
    )


def aggregate(now):
    """Create GLOBAL + per-terminal + per-account snapshots. Returns the list."""
    from execution.models import TerminalNode
    from trading.models import TradingAccount

    all_rows = list(ComponentHealth.objects.all())
    snapshots = [_persist(now, Scope.GLOBAL, all_rows)]

    # Per terminal: this terminal's scoped rows + global infrastructure rows.
    global_rows = [r for r in all_rows if r.terminal_node_id is None and r.trading_account_id is None]
    for node in TerminalNode.objects.filter(status="active"):
        scoped = [r for r in all_rows if r.terminal_node_id == node.id]
        snapshots.append(_persist(now, Scope.TERMINAL, scoped + global_rows, terminal_node=node))

    # Per account: account-scoped rows + its terminal's rows + global infra.
    for acc in TradingAccount.objects.filter(is_active=True):
        acc_node_id = getattr(acc, "terminal_node_id", None)
        scoped = [r for r in all_rows if r.trading_account_id == acc.id
                  or (acc_node_id and r.terminal_node_id == acc_node_id)]
        snapshots.append(_persist(now, Scope.ACCOUNT, scoped + global_rows, trading_account=acc))
    return snapshots

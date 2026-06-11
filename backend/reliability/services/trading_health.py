"""RX-2A — Trading Health Engine (aggregation).

Rolls ComponentHealth up into TradingHealthSnapshot at GLOBAL / per-terminal /
per-account scope. Trading health, not process health: the bridge process can
be UP while a logged-out account is DOWN.
"""
from ..constants import (
    Component, HealthStatus, TradingState, Scope, CRITICAL_COMPONENTS,
)
from ..models import ComponentHealth, TradingHealthSnapshot


def _human(component, status):
    label = dict(Component.CHOICES).get(component, component)
    if component == Component.MT5_BROKER and status == HealthStatus.FAILED:
        return "MT5 is not connected/logged in to the broker"
    if component == Component.MT5_TERMINAL and status == HealthStatus.FAILED:
        return "MT5 terminal is not responding"
    if component == Component.SNAPSHOT_FEED and status in (HealthStatus.STALE, HealthStatus.FAILED):
        return "Market data feed is stale"
    if component == Component.EXECUTION_PIPELINE and status != HealthStatus.OK:
        return "Execution pipeline has orphaned/stuck jobs"
    return f"{label}: {status.lower()}"


def _state_from(rows):
    """rows: iterable of ComponentHealth. Returns (state, can_trade, reasons, components_map)."""
    comp_map = {}
    reasons = []
    crit_failed = crit_stale = supporting_bad = any_known = False
    for r in rows:
        comp_map[r.component] = r.status
        if r.status != HealthStatus.UNKNOWN:
            any_known = True
        if r.status == HealthStatus.OK:
            continue
        reasons.append(_human(r.component, r.status))
        critical = r.component in CRITICAL_COMPONENTS
        if r.status == HealthStatus.FAILED:
            if critical:
                crit_failed = True
            elif r.component == Component.SNAPSHOT_FEED:
                supporting_bad = True
            else:
                supporting_bad = True
        elif r.status == HealthStatus.STALE:
            if critical:
                crit_stale = True
            else:
                supporting_bad = True
        elif r.status == HealthStatus.DEGRADED:
            if critical:
                crit_stale = True
            else:
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

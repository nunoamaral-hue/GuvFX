import logging
import requests

from django.conf import settings
from django.utils import timezone
from rest_framework import permissions, viewsets, status
from rest_framework.decorators import action
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated

from django.db import transaction
from trading.models import TradingAccount

from .models import (
    Strategy,
    StrategyAssignment,
    StrategyChangeLog,
    StrategyRuntimeState,
    StrategyRuntimeEvent,
)
from .serializers import (
    StrategySerializer,
    StrategyAssignmentSerializer,
    StrategyChangeLogSerializer,
)
from .execution import (
    validate_strategy_for_execution,
    prepare_execution_config,
    get_execution_status,
)
from backtests.models import BacktestConfig, BacktestRun
from billing.enforcement import require_entitlement
from core.audit import (
    log_strategy_created,
    log_strategy_updated,
    log_strategy_deleted,
    log_assignment_created,
)

logger = logging.getLogger(__name__)

# Marketplace templates (server-side catalog)
# NOTE: marketplace_strategy_id must match the frontend seed ids.
MARKETPLACE_STRATEGIES = {
    "mp-001": {
        "name": "London Session Box Breakout",
        "description": "Trades Asian session range breakouts during the London open with spread guardrails.",
        "defaults": {
            "timeframe": "M15",
            "symbol_universe": "GBPUSD,EURUSD,GBPJPY",
            "edge_type": "BREAKOUT",
            "risk_per_trade_pct": 0.5,
            "auto_optimize_by_ai": False,
        },
    },
    "mp-002": {
        "name": "Trend EMA Crossover (HTF filter)",
        "description": "EMA crossover with a higher timeframe trend filter.",
        "defaults": {
            "timeframe": "H1",
            "symbol_universe": "EURUSD,USDJPY,AUDUSD",
            "edge_type": "TREND_FOLLOWING",
            "risk_per_trade_pct": 0.5,
            "auto_optimize_by_ai": True,
        },
    },
    "mp-004": {
        "name": "Head & Shoulders Reversal (Beta)",
        "description": "Chart-pattern reversal strategy template (beta).",
        "defaults": {
            "timeframe": "H1",
            "symbol_universe": "EURUSD,GBPUSD,USDJPY,AUDUSD",
            "edge_type": "PATTERN_REVERSAL",
            "risk_per_trade_pct": 0.25,
            "auto_optimize_by_ai": False,
        },
    },
    "mp-005": {
        "name": "Trendline Break Pocket",
        "description": "HTF zone + trendline break + structure shift. Fixed 2R model. Manual zones editable.",
        "trader": "Ali",
        "category": "System-grade",
        "template_slug": "trendline-break-pocket-ali",
        "marketplace_listed": True,
        "automation_ready": True,
        "defaults": {
            "timeframe": "H4",
            "symbol_universe": "EURUSD,GBPUSD",
            "edge_type": "BREAKOUT",
            "risk_per_trade_pct": 0.1,
            "auto_optimize_by_ai": False,
            # Strategy-specific parameters stored in filters JSON
            "filters": {
                "template_slug": "trendline-break-pocket-ali",
                "enabled": True,
                "direction_mode": "both",
                "pairs_enabled": ["EURUSD", "GBPUSD"],
                "htf_timeframe": "D1",
                "execution_timeframe": "H4",
                "rr_target": 2.0,
                "trendline_lookback_bars": 101,
                "trendline_pivot_strength": 2,
                "break_confirm_bars": 1,
                "swing_break_mode": "close_break",
                "swing_lookback": 7,
                "pocket_retest_required": True,
                "entry_buffer_pips": {"EURUSD": 2, "GBPUSD": 3},
                "overshoot_max_pips": {"EURUSD": 12, "GBPUSD": 18},
                "clean_air_min_pips": {"EURUSD": 8, "GBPUSD": 12},
                "max_trades_per_day": 1,
                "news_filter_mode": "major_only",
                "zones": {
                    "EURUSD": [
                        {"zone_name": "Supply 1", "zone_type": "supply", "low": 1.1830, "high": 1.1860, "source": "seeded"},
                        {"zone_name": "Pivot", "zone_type": "pivot", "low": 1.1775, "high": 1.1795, "source": "seeded"},
                        {"zone_name": "Demand 1", "zone_type": "demand", "low": 1.1715, "high": 1.1745, "source": "seeded"},
                    ],
                    "GBPUSD": [
                        {"zone_name": "Supply 1", "zone_type": "supply", "low": 1.3600, "high": 1.3640, "source": "seeded"},
                        {"zone_name": "Pivot", "zone_type": "pivot", "low": 1.3490, "high": 1.3530, "source": "seeded"},
                        {"zone_name": "Demand 1", "zone_type": "demand", "low": 1.3380, "high": 1.3420, "source": "seeded"},
                    ],
                },
            },
            # Entry/exit logic descriptions
            "entry_logic": "1. Price in HTF zone (D1 supply/demand)\n2. Trendline break confirmed (close beyond TL)\n3. Structure shift (swing break)\n4. Retest pocket entry (if enabled)\n5. Clean air validation",
            "exit_logic": "Fixed 2R target from entry. Stop at structural invalidation.",
        },
    },
    "mp-006": {
        "name": "Adaptive Liquidity Trap Scalper",
        "description": "Range-regime liquidity sweep + displacement + confirmation. M5 execution with M15 regime filter.",
        "category": "System-grade",
        "template_slug": "adaptive-liquidity-trap-scalper",
        "marketplace_listed": True,
        "automation_ready": True,
        "defaults": {
            "timeframe": "M5",
            "symbol_universe": "EURUSD,GBPUSD",
            "edge_type": "MEAN_REVERSION",
            "risk_per_trade_pct": 0.1,
            "auto_optimize_by_ai": False,
            "filters": {
                "template_slug": "adaptive-liquidity-trap-scalper",
                "enabled": True,
                "direction_mode": "both",
                "pairs_enabled": ["EURUSD", "GBPUSD"],
                "execution_timeframe": "M5",
                "regime_timeframe": "M15",
                "rr_target": 2.0,
                "max_trades_per_day": 10,
            },
            "entry_logic": "1. M15 regime = range (ADX < 25, price within Keltner)\n2. Liquidity sweep beyond session high/low\n3. Displacement candle confirmation (body > 1.0 ATR)\n4. Entry on pullback into displacement origin",
            "exit_logic": "Fixed 2R target from entry. Stop beyond sweep extreme.",
        },
    },
    "mp-007": {
        "name": "Structural Continuation Engine",
        "description": "H4 bias + H1 BOS + pullback + rejection continuation. H1 execution with H4 context.",
        "category": "System-grade",
        "template_slug": "structural-continuation-engine",
        "marketplace_listed": True,
        "automation_ready": True,
        "defaults": {
            "timeframe": "H1",
            "symbol_universe": "EURUSD,GBPUSD",
            "edge_type": "TREND_FOLLOWING",
            "risk_per_trade_pct": 0.1,
            "auto_optimize_by_ai": False,
            "filters": {
                "template_slug": "structural-continuation-engine",
                "enabled": True,
                "direction_mode": "both",
                "pairs_enabled": ["EURUSD", "GBPUSD"],
                "htf_timeframe": "H4",
                "execution_timeframe": "H1",
                "rr_target": 2.0,
                "max_trades_per_day": 4,
            },
            "entry_logic": "1. H4 directional bias established (fractal HH/HL or LH/LL + ADX)\n2. H1 break of structure (BOS) in bias direction\n3. Pullback into 38-62% Fibonacci zone\n4. Rejection candle confirmation (body > 0.5 ATR)",
            "exit_logic": "Fixed 2R target from entry. Stop at structural invalidation.",
        },
    },
    "mp-008": {
        "name": "Trend Continuation Engine v1",
        "description": "EMA50/200 trend filter + ATR pullback zone + confirmation candle. H4 execution, fixed 1.5R.",
        "category": "System-grade",
        "template_slug": "tc1-engine-v1",
        "marketplace_listed": True,
        "automation_ready": True,
        "defaults": {
            "timeframe": "H4",
            "symbol_universe": "EURUSD,GBPUSD",
            "edge_type": "TREND_FOLLOWING",
            "risk_per_trade_pct": 1.5,
            "auto_optimize_by_ai": False,
            "filters": {
                "template_slug": "tc1-engine-v1",
                "enabled": True,
                "direction_mode": "both",
                "pairs_enabled": ["EURUSD", "GBPUSD"],
                "execution_timeframe": "H4",
                "ema_fast": 50,
                "ema_slow": 200,
                "atr_period": 14,
                "pullback_atr_mult": 0.25,
                "sl_atr_mult": 1.2,
                "rr_fixed": 1.5,
                "risk_pct": 1.5,
                "max_trades_per_day": 4,
            },
            "entry_logic": "1. EMA50 > EMA200 (bull) or EMA50 < EMA200 (bear) establishes trend\n2. Price enters pullback zone within 0.25 × ATR14 of EMA50\n3. Confirmation candle closes in trend direction\n4. Market entry at next bar open",
            "exit_logic": "SL = 1.2 × ATR14 beyond entry. TP = 1.5 × SL distance (fixed 1.5R).",
        },
    },
    "mp-009": {
        "name": "TBP V3 Hybrid Sleeve v1",
        "description": "Wrapper: CORE (TBP trendline break pocket) + SLEEVE (TC1 trend continuation on risk-on days, EURUSD/GBPUSD only). H4 execution.",
        "category": "System-grade",
        "template_slug": "tbp-v3-hybrid-sleeve-v1",
        "marketplace_listed": True,
        "automation_ready": True,
        "defaults": {
            "timeframe": "H4",
            "symbol_universe": "EURUSD,GBPUSD",
            "edge_type": "TREND_FOLLOWING",
            "risk_per_trade_pct": 0.03,
            "auto_optimize_by_ai": False,
            "filters": {
                "template_slug": "tbp-v3-hybrid-sleeve-v1",
                "enabled": True,
                "direction_mode": "both",
                "pairs_enabled": ["EURUSD", "GBPUSD"],
                "alpha": 0.25,
                "max_trades_per_day": 4,
                # TBP-compatible fields (CORE)
                "htf_timeframe": "D1",
                "execution_timeframe": "H4",
                "rr_target": 2.0,
                "trendline_lookback_bars": 101,
                "trendline_pivot_strength": 2,
                "break_confirm_bars": 1,
                "swing_break_mode": "close_break",
                "swing_lookback": 7,
                "pocket_retest_required": True,
                "entry_buffer_pips": {"EURUSD": 2, "GBPUSD": 3},
                "overshoot_max_pips": {"EURUSD": 12, "GBPUSD": 18},
                "clean_air_min_pips": {"EURUSD": 8, "GBPUSD": 12},
                "news_filter_mode": "major_only",
                "zones": {
                    "EURUSD": [
                        {"zone_name": "Supply 1", "zone_type": "supply", "low": 1.1830, "high": 1.1860, "source": "seeded"},
                        {"zone_name": "Pivot", "zone_type": "pivot", "low": 1.1775, "high": 1.1795, "source": "seeded"},
                        {"zone_name": "Demand 1", "zone_type": "demand", "low": 1.1715, "high": 1.1745, "source": "seeded"},
                    ],
                    "GBPUSD": [
                        {"zone_name": "Supply 1", "zone_type": "supply", "low": 1.3600, "high": 1.3640, "source": "seeded"},
                        {"zone_name": "Pivot", "zone_type": "pivot", "low": 1.3490, "high": 1.3530, "source": "seeded"},
                        {"zone_name": "Demand 1", "zone_type": "demand", "low": 1.3380, "high": 1.3420, "source": "seeded"},
                    ],
                },
            },
            "entry_logic": "CORE: HTF zone + trendline break + structure shift (TBP, fixed 2R). SLEEVE: EMA50/200 trend + ATR pullback + confirmation (TC1, 1.5R) — only on risk-on days for EURUSD/GBPUSD.",
            "exit_logic": "CORE: Fixed 2R target from entry. SLEEVE: SL = 1.2 × ATR14, TP = 1.5R. Selection: CORE_PRIORITY (TBP first, TC1 fallback).",
        },
    },
}


def validate_trendline_break_pocket_filters(filters: dict) -> dict:
    """
    Validate Trendline Break Pocket (Ali) strategy-specific filter parameters.
    Returns dict of validation errors (empty if valid).
    """
    errors = {}
    template_slug = filters.get("template_slug", "")

    # Only validate if this is the Trendline Break Pocket template
    if template_slug != "trendline-break-pocket-ali":
        return errors

    # direction_mode validation
    direction_mode = filters.get("direction_mode")
    valid_direction_modes = {"both", "long", "short"}
    if direction_mode and direction_mode not in valid_direction_modes:
        errors["direction_mode"] = f"direction_mode must be one of: {', '.join(valid_direction_modes)}"

    # trendline_lookback_bars validation (must be >= 50)
    lookback = filters.get("trendline_lookback_bars")
    if lookback is not None:
        try:
            lookback_int = int(lookback)
            if lookback_int < 50:
                errors["trendline_lookback_bars"] = "trendline_lookback_bars must be >= 50"
        except (TypeError, ValueError):
            errors["trendline_lookback_bars"] = "trendline_lookback_bars must be an integer"

    # rr_target validation (must be > 0)
    rr_target = filters.get("rr_target")
    if rr_target is not None:
        try:
            rr_float = float(rr_target)
            if rr_float <= 0:
                errors["rr_target"] = "rr_target must be > 0"
        except (TypeError, ValueError):
            errors["rr_target"] = "rr_target must be a number"

    # Zone validation (low < high for each zone)
    zones = filters.get("zones") or {}
    for symbol, zone_list in zones.items():
        if not isinstance(zone_list, list):
            continue
        for i, zone in enumerate(zone_list):
            if not isinstance(zone, dict):
                continue
            low = zone.get("low")
            high = zone.get("high")
            if low is not None and high is not None:
                try:
                    low_f = float(low)
                    high_f = float(high)
                    if low_f >= high_f:
                        errors[f"zones.{symbol}[{i}]"] = f"Zone low ({low_f}) must be < high ({high_f})"
                except (TypeError, ValueError):
                    errors[f"zones.{symbol}[{i}]"] = "Zone low/high must be numbers"

    return errors

class StrategyViewSet(viewsets.ModelViewSet):
    queryset = Strategy.objects.all()
    serializer_class = StrategySerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = Strategy.objects.all()
        if not user.is_staff:
            qs = qs.filter(owner=user)
        return qs

    def perform_create(self, serializer):
        """Create strategy and log audit event."""
        instance = serializer.save()
        log_strategy_created(self.request, instance)

    def perform_destroy(self, instance):
        """Delete strategy and log audit event."""
        strategy_id = instance.id
        strategy_name = instance.name
        instance.delete()
        log_strategy_deleted(self.request, strategy_id, strategy_name)

    def perform_update(self, serializer):
        user = self.request.user
        instance: Strategy = self.get_object()

        # Snapshot key settings before update
        before_settings = {
            "timeframe": instance.timeframe,
            "symbol_universe": instance.symbol_universe,
            "ma_fast_period": instance.ma_fast_period,
            "ma_slow_period": instance.ma_slow_period,
            "ma_type": instance.ma_type,
            "magic_number": instance.magic_number,
        }

        updated: Strategy = serializer.save()

        after_settings = {
            "timeframe": updated.timeframe,
            "symbol_universe": updated.symbol_universe,
            "ma_fast_period": updated.ma_fast_period,
            "ma_slow_period": updated.ma_slow_period,
            "ma_type": updated.ma_type,
            "magic_number": updated.magic_number,
        }

        # Only log if something actually changed
        if before_settings != after_settings:
            StrategyChangeLog.objects.create(
                strategy=updated,
                source=StrategyChangeLog.SOURCE_MANUAL,
                changed_by=user,
                before_settings=before_settings,
                after_settings=after_settings,
            )
            # Audit log
            changed_fields = [k for k, v in after_settings.items() if before_settings.get(k) != v]
            log_strategy_updated(self.request, updated, changed_fields)

    @action(detail=True, methods=["get"], url_path="execution/validate")
    def execution_validate(self, request, pk=None):
        """
        Validate if a strategy is ready for execution.

        Returns validation status, errors, and warnings.
        This is a placeholder endpoint - execution engine is not yet implemented.
        """
        strategy = self.get_object()
        validation = validate_strategy_for_execution(strategy)
        return Response({
            "strategy_id": strategy.id,
            "strategy_name": strategy.name,
            **validation,
        })

    @action(detail=True, methods=["get"], url_path="execution/config")
    def execution_config(self, request, pk=None):
        """
        Get the execution configuration for a strategy.

        Returns the configuration that would be sent to the execution engine.
        NOTE: This is a placeholder - no trades are executed.
        """
        strategy = self.get_object()
        config = prepare_execution_config(strategy)
        return Response(config)

    @action(detail=True, methods=["get"], url_path="execution/status")
    def execution_status(self, request, pk=None):
        """
        Get the current execution status of a strategy.

        Returns status information about the strategy's execution state.
        NOTE: Execution engine is not yet implemented.
        """
        strategy = self.get_object()
        status_info = get_execution_status(strategy)
        return Response(status_info)

    @action(detail=True, methods=["get"], url_path="execution/live-status")
    def execution_live_status(self, request, pk=None):
        """
        GET /api/strategies/strategies/<id>/execution/live-status/?account_id=<id>

        Health-check whether this strategy is "live" end-to-end:
        strategy active, assignment active, scheduler running, agents reachable, ingest healthy.

        Returns:
            { overall: "PASS"|"FAIL"|"DEGRADED", strategy_id, account_id,
              checked_at, checks: [{name, status, detail}, ...] }
        """
        import os
        import json as _json
        import urllib.request
        import urllib.parse
        from datetime import timedelta
        from execution.models import ExecutionJob
        from core.models import AuditEvent

        strategy = self.get_object()
        account_id = request.query_params.get("account_id")
        if not account_id:
            return Response(
                {"ok": False, "reason": "account_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        account = TradingAccount.objects.filter(id=account_id).first()
        if not account:
            return Response(
                {"ok": False, "reason": f"Account {account_id} not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        now = timezone.now()
        lookback_24h = now - timedelta(hours=24)
        checks = []

        # 1. Strategy active
        if not strategy.is_active:
            checks.append({"name": "strategy_active", "status": "FAIL",
                           "detail": f"Strategy '{strategy.name}' is_active=False"})
        else:
            checks.append({"name": "strategy_active", "status": "PASS",
                           "detail": f"Strategy '{strategy.name}' is_active=True"})

        # 2. Account active + demo
        if not account.is_active:
            checks.append({"name": "account_active", "status": "FAIL",
                           "detail": f"Account {account_id} is_active=False"})
        elif not account.is_demo:
            checks.append({"name": "account_demo", "status": "WARN",
                           "detail": f"Account {account_id} is_demo=False"})
        else:
            checks.append({"name": "account_active", "status": "PASS",
                           "detail": f"Account {account_id} is_active=True, is_demo=True"})

        # 3. Assignment active
        assignment = StrategyAssignment.objects.filter(
            strategy=strategy, account=account, is_active=True,
        ).first()
        if not assignment:
            checks.append({"name": "assignment_active", "status": "FAIL",
                           "detail": f"No active assignment for strategy={strategy.id} account={account_id}"})
        else:
            checks.append({"name": "assignment_active", "status": "PASS",
                           "detail": f"Assignment id={assignment.id} is_active=True"})

        # 4. Recent PLACE_ORDER jobs (scheduler activity)
        recent_po = ExecutionJob.objects.filter(
            account_id=account_id, strategy_id=strategy.id,
            job_type=ExecutionJob.JobType.PLACE_ORDER,
            created_at__gte=lookback_24h,
        ).order_by("-created_at")
        po_count = recent_po.count()
        if po_count > 0:
            latest = recent_po.first()
            checks.append({"name": "scheduler_recent", "status": "PASS",
                           "detail": f"{po_count} PLACE_ORDER jobs in 24h, latest={latest.status} at {latest.created_at.isoformat()}"})
        else:
            recent_evals = AuditEvent.objects.filter(
                event_type="SIGNAL_EVALUATED", entity_type="strategy",
                entity_id=str(strategy.id), created_at__gte=lookback_24h,
            ).count()
            if recent_evals > 0:
                checks.append({"name": "scheduler_recent", "status": "PASS",
                               "detail": f"0 PLACE_ORDER but {recent_evals} SIGNAL_EVALUATED in 24h"})
            else:
                checks.append({"name": "scheduler_recent", "status": "FAIL",
                               "detail": "No PLACE_ORDER or SIGNAL_EVALUATED events in 24h"})

        # 5. Ingest worker (recent SYNC_POSITIONS)
        sync_count = ExecutionJob.objects.filter(
            account_id=account_id,
            job_type=ExecutionJob.JobType.SYNC_POSITIONS,
            created_at__gte=lookback_24h,
        ).count()
        if sync_count > 0:
            checks.append({"name": "ingest_worker", "status": "PASS",
                           "detail": f"{sync_count} SYNC_POSITIONS in 24h"})
        else:
            checks.append({"name": "ingest_worker", "status": "WARN",
                           "detail": "No SYNC_POSITIONS in 24h (normal if no trades)"})

        # Overall verdict
        fail_count = sum(1 for c in checks if c["status"] == "FAIL")
        warn_count = sum(1 for c in checks if c["status"] == "WARN")
        overall = "FAIL" if fail_count > 0 else ("DEGRADED" if warn_count > 0 else "PASS")

        return Response({
            "overall": overall,
            "strategy_id": strategy.id,
            "account_id": int(account_id),
            "checked_at": now.isoformat(),
            "checks": checks,
        })

    @action(detail=True, methods=["get"], url_path="execution/engine-status")
    def execution_engine_status(self, request, pk=None):
        """
        GET /api/strategies/strategies/<id>/execution/engine-status/?account_id=<id>

        Returns per-engine, per-symbol runtime state and recent evaluation events
        for the active assignment. Used by the frontend observability dashboard.

        Response:
            { strategy_id, account_id, assignment_id, stage,
              checked_at, runtime_states: [...], recent_events: [...] }
        """
        strategy = self.get_object()
        account_id = request.query_params.get("account_id")
        if not account_id:
            return Response(
                {"ok": False, "reason": "account_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        account = TradingAccount.objects.filter(id=account_id).first()
        if not account:
            return Response(
                {"ok": False, "reason": f"Account {account_id} not found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        assignment = StrategyAssignment.objects.filter(
            strategy=strategy, account=account, is_active=True,
        ).order_by("-id").first()
        if not assignment:
            return Response(
                {"ok": False, "reason": "no_active_assignment"},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Fetch all runtime states for this assignment
        states = StrategyRuntimeState.objects.filter(
            assignment=assignment,
        ).order_by("strategy_key", "symbol")

        runtime_states = []
        for s in states:
            runtime_states.append({
                "strategy_key": s.strategy_key,
                "symbol": s.symbol,
                "last_eval_at": s.last_eval_at.isoformat() if s.last_eval_at else None,
                "daily_r_pnl": str(s.daily_r_pnl),
                "daily_trade_count": s.daily_trade_count,
                "weekly_r_pnl": str(s.weekly_r_pnl),
                "consecutive_losses": s.consecutive_losses,
                "paused_until": s.paused_until.isoformat() if s.paused_until else None,
                "pause_reason": s.pause_reason,
                "regime_blob": s.regime_blob or {},
                "updated_at": s.updated_at.isoformat(),
            })

        # Fetch recent events (last 50)
        events = StrategyRuntimeEvent.objects.filter(
            assignment=assignment,
        ).order_by("-created_at")[:50]

        recent_events = []
        for e in events:
            recent_events.append({
                "event_type": e.event_type,
                "strategy_key": e.strategy_key,
                "symbol": e.symbol,
                "reason_code": e.reason_code,
                "bar_close_time": e.bar_close_time,
                "created_at": e.created_at.isoformat(),
            })

        return Response({
            "strategy_id": strategy.id,
            "account_id": int(account_id),
            "assignment_id": assignment.id,
            "stage": assignment.stage,
            "checked_at": timezone.now().isoformat(),
            "runtime_states": runtime_states,
            "recent_events": recent_events,
        })

    @action(detail=True, methods=["post"], url_path="execution/run-signal")
    def run_signal(self, request, pk=None):
        """
        Evaluate and execute a signal for the Trendline Break Pocket strategy.

        POST /api/strategies/{id}/execution/run-signal/

        Query params:
            account_id: Required. The trading account ID.

        Request body (optional, for manual test signals):
            {
                "symbol": "EURUSD",  # Required
                "manual": true,      # If true, uses manual_params below
                "side": "BUY",       # "BUY" or "SELL"
                "entry_price": 1.0850,
                "sl_price": 1.0800,
                "tp_price": 1.0950
            }

        Returns:
            {
                "ok": true/false,
                "signal_type": "BUY" or "SELL" or null,
                "symbol": "EURUSD",
                "entry_price": 1.0850,
                "sl_price": 1.0800,
                "tp_price": 1.0950,
                "lots": 0.01,
                "reason": "job_queued" or rejection reason,
                "job_id": 123 or null,
                "details": {...}
            }
        """
        from .signal_engine import run_signal_evaluation
        from execution.models import ExecutionKillSwitchEngaged

        strategy = self.get_object()
        user = request.user

        # Get account_id from query params
        account_id = request.query_params.get("account_id")
        if not account_id:
            return Response(
                {"ok": False, "reason": "account_id_required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Validate account ownership
        account = TradingAccount.objects.filter(id=account_id).first()
        if not account:
            return Response(
                {"ok": False, "reason": "account_not_found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if not user.is_staff and account.user_id != user.id:
            return Response(
                {"ok": False, "reason": "account_not_owned"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Validate strategy ownership
        if not user.is_staff and strategy.owner_id != user.id:
            return Response(
                {"ok": False, "reason": "strategy_not_owned"},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Get symbol from request body
        symbol = request.data.get("symbol", "EURUSD").upper()

        # Check for manual test signal params
        manual_params = None
        if request.data.get("manual"):
            manual_params = {
                "side": request.data.get("side", "BUY"),
                "entry_price": request.data.get("entry_price"),
                "sl_price": request.data.get("sl_price"),
                "tp_price": request.data.get("tp_price"),
            }
            # Optional explicit lots override (validated in signal engine)
            if "lots" in request.data:
                manual_params["lots"] = request.data.get("lots")

            # Validate required fields for manual signal
            if not all([
                manual_params.get("entry_price"),
                manual_params.get("sl_price"),
                manual_params.get("tp_price"),
            ]):
                return Response(
                    {"ok": False, "reason": "manual_signal_missing_prices"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Run signal evaluation. If the kill switch is engaged, the order-job
        # creation fails closed at the model layer (no order placed); translate
        # that to a clean 503 instead of an unhandled 500.
        try:
            result = run_signal_evaluation(
                request=request,
                strategy=strategy,
                account=account,
                symbol=symbol,
                user=user,
                manual_params=manual_params,
            )
        except ExecutionKillSwitchEngaged as exc:
            return Response(
                {
                    "ok": False,
                    "reason": "execution_disabled",
                    "detail": "Execution is currently disabled (kill switch engaged); no order was placed.",
                    "kill_reason": exc.reason,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(result.to_dict())

    @action(detail=False, methods=["post"], url_path="marketplace/assign")
    def marketplace_assign(self, request):
        user = request.user

        # Entitlement gate: user must have can_assign_strategies
        require_entitlement(user, "can_assign_strategies")

        marketplace_strategy_id = request.data.get("marketplace_strategy_id")
        account_id = request.data.get("account_id")

        if not marketplace_strategy_id:
            return Response({"detail": "marketplace_strategy_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        tpl = MARKETPLACE_STRATEGIES.get(marketplace_strategy_id)
        if not tpl:
            return Response({"detail": "Unknown marketplace_strategy_id"}, status=status.HTTP_400_BAD_REQUEST)

        if not account_id:
            return Response({"detail": "account_id is required"}, status=status.HTTP_400_BAD_REQUEST)

        # Ownership gate for account
        acc_qs = TradingAccount.objects.filter(id=account_id)
        if not user.is_staff:
            acc_qs = acc_qs.filter(user=user)
        account = acc_qs.first()
        if not account:
            return Response({"detail": "account not found"}, status=status.HTTP_404_NOT_FOUND)

        defaults = tpl.get("defaults") or {}

        # Only apply fields that exist on Strategy
        allowed_fields = {f.name for f in Strategy._meta.fields}
        create_kwargs = {k: v for k, v in defaults.items() if k in allowed_fields}

        # Ensure required ownership fields
        if "owner" in allowed_fields:
            create_kwargs["owner"] = user

        template_name = tpl.get("name") or "Marketplace Strategy"

        if "name" in allowed_fields:
            create_kwargs["name"] = template_name

        if "description" in allowed_fields:
            create_kwargs["description"] = tpl.get("description") or ""

        try:
            with transaction.atomic():
                # 1. Find or create the Strategy row for this marketplace template
                if "owner" in allowed_fields:
                    existing_strategy = (
                        Strategy.objects
                        .filter(owner=user, name=template_name)
                        .order_by("-id")
                        .first()
                    )
                else:
                    existing_strategy = (
                        Strategy.objects
                        .filter(name=template_name)
                        .order_by("-id")
                        .first()
                    )

                if existing_strategy:
                    strategy_to_use = existing_strategy
                    # Update filters on existing strategy if stale (ensures engine picks up config)
                    tpl_filters = defaults.get("filters")
                    if tpl_filters and existing_strategy.filters != tpl_filters:
                        existing_strategy.filters = tpl_filters
                        existing_strategy.save(update_fields=["filters", "updated_at"])
                else:
                    strategy_to_use = Strategy.objects.create(**create_kwargs)

                # 2. Look for existing assignment scoped to (account, strategy)
                existing = (
                    StrategyAssignment.objects
                    .select_for_update()
                    .filter(account=account, strategy=strategy_to_use)
                    .order_by("-id")
                    .first()
                )

                # 3. Idempotency: if assignment already exists and is active, return early
                if existing and existing.is_active:
                    return Response(
                        {
                            "ok": True,
                            "marketplace_strategy_id": marketplace_strategy_id,
                            "strategy_id": existing.strategy_id,
                            "assignment_id": existing.id,
                            "strategy_name": getattr(existing.strategy, "name", ""),
                            "account_id": account.id,
                            "stage": existing.stage,
                            "already_assigned": True,
                        },
                        status=status.HTTP_200_OK,
                    )

                # 4. Re-activate if deactivated, else create new
                if existing:
                    existing.is_active = True
                    existing.stage = StrategyAssignment.STAGE_TEST
                    existing.save(update_fields=["is_active", "stage", "updated_at"])
                    assignment = existing
                else:
                    assignment = StrategyAssignment.objects.create(
                        strategy=strategy_to_use,
                        account=account,
                        is_active=True,
                        stage=StrategyAssignment.STAGE_TEST,
                    )

        except Exception:
            logger.exception("marketplace_assign failed for %s account=%s", marketplace_strategy_id, account_id)
            return Response(
                {"ok": False, "detail": "Internal error during assignment. Check server logs."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # 5. Return payload
        return Response(
            {
                "ok": True,
                "marketplace_strategy_id": marketplace_strategy_id,
                "strategy_id": strategy_to_use.id,
                "assignment_id": assignment.id,
                "strategy_name": getattr(strategy_to_use, "name", ""),
                "account_id": account.id,
                "stage": assignment.stage,
                "already_assigned": False,
            },
            status=status.HTTP_201_CREATED,
        )

class StrategyAssignmentViewSet(viewsets.ModelViewSet):
    queryset = StrategyAssignment.objects.select_related("strategy", "account")
    serializer_class = StrategyAssignmentSerializer
    permission_classes = [IsAuthenticated]

    def perform_create(self, serializer):
        user = self.request.user
        require_entitlement(user, "can_assign_strategies")

        assignment = serializer.validated_data
        account = assignment["account"]

        # Non-staff can only assign strategies to their own accounts
        if not user.is_staff and account.user_id != user.id:
            raise PermissionDenied("You do not own this trading account.")

        with transaction.atomic():
            obj = serializer.save()
            if obj.is_active and obj.account.mt5_instance_id:
                # Deactivate any other active assignments on accounts within the same instance
                StrategyAssignment.objects.filter(
                    account__mt5_instance_id=obj.account.mt5_instance_id,
                    account__user_id=obj.account.user_id,
                    is_active=True,
                ).exclude(id=obj.id).update(is_active=False)

            # Audit log
            log_assignment_created(self.request, obj)

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user

        if not user.is_superuser:
            qs = qs.filter(strategy__owner=user)

        strategy_id = self.request.query_params.get("strategy")
        account_id = self.request.query_params.get("account")

        if strategy_id:
            qs = qs.filter(strategy_id=strategy_id)
        if account_id:
            qs = qs.filter(account_id=account_id)

        return qs

class StrategyAutoTuneView(APIView):
    """
    POST /api/strategies/strategies/<pk>/auto-tune/

    - Ensures the strategy has a backtest config (creates a default if needed)
    - Creates and processes a new backtest run (fake engine for now)
    - Uses the same AI helper logic to build parameter & risk suggestions
    - If strategy.auto_optimize_by_ai is True, applies recommended_settings.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk: int):
        user = request.user
        require_entitlement(user, "can_run_backtests")

        try:
            strategy = Strategy.objects.get(pk=pk)
        except Strategy.DoesNotExist:
            raise NotFound("Strategy not found.")

        if not user.is_staff and strategy.owner_id != user.id:
            raise PermissionDenied("You do not have access to this strategy.")

        # 1) Ensure there is at least one backtest config
        config = (
            BacktestConfig.objects.filter(strategy=strategy)
            .order_by("-created_at")
            .first()
        )
        if not config:
            # Create a simple default config if none exist
            symbol = (strategy.symbol_universe.split(",")[0].strip()
                      if strategy.symbol_universe else "EURUSD")
            from datetime import date, timedelta
            today = date.today()
            two_years_ago = today.replace(year=today.year - 2)

            config = BacktestConfig.objects.create(
                owner=user,
                name=f"Auto-tune for {strategy.name}",
                description="Auto-created config for AI auto-tuning",
                strategy=strategy,
                symbol=symbol,
                timeframe=strategy.timeframe or "H4",
                date_from=two_years_ago,
                date_to=today,
                initial_balance=10000,  # simple fallback
                risk_per_trade_pct=strategy.risk_per_trade_pct,
                slippage_points=None,
                commission_per_lot=None,
                is_active=True,
            )

        # 2) Create a new backtest run for this config
        run = BacktestRun.objects.create(
            config=config,
            symbol=config.symbol,
            timeframe=config.timeframe,
            date_from=config.date_from,
            date_to=config.date_to,
            initial_balance=config.initial_balance,
            status=BacktestRun.STATUS_PENDING,
        )

        # 3) Process the run immediately with a fake engine
        run.status = BacktestRun.STATUS_RUNNING
        run.started_at = timezone.now()
        run.save(update_fields=["status", "started_at"])

        metrics, equity_curve = self._generate_dummy_results(run)

        run.status = BacktestRun.STATUS_COMPLETED
        run.finished_at = timezone.now()
        run.metrics = metrics
        run.equity_curve = equity_curve
        run.error_message = ""
        run.save(
            update_fields=[
                "status",
                "finished_at",
                "metrics",
                "equity_curve",
                "error_message",
            ]
        )

        summary = f"Auto-tune backtest completed with {metrics.get('num_trades', 0)} trades."
        param_suggestions = {}
        risk_suggestions = {}
        notes = ""
        applied_settings = None

        if strategy.auto_optimize_by_ai:
            rec = param_suggestions.get("recommended_settings") or {}
            if rec:
                # Snapshot key settings before applying AI changes
                before_settings = {
                    "timeframe": strategy.timeframe,
                    "symbol_universe": strategy.symbol_universe,
                    "ma_fast_period": strategy.ma_fast_period,
                    "ma_slow_period": strategy.ma_slow_period,
                    "ma_type": strategy.ma_type,
                    "magic_number": strategy.magic_number,
                }

                # Apply recommended settings to the strategy
                strategy.timeframe = rec.get("timeframe", strategy.timeframe)
                if "symbol_universe" in rec and rec["symbol_universe"] is not None:
                    strategy.symbol_universe = rec["symbol_universe"]
                if "ma_fast_period" in rec:
                    strategy.ma_fast_period = rec["ma_fast_period"]
                if "ma_slow_period" in rec:
                    strategy.ma_slow_period = rec["ma_slow_period"]
                if "ma_type" in rec and rec["ma_type"] is not None:
                    strategy.ma_type = rec["ma_type"]
                if "magic_number" in rec and rec["magic_number"] is not None:
                    strategy.magic_number = rec["magic_number"]

                strategy.save()
                applied_settings = rec

                # Snapshot key settings after applying AI changes
                after_settings = {
                    "timeframe": strategy.timeframe,
                    "symbol_universe": strategy.symbol_universe,
                    "ma_fast_period": strategy.ma_fast_period,
                    "ma_slow_period": strategy.ma_slow_period,
                    "ma_type": strategy.ma_type,
                    "magic_number": strategy.magic_number,
                }

                # Log change if there were actual modifications
                if before_settings != after_settings:
                    StrategyChangeLog.objects.create(
                        strategy=strategy,
                        source=StrategyChangeLog.SOURCE_AI_AUTO_TUNE,
                        changed_by=None,  # AI change
                        before_settings=before_settings,
                        after_settings=after_settings,
                    )

        return Response(
            {
                "strategy_id": strategy.id,
                "auto_optimize_by_ai": strategy.auto_optimize_by_ai,
                "backtest_run_id": run.id,
                "performance_summary": summary,
                "parameter_suggestions": param_suggestions,
                "risk_suggestions": risk_suggestions,
                "applied_settings": applied_settings,
                "notes": notes,
            },
            status=200,
        )

    def _generate_dummy_results(self, run: BacktestRun):
        """
        Same style as the fake engine: produce simple metrics & equity curve.
        """
        total_return_pct = 12.5
        max_drawdown_pct = 8.0
        win_rate_pct = 57.0
        num_trades = 120

        initial_equity = float(run.initial_balance)
        final_equity = initial_equity * (1 + total_return_pct / 100.0)

        equity_curve = [
            {"step": 0, "equity": initial_equity},
            {"step": 1, "equity": initial_equity * 0.98},
            {"step": 2, "equity": initial_equity * 1.03},
            {"step": 3, "equity": initial_equity * 1.05},
            {"step": 4, "equity": final_equity},
        ]

        metrics = {
            "total_return_pct": total_return_pct,
            "max_drawdown_pct": max_drawdown_pct,
            "win_rate_pct": win_rate_pct,
            "num_trades": num_trades,
            "initial_balance": float(run.initial_balance),
            "final_balance": final_equity,
        }

        return metrics, equity_curve
    
class StrategyChangeLogViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only list of change logs for a strategy.
    GET /api/strategies/changes/?strategy=<id>
    """
    serializer_class = StrategyChangeLogSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = (
            StrategyChangeLog.objects
            .select_related("strategy", "changed_by")
            .all()
        )
        strategy_id = self.request.query_params.get("strategy")
        if strategy_id:
            qs = qs.filter(strategy_id=strategy_id)

        # Restrict to strategies owned by this user (unless staff)
        if not user.is_staff:
            qs = qs.filter(strategy__owner=user)

        return qs


# =============================================================================
# Windows Agent Strategy Assignment (MVP)
# =============================================================================


def _get_agent_headers():
    """
    Build headers for Windows agent requests.
    Raises ValueError if token is not configured.
    """
    token = getattr(settings, "GUVFX_WINDOWS_AGENT_TOKEN", "")
    if not token:
        raise ValueError("GUVFX_WINDOWS_AGENT_TOKEN is not configured.")
    return {
        "X-GuvFX-Agent-Token": token,
        "Content-Type": "application/json",
    }


def _get_agent_base_url():
    """Get the Windows agent base URL from settings."""
    return getattr(settings, "GUVFX_WINDOWS_AGENT_BASE_URL", "http://10.50.0.2:8787")


class WindowsStrategyAssignView(APIView):
    """
    POST /api/strategies/windows/assign/

    Assigns a strategy to an MT5 instance via the Windows agent.

    Request body:
    {
        "strategy_id": <int>,
        "account_id": <int>,
        "username": "<str>",
        "datadir": "<str>",
        "magic_number": <int>  (optional, uses strategy.magic_number if not provided)
    }
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        user = request.user
        data = request.data

        # Validate required fields
        strategy_id = data.get("strategy_id")
        account_id = data.get("account_id")
        username = data.get("username")
        datadir = data.get("datadir", "")

        if not strategy_id:
            return Response(
                {"ok": False, "error": "strategy_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not account_id:
            return Response(
                {"ok": False, "error": "account_id is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not username:
            return Response(
                {"ok": False, "error": "username is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Fetch strategy
        try:
            strategy = Strategy.objects.get(id=strategy_id)
        except Strategy.DoesNotExist:
            raise NotFound(f"Strategy with id {strategy_id} not found.")

        # Fetch account
        try:
            account = TradingAccount.objects.get(id=account_id)
        except TradingAccount.DoesNotExist:
            raise NotFound(f"TradingAccount with id {account_id} not found.")

        # Ownership check (non-staff must own both)
        if not user.is_staff:
            if strategy.owner != user:
                raise PermissionDenied("You do not own this strategy.")
            if account.user != user:
                raise PermissionDenied("You do not own this account.")

        # Determine magic number
        magic_number = data.get("magic_number")
        if magic_number is None:
            magic_number = strategy.magic_number
        if magic_number is None:
            # Generate a default magic number if none exists
            magic_number = strategy.id * 1000

        # Parse symbol_universe into list
        symbols = []
        if strategy.symbol_universe:
            symbols = [s.strip() for s in strategy.symbol_universe.split(",") if s.strip()]

        # Build agent payload
        agent_payload = {
            "username": username,
            "datadir": datadir,
            "account_id": account_id,
            "strategy_id": strategy_id,
            "strategy": {
                "name": strategy.name,
                "symbols": symbols,
                "timeframe": strategy.timeframe or "",
                "magic": magic_number,
            },
        }

        # Call agent
        try:
            headers = _get_agent_headers()
        except ValueError as e:
            return Response(
                {"ok": False, "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        agent_url = f"{_get_agent_base_url()}/mt5/strategy/assign"

        try:
            resp = requests.post(agent_url, json=agent_payload, headers=headers, timeout=10)
        except requests.RequestException as e:
            logger.error(f"Windows agent assign request failed: {e}")
            return Response(
                {"ok": False, "error": f"Agent connection failed: {e}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if resp.status_code != 200:
            return Response(
                {"ok": False, "error": f"Agent returned status {resp.status_code}", "detail": resp.text},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        try:
            agent_resp = resp.json()
        except ValueError:
            return Response(
                {"ok": False, "error": "Agent returned invalid JSON"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        if not agent_resp.get("ok"):
            return Response(
                {"ok": False, "error": agent_resp.get("error", "Unknown agent error")},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Return success with agent response fields
        return Response({
            "ok": True,
            "request_path": agent_resp.get("request_path"),
            "launch": agent_resp.get("launch"),
            "strategy_id": strategy_id,
            "account_id": account_id,
        })
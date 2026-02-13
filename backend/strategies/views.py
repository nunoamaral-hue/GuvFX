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

from .models import Strategy, StrategyAssignment, StrategyChangeLog
from .serializers import (
    StrategySerializer,
    StrategyAssignmentSerializer,
    StrategyChangeLogSerializer,
)
from backtests.models import BacktestConfig, BacktestRun
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
}

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

    @action(detail=False, methods=["post"], url_path="marketplace/assign")
    def marketplace_assign(self, request):
        user = request.user

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

        with transaction.atomic():
            # 1. Lock and fetch existing active assignment for this account
            existing = (
                StrategyAssignment.objects
                .select_for_update()
                .filter(account=account, is_active=True)
                .first()
            )

            # 2. Try to find an existing Strategy for this user with the marketplace template name
            if "owner" in allowed_fields:
                existing_strategy = (
                    Strategy.objects
                    .filter(owner=user, name=template_name)
                    .order_by("-id")
                    .first()
                )
            else:
                # Fallback for staff or if owner field isn't present
                existing_strategy = (
                    Strategy.objects
                    .filter(name=template_name)
                    .order_by("-id")
                    .first()
                )

            # 3. Idempotency: if assignment exists and already points to this template, return early
            if existing and getattr(existing.strategy, "name", "") == template_name:
                return Response(
                    {
                        "ok": True,
                        "marketplace_strategy_id": marketplace_strategy_id,
                        "strategy_id": existing.strategy_id,
                        "assignment_id": existing.id,
                        "strategy_name": getattr(existing.strategy, "name", ""),
                        "account_id": account.id,
                        "already_assigned": True,
                    },
                    status=status.HTTP_200_OK,
                )

            # 4/5. Choose strategy_to_use: prefer existing_strategy, else create new
            if existing_strategy:
                strategy_to_use = existing_strategy
            else:
                strategy_to_use = Strategy.objects.create(**create_kwargs)

            # 4. If assignment exists but points to different strategy, update it
            if existing:
                existing.strategy = strategy_to_use
                existing.is_active = True
                existing.save(update_fields=["strategy", "is_active", "updated_at"])
                assignment = existing
            else:
                # 5. No assignment exists, create one
                assignment = StrategyAssignment.objects.create(
                    strategy=strategy_to_use,
                    account=account,
                    is_active=True,
                )

            # Deactivate other active assignments on the same MT5 instance
            if assignment.account.mt5_instance_id:
                StrategyAssignment.objects.filter(
                    account__mt5_instance_id=assignment.account.mt5_instance_id,
                    account__user_id=assignment.account.user_id,
                    is_active=True,
                ).exclude(id=assignment.id).update(is_active=False)

        # 6. Return payload
        return Response(
            {
                "ok": True,
                "marketplace_strategy_id": marketplace_strategy_id,
                "strategy_id": strategy_to_use.id,
                "assignment_id": assignment.id,
                "strategy_name": getattr(strategy_to_use, "name", ""),
                "account_id": account.id,
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
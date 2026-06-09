import logging
import requests

from django.conf import settings
from django.utils import timezone
from rest_framework import permissions, viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, NotFound

from .models import BacktestConfig, BacktestExecution, BacktestJob, BacktestRun, PromotionCandidate, WindowsBacktestJob
from .serializers import (
    BacktestArtifactMetadataSerializer,
    BacktestConfigSerializer,
    BacktestResultsResponseSerializer,
    BacktestRunRequestSerializer,
    BacktestRunResponseSerializer,
    BacktestRunSerializer,
    BacktestStatusResponseSerializer,
    ExecutionCandidateResponseSerializer,
    PromotionCandidateReviewSerializer,
    PromotionCandidateSerializer,
    WindowsBacktestRunRequestSerializer,
    WindowsBacktestJobSerializer,
    AIBacktestRecommendationRequestSerializer,
)
from .services import (
    create_backtest_request,
    create_promotion_candidate_for_execution_for_user,
    get_backtest_status_for_user,
    get_backtest_results_for_user,
    list_backtest_artifacts_for_user,
    review_promotion_candidate,
    create_execution_candidate,
    PromotionNotApprovedError,
)
from admin_ops.permissions import IsSuperOrOpsAdmin
from strategies.models import Strategy
from trading.models import TradingAccount
from billing.enforcement import require_entitlement
from core.audit import (
    log_backtest_config_created,
    log_backtest_job_created,
    log_backtest_run_created,
    log_backtest_status_viewed,
    log_backtest_results_viewed,
    log_backtest_artifacts_viewed,
    log_backtests_processed,
)

logger = logging.getLogger(__name__)


# =========================================================================
# Packet B — B5: Canonical backtest API views
# =========================================================================


class BacktestJobRunView(APIView):
    """
    POST /api/backtests/jobs/run/

    Create a BacktestJob + initial BacktestExecution in queued state.
    Respects entitlements and ownership.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = BacktestRunRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user

        # Entitlement gate
        require_entitlement(user, "can_run_backtests")

        # Ownership check: non-staff must own the strategy
        strategy = Strategy.objects.get(id=data["strategy_id"])
        if not user.is_staff and strategy.owner != user:
            raise PermissionDenied("You can only run backtests on your own strategies.")

        # Service layer: create job + execution
        job, execution = create_backtest_request(
            user=user,
            strategy=strategy,
            symbol=data["symbol"],
            timeframe=data["timeframe"],
            start_date=data["start_date"],
            end_date=data["end_date"],
            parameter_set=data.get("parameter_set"),
            data_source=data.get("data_source", ""),
        )

        # Audit
        log_backtest_job_created(
            request,
            job_id=job.pk,
            execution_id=execution.pk,
            strategy_id=strategy.pk,
            symbol=data["symbol"],
        )

        response_data = {
            "backtest_job_id": job.pk,
            "backtest_execution_id": execution.pk,
            "run_identifier": execution.run_identifier,
            "status": job.status,
            "requested_at": job.requested_at,
        }
        response_serializer = BacktestRunResponseSerializer(response_data)
        return Response(response_serializer.data, status=status.HTTP_201_CREATED)


class BacktestJobStatusView(APIView):
    """
    GET /api/backtests/jobs/{id}/status/

    Return safe status/lifecycle information for a BacktestJob.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, job_id):
        try:
            data = get_backtest_status_for_user(job_id, request.user)
        except BacktestJob.DoesNotExist:
            raise NotFound("Backtest job not found.")

        log_backtest_status_viewed(request, job_id=job_id)

        serializer = BacktestStatusResponseSerializer(data)
        return Response(serializer.data)


class BacktestJobResultsView(APIView):
    """
    GET /api/backtests/jobs/{id}/results/

    Return BacktestSummary + safe execution result metadata.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, job_id):
        try:
            data = get_backtest_results_for_user(job_id, request.user)
        except BacktestJob.DoesNotExist:
            raise NotFound("Backtest job not found.")

        log_backtest_results_viewed(request, job_id=job_id)

        serializer = BacktestResultsResponseSerializer(data)
        return Response(serializer.data)


class BacktestJobArtifactsView(APIView):
    """
    GET /api/backtests/jobs/{id}/artifacts/

    Return safe artifact metadata listing for a BacktestJob.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, job_id):
        try:
            artifacts = list_backtest_artifacts_for_user(job_id, request.user)
        except BacktestJob.DoesNotExist:
            raise NotFound("Backtest job not found.")

        log_backtest_artifacts_viewed(request, job_id=job_id)

        serializer = BacktestArtifactMetadataSerializer(artifacts, many=True)
        return Response(serializer.data)


class BacktestPromoteView(APIView):
    """
    POST /api/backtests/{execution_id}/promote/

    Idempotently create a PromotionCandidate for an execution.
    Returns existing candidate if already present.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, execution_id):
        try:
            candidate, created = create_promotion_candidate_for_execution_for_user(
                execution_id, request.user, request=request
            )
        except BacktestExecution.DoesNotExist:
            raise NotFound("Backtest execution not found.")

        serializer = PromotionCandidateSerializer(candidate)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PromotionCandidateReviewView(APIView):
    """
    POST /api/backtests/candidates/{id}/review/

    Apply a review decision (approved/rejected) to a PromotionCandidate.
    Restricted to ops_admin and super_admin roles.
    """

    permission_classes = [IsSuperOrOpsAdmin]

    def post(self, request, candidate_id):
        serializer = PromotionCandidateReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            candidate = review_promotion_candidate(
                candidate_id=candidate_id,
                decision=serializer.validated_data["decision"],
                notes=serializer.validated_data.get("notes", ""),
                reviewer=request.user,
                request=request,
            )
        except PromotionCandidate.DoesNotExist:
            raise NotFound("Promotion candidate not found.")

        output = PromotionCandidateSerializer(candidate)
        return Response(output.data)


class ExecutionCandidateStageView(APIView):
    """
    POST /api/backtests/candidates/{id}/stage/

    Stage an approved PromotionCandidate as an ExecutionCandidate.
    Idempotent: returns existing ExecutionCandidate if already staged.
    Restricted to ops_admin and super_admin roles.

    Metadata-only — does NOT create ExecutionJobs, trigger workers,
    or modify the execution pipeline.
    """

    permission_classes = [IsSuperOrOpsAdmin]

    def post(self, request, candidate_id):
        try:
            promo = PromotionCandidate.objects.get(pk=candidate_id)
        except PromotionCandidate.DoesNotExist:
            raise NotFound("Promotion candidate not found.")

        try:
            ec, created = create_execution_candidate(
                promotion_candidate=promo,
                actor_user=request.user,
                request=request,
            )
        except PromotionNotApprovedError as e:
            return Response(
                {"detail": str(e)},
                status=status.HTTP_409_CONFLICT,
            )

        serializer = ExecutionCandidateResponseSerializer(ec)
        return Response(
            serializer.data,
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


# =========================================================================
# Legacy views (existing — unchanged)
# =========================================================================


class BacktestConfigViewSet(viewsets.ModelViewSet):
    """
    CRUD for backtest configurations.

    - Non-staff users see only their own configs.
    - Staff users see all configs.
    """
    serializer_class = BacktestConfigSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = (
            BacktestConfig.objects
            .select_related("owner", "strategy", "reference_account")
            .all()
        )
        if not user.is_staff:
            qs = qs.filter(owner=user)
        return qs

    def perform_create(self, serializer):
        instance = serializer.save()  # BacktestConfigSerializer.create() sets owner
        log_backtest_config_created(self.request, instance)


class BacktestRunViewSet(viewsets.ModelViewSet):
    """
    Manage backtest runs.

    For now:
    - Create() creates a PENDING run snapshotting config parameters.
    - Later a worker/engine will pick up PENDING runs and update status/metrics.
    """

    queryset = (
        BacktestRun.objects
        .select_related("config", "config__owner", "config__strategy")
        .all()
        .order_by("-started_at", "-id")
    )
    serializer_class = BacktestRunSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        qs = super().get_queryset()
        if not user.is_staff:
            qs = qs.filter(config__owner=user)

        # Filter by strategy ID
        strategy_id = self.request.query_params.get("strategy")
        if strategy_id:
            qs = qs.filter(config__strategy_id=strategy_id)

        # Filter by config ID (supports both 'config' and 'config_id' params)
        config_id = self.request.query_params.get("config") or self.request.query_params.get("config_id")
        if config_id:
            qs = qs.filter(config_id=config_id)

        return qs

    def perform_create(self, serializer):
        user = self.request.user
        config = serializer.validated_data["config"]

        # Entitlement gate: user must have can_run_backtests
        require_entitlement(user, "can_run_backtests")

        # Ownership check: non-staff can only create runs on their own configs
        if (not user.is_staff) and config.owner != user:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("You can only run backtests on your own configs.")

        # Snapshot current config parameters into the run
        run = BacktestRun.objects.create(
            config=config,
            symbol=config.symbol,
            timeframe=config.timeframe,
            date_from=config.date_from,
            date_to=config.date_to,
            initial_balance=config.initial_balance,
            status=BacktestRun.STATUS_PENDING,
            created_at=timezone.now(),  # likely redundant, but explicit
        )

        # If you wanted a fake/dummy "instant completion" for now, you could update here.
        # For now we leave it as PENDING to be picked up by a future worker.

        # Audit log
        log_backtest_run_created(self.request, run)

        # Ensure serializer instance is set for response rendering
        serializer.instance = run


class ProcessPendingBacktestsView(APIView):
    """
    POST /api/backtests/process-pending/

    Processes all PENDING BacktestRun objects using the real MT5-data
    backtesting engine (EMA crossover strategy).

    Fetches OHLC bars from the MT5 signal bridge, runs a deterministic
    simulation, and stores metrics + equity curve + trade list.

    No live execution.  No ExecutionJob created.  No MT5 orders sent.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        pending_runs = (
            BacktestRun.objects
            .filter(status=BacktestRun.STATUS_PENDING)
            .select_related("config", "config__owner", "config__strategy")
        )

        if not pending_runs.exists():
            return Response(
                {"processed_runs": 0, "processed_at": timezone.now()},
                status=200,
            )

        processed_count = 0

        for run in pending_runs:
            # Mark as RUNNING
            run.status = BacktestRun.STATUS_RUNNING
            run.started_at = timezone.now()
            run.save(update_fields=["status", "started_at"])

            try:
                metrics, equity_curve = self._run_real_backtest(run)
                run.status = BacktestRun.STATUS_COMPLETED
                run.error_message = ""
            except Exception as e:
                metrics = {"error": str(e), "demo": False}
                equity_curve = []
                run.status = BacktestRun.STATUS_FAILED
                run.error_message = str(e)[:500]

            run.finished_at = timezone.now()
            run.metrics = metrics
            run.equity_curve = equity_curve

            run.save(
                update_fields=[
                    "status",
                    "finished_at",
                    "metrics",
                    "equity_curve",
                    "error_message",
                ]
            )

            processed_count += 1

        # Audit log
        if processed_count > 0:
            log_backtests_processed(request, processed_count)

        return Response(
            {
                "processed_runs": processed_count,
                "processed_at": timezone.now(),
            },
            status=200,
        )

    def _run_real_backtest(self, run: BacktestRun):
        """
        Run real MT5-data backtest using the engine.

        Fetches OHLC bars from the signal bridge, runs EMA crossover
        simulation, returns (metrics_dict, equity_curve_list).
        """
        from backtests.engine import fetch_bars, run_backtest, StrategyParams

        symbol = run.symbol or run.config.symbol
        timeframe = run.timeframe or run.config.timeframe
        initial_balance = float(run.initial_balance)

        # Strategy parameters from config or defaults
        strategy = run.config.strategy
        risk_pct = float(run.config.risk_per_trade_pct or strategy.risk_per_trade_pct or 1)
        lots = float(getattr(strategy, "fixed_lot_size", None) or 0.01)

        params = StrategyParams(
            fast_ema=20,
            slow_ema=50,
            sl_pips=30.0,
            tp_pips=60.0,
            lots=lots,
            spread_pips=1.5,
        )

        # Fetch bars — use batch fetching for larger counts
        bar_count = 500  # default; could be configurable per config later
        bars = fetch_bars(symbol, timeframe, count=bar_count)

        if not bars:
            raise RuntimeError(f"No bars returned for {symbol} {timeframe}")

        result = run_backtest(
            bars, params,
            symbol=symbol,
            timeframe=timeframe,
            initial_balance=initial_balance,
        )

        if result.error:
            raise RuntimeError(result.error)

        # Build metrics dict (frontend-compatible format)
        dq = result.data_quality
        metrics = {
            **result.metrics,
            "equity_curve": result.equity_curve,
            # Data source + quality
            "data_source": "MT5",
            "data_quality": {
                "status": dq.status,
                "bar_count": dq.bar_count,
                "first_bar_time": dq.first_bar_time,
                "last_bar_time": dq.last_bar_time,
                "notes": dq.notes,
            },
            # Strategy params snapshot
            "strategy_params": {
                "fast_ema": params.fast_ema,
                "slow_ema": params.slow_ema,
                "sl_pips": params.sl_pips,
                "tp_pips": params.tp_pips,
                "lots": params.lots,
                "spread_pips": params.spread_pips,
            },
            # Reconciliation metadata
            "reconciliation": result.reconciliation,
            # Display metadata
            "bars_count": result.bars_count,
            "date_range": f"{result.start_date} to {result.end_date}",
            "mode": "research",
            "mode_label": "Research Mode Backtest",
            "mode_disclaimer": (
                "Results are simulated using MT5 OHLC data and GuvFX execution assumptions. "
                "They may differ from MT5 Strategy Tester or live execution."
            ),
            # Trade list
            "trades": [
                {
                    "trade_number": t.trade_number,
                    "side": t.side,
                    "entry_time": t.entry_time,
                    "exit_time": t.exit_time,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "sl": t.sl,
                    "tp": t.tp,
                    "lots": t.lots,
                    "pnl": t.pnl,
                    "exit_reason": t.exit_reason,
                }
                for t in result.trades
            ],
            "demo": False,
        }

        return metrics, result.equity_curve

    def _generate_dummy_results(self, run: BacktestRun):
        """
        Legacy: Deterministic demo result generator seeded by (config.id, run.id).
        Produces realistic equity curve with drawdown phases and timestamps.
        Marked as demo data for compliance.
        """
        import random
        import math
        from datetime import datetime, timedelta

        # Seed for determinism: same config+run always produces same data
        seed = (run.config_id * 1000) + run.id
        rng = random.Random(seed)

        initial_equity = float(run.initial_balance)

        # Seeded parameters for variety across runs
        base_return = rng.uniform(-5.0, 25.0)  # Range: loss to gain
        volatility = rng.uniform(0.5, 2.5)  # Daily % volatility
        win_rate_pct = rng.uniform(35.0, 65.0)
        num_trades = rng.randint(40, 200)

        # Generate 60 equity points (e.g., 60 days of trading)
        num_points = 60
        equity_curve = []

        # Parse date range for timestamps
        try:
            start_date = datetime.strptime(str(run.date_from), "%Y-%m-%d")
            end_date = datetime.strptime(str(run.date_to), "%Y-%m-%d")
            days_span = max((end_date - start_date).days, num_points)
        except (ValueError, TypeError):
            start_date = datetime(2024, 1, 1)
            days_span = 90

        day_step = max(1, days_span // num_points)

        equity = initial_equity
        running_max = initial_equity
        max_drawdown = 0.0

        for i in range(num_points):
            # Calculate timestamp
            point_date = start_date + timedelta(days=i * day_step)
            timestamp = point_date.strftime("%Y-%m-%dT09:00:00Z")

            # Random walk with trend toward base_return
            trend = (base_return / 100.0) / num_points
            noise = rng.gauss(0, volatility / 100.0)

            # Add occasional larger moves (drawdowns or rallies)
            if rng.random() < 0.1:
                noise *= 2.5

            daily_return = trend + noise
            equity *= (1 + daily_return)

            # Track max drawdown
            if equity > running_max:
                running_max = equity
            current_dd = ((running_max - equity) / running_max) * 100
            if current_dd > max_drawdown:
                max_drawdown = current_dd

            equity_curve.append({
                "timestamp": timestamp,
                "equity": round(equity, 2),
                "step": i,
            })

        final_equity = equity_curve[-1]["equity"]
        total_return_pct = ((final_equity - initial_equity) / initial_equity) * 100

        # Clamp max_drawdown to 0-100 range for safety
        max_drawdown = min(max(max_drawdown, 0.0), 100.0)

        metrics = {
            "total_return_pct": round(total_return_pct, 2),
            "max_drawdown_pct": round(max_drawdown, 2),
            "win_rate_pct": round(win_rate_pct, 1),
            "num_trades": num_trades,
            "initial_balance": initial_equity,
            "final_balance": round(final_equity, 2),
            # Include equity_curve in metrics for frontend compatibility
            "equity_curve": equity_curve,
            # Demo flag for compliance
            "demo": True,
            "notes": "Demo data. For illustrative purposes only. Not real execution.",
        }

        return metrics, equity_curve


# =============================================================================
# Windows Agent Backtest Views (MVP)
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


class WindowsBacktestRunView(APIView):
    """
    POST /api/backtests/windows/run/

    Creates a new backtest job on the Windows agent and persists it locally.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = WindowsBacktestRunRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user

        # Fetch strategy and account
        strategy = Strategy.objects.filter(id=data["strategy_id"]).first()
        account = TradingAccount.objects.filter(id=data["account_id"]).first()

        # Ownership check (non-staff must own both)
        if not user.is_staff:
            if strategy and strategy.owner != user:
                raise PermissionDenied("You do not own this strategy.")
            if account and account.user != user:
                raise PermissionDenied("You do not own this account.")

        # Build agent request payload
        agent_payload = {
            "username": data["username"],
            "datadir": data.get("datadir", ""),
            "account_id": data["account_id"],
            "strategy_id": data["strategy_id"],
            "symbol": data["symbol"],
            "timeframe": data["timeframe"],
            "date_from": str(data["date_from"]),
            "date_to": str(data["date_to"]),
            "deposit": float(data["deposit"]),
            "leverage": data["leverage"],
            "mode": data.get("mode", "real_ticks"),
        }

        # Call agent
        try:
            headers = _get_agent_headers()
        except ValueError as e:
            return Response(
                {"ok": False, "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        agent_url = f"{_get_agent_base_url()}/mt5/backtest/run"

        try:
            resp = requests.post(agent_url, json=agent_payload, headers=headers, timeout=10)
        except requests.RequestException as e:
            logger.error(f"Windows agent request failed: {e}")
            return Response(
                {"ok": False, "error": f"Agent connection failed: {e}"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Agent may return 202 Accepted for queued jobs (still a success)
        if resp.status_code not in (200, 202):
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

        job_id = agent_resp.get("job_id")
        if not job_id:
            return Response(
                {"ok": False, "error": "Agent did not return job_id"},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Persist job locally
        job = WindowsBacktestJob.objects.create(
            job_id=job_id,
            owner=user,
            strategy=strategy,
            account=account,
            username=data["username"],
            datadir=data.get("datadir", ""),
            symbol=data["symbol"],
            timeframe=data["timeframe"],
            date_from=data["date_from"],
            date_to=data["date_to"],
            deposit=data["deposit"],
            leverage=data["leverage"],
            mode=data.get("mode", "real_ticks"),
            state=WindowsBacktestJob.STATE_QUEUED,
        )

        return Response({
            "ok": True,
            "job_id": job.job_id,
            "state": job.state,
        }, status=status.HTTP_201_CREATED)


class WindowsBacktestStatusView(APIView):
    """
    GET /api/backtests/windows/status/?job_id=<id>

    Polls the Windows agent for job status and updates local record.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        job_id = request.query_params.get("job_id")
        if not job_id:
            return Response(
                {"ok": False, "error": "job_id query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user

        # Look up job
        try:
            job = WindowsBacktestJob.objects.get(job_id=job_id)
        except WindowsBacktestJob.DoesNotExist:
            raise NotFound(f"Job with job_id '{job_id}' not found.")

        # Ownership check
        if not user.is_staff and job.owner != user:
            raise PermissionDenied("You do not have access to this job.")

        # Call agent
        try:
            headers = _get_agent_headers()
        except ValueError as e:
            return Response(
                {"ok": False, "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        agent_url = f"{_get_agent_base_url()}/mt5/backtest/status"

        try:
            resp = requests.get(agent_url, params={"job_id": job_id}, headers=headers, timeout=10)
        except requests.RequestException as e:
            logger.error(f"Windows agent status request failed: {e}")
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

        # Update local job state
        # Prefer agent_resp["status"]["state"], fallback to agent_resp["state"]
        agent_state = None
        if isinstance(agent_resp.get("status"), dict):
            agent_state = agent_resp["status"].get("state", "").lower()
        if not agent_state:
            agent_state = agent_resp.get("state", "").lower()

        if agent_state in ["queued", "running", "completed", "failed"]:
            job.state = agent_state
        job.status_json = agent_resp
        job.save(update_fields=["state", "status_json", "updated_at"])

        return Response({
            "ok": True,
            "job_id": job.job_id,
            "state": job.state,
            "status_json": job.status_json,
        })


class WindowsBacktestResultView(APIView):
    """
    GET /api/backtests/windows/result/?job_id=<id>

    Fetches the backtest result from the Windows agent and stores it.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        job_id = request.query_params.get("job_id")
        if not job_id:
            return Response(
                {"ok": False, "error": "job_id query parameter is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = request.user

        # Look up job
        try:
            job = WindowsBacktestJob.objects.get(job_id=job_id)
        except WindowsBacktestJob.DoesNotExist:
            raise NotFound(f"Job with job_id '{job_id}' not found.")

        # Ownership check
        if not user.is_staff and job.owner != user:
            raise PermissionDenied("You do not have access to this job.")

        # Call agent
        try:
            headers = _get_agent_headers()
        except ValueError as e:
            return Response(
                {"ok": False, "error": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        agent_url = f"{_get_agent_base_url()}/mt5/backtest/result"

        try:
            resp = requests.get(agent_url, params={"job_id": job_id}, headers=headers, timeout=10)
        except requests.RequestException as e:
            logger.error(f"Windows agent result request failed: {e}")
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

        # Update local job with result
        job.result_json = agent_resp

        # Read state from agent_resp["result"]["state"] if available, else fallback
        agent_state = None
        if isinstance(agent_resp.get("result"), dict):
            agent_state = agent_resp["result"].get("state", "").lower()
        if not agent_state:
            agent_state = agent_resp.get("state", "").lower()

        if agent_state in ["queued", "running", "completed", "failed"]:
            job.state = agent_state
        elif agent_resp.get("ok"):
            # Fallback: mark completed if result indicates success
            job.state = WindowsBacktestJob.STATE_COMPLETED

        job.save(update_fields=["state", "result_json", "updated_at"])

        return Response({
            "ok": True,
            "job_id": job.job_id,
            "state": job.state,
            "result_json": job.result_json,
        })


class AIBacktestRecommendationsView(APIView):
    """
    POST /api/ai/backtest-recommendations/

    Computes minimal AI recommendations based on backtest result.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        serializer = AIBacktestRecommendationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job_id = serializer.validated_data["job_id"]

        user = request.user

        # Look up job
        try:
            job = WindowsBacktestJob.objects.get(job_id=job_id)
        except WindowsBacktestJob.DoesNotExist:
            raise NotFound(f"Job with job_id '{job_id}' not found.")

        # Ownership check
        if not user.is_staff and job.owner != user:
            raise PermissionDenied("You do not have access to this job.")

        # Check if result exists
        if not job.result_json:
            return Response(
                {"ok": False, "error": "No result_json available for this job. Fetch result first."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Compute recommendations
        result = job.result_json
        recommendations = self._compute_recommendations(result)

        return Response({
            "ok": True,
            "job_id": job.job_id,
            **recommendations,
        })

    def _compute_recommendations(self, result: dict) -> dict:
        """
        Compute minimal AI recommendations from backtest result.
        """
        reasons = []

        # Extract deals if present
        deals = result.get("deals", [])
        num_deals = len(deals)

        # Calculate net PnL from deals
        net_pnl = 0.0
        if deals:
            for deal in deals:
                # Try common field names for profit
                profit = deal.get("profit", 0) or deal.get("pnl", 0) or 0
                try:
                    net_pnl += float(profit)
                except (TypeError, ValueError):
                    pass

        # Extract balance/equity if present
        balance = result.get("balance")
        equity = result.get("equity")
        initial_deposit = result.get("deposit") or result.get("initial_deposit")

        # Determine go_live recommendation
        go_live = False
        confidence = "low"

        if net_pnl > 0 and num_deals >= 3:
            go_live = True
            confidence = "medium"
            reasons.append(f"Positive net PnL of {net_pnl:.2f}")
            reasons.append(f"Sufficient trade count ({num_deals} deals)")
        else:
            if net_pnl <= 0:
                reasons.append(f"Net PnL is not positive ({net_pnl:.2f})")
            if num_deals < 3:
                reasons.append(f"Insufficient trade count ({num_deals} deals, need at least 3)")

        # Additional confidence boost if we have more trades
        if num_deals >= 10 and net_pnl > 0:
            confidence = "high"
            reasons.append(f"Good sample size with {num_deals} trades")

        # Suggested risk per trade
        suggested_risk_per_trade_pct = 0.5 if go_live else 0.25

        return {
            "go_live": go_live,
            "suggested_risk_per_trade_pct": suggested_risk_per_trade_pct,
            "confidence": confidence,
            "reasons": reasons,
            "metrics": {
                "net_pnl": net_pnl,
                "num_deals": num_deals,
                "balance": balance,
                "equity": equity,
                "initial_deposit": initial_deposit,
            },
        }
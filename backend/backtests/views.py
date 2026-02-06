import logging
import requests

from django.conf import settings
from django.utils import timezone
from rest_framework import permissions, viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.exceptions import PermissionDenied, NotFound

from .models import BacktestConfig, BacktestRun, WindowsBacktestJob
from .serializers import (
    BacktestConfigSerializer,
    BacktestRunSerializer,
    WindowsBacktestRunRequestSerializer,
    WindowsBacktestJobSerializer,
    AIBacktestRecommendationRequestSerializer,
)
from strategies.models import Strategy
from trading.models import TradingAccount

logger = logging.getLogger(__name__)


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
        serializer.save()  # BacktestConfigSerializer.create() sets owner


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

        # Ensure serializer instance is set for response rendering
        serializer.instance = run


class ProcessPendingBacktestsView(APIView):
    """
    POST /api/backtests/process-pending/

    Processes all PENDING BacktestRun objects using the same dummy logic
    as the management command, and returns how many runs were updated.
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

            # Generate dummy metrics
            metrics, equity_curve = self._generate_dummy_results(run)

            # Mark as COMPLETED
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

            processed_count += 1

        return Response(
            {
                "processed_runs": processed_count,
                "processed_at": timezone.now(),
            },
            status=200,
        )

    def _generate_dummy_results(self, run: BacktestRun):
        """
        Deterministic demo result generator seeded by (config.id, run.id).
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
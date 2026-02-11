import os

from django.conf import settings as django_settings
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    ExecutionJob,
    DEMO_ALLOWED_SYMBOLS,
    DEMO_FIXED_LOT_SIZE,
    DEMO_MAX_TRADES_PER_DAY,
)
from .serializers import (
    ExecutionJobSerializer,
    OpenTradeJobRequestSerializer,
    DemoTradeJobRequestSerializer,
)
from .services import OpenTradeParams, create_open_trade_job
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount
from core.audit import (
    log_execution_attempt,
    log_execution_job_created,
    log_execution_job_claimed,
    log_execution_job_completed,
)

class IsAuthenticatedOrWorkerToken(permissions.BasePermission):
    """
    Allow access if:
    - the request has an authenticated user, OR
    - the request provides the correct X-Worker-Token header.
    """

    def has_permission(self, request, view):
        # Normal user auth path
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return True

        # Worker token path
        expected_token = os.getenv("MT5_WORKER_TOKEN")
        provided_token = request.headers.get("X-Worker-Token")

        if expected_token and provided_token == expected_token:
            return True

        return False

class ExecutionJobViewSet(viewsets.ModelViewSet):
    serializer_class = ExecutionJobSerializer
    permission_classes = [IsAuthenticatedOrWorkerToken]

    def get_queryset(self):
        qs = ExecutionJob.objects.select_related("account", "strategy", "assignment").all()
        request = self.request
        user = getattr(request, "user", None)

        expected_token = os.getenv("MT5_WORKER_TOKEN")
        provided_token = request.headers.get("X-Worker-Token")
        is_worker = (expected_token and provided_token == expected_token and (user is None or not user.is_authenticated))

        if not is_worker and user is not None and user.is_authenticated and not user.is_superuser:
            qs = qs.filter(account__user=user)

        account_id = request.query_params.get("account")
        if account_id:
            qs = qs.filter(account_id=account_id)

        strategy_id = request.query_params.get("strategy")
        if strategy_id:
            qs = qs.filter(strategy_id=strategy_id)

        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=["get"], url_path="next")
    def next_job(self, request):
        """
        Called by MT5 worker: claim the oldest PENDING job and mark it RUNNING.

        Query params:
        - worker_id: Identifier for the claiming worker (default: "mt5-worker")
        - account_id: Filter jobs by account ID
        - job_type: Filter by job type. If omitted, defaults to SYNC_POSITIONS only.
                    This prevents Linux ingest workers from claiming PLACE_TEST_ORDER jobs.
        """
        worker_id = request.query_params.get("worker_id", "mt5-worker")
        account_id = request.query_params.get("account_id")
        job_type = request.query_params.get("job_type")

        qs = ExecutionJob.objects.filter(status=ExecutionJob.Status.PENDING)

        # Job type filtering: explicit param or default to SYNC_POSITIONS
        if job_type:
            qs = qs.filter(job_type=job_type)
        else:
            # Default: only return SYNC_POSITIONS (backward compat for Linux ingest worker)
            qs = qs.filter(job_type=ExecutionJob.JobType.SYNC_POSITIONS)

        if account_id:
            qs = qs.filter(account_id=account_id)

        job = qs.order_by("created_at").first()

        if not job:
            # 204 No Content – no jobs available
            return Response({"detail": "no_jobs"}, status=204)

        job.status = ExecutionJob.Status.RUNNING
        job.worker_id = worker_id
        job.started_at = timezone.now()
        job.save(update_fields=["status", "worker_id", "started_at"])

        # Audit log
        log_execution_job_claimed(
            request=None,  # Worker request, not user request
            job_id=str(job.id),
            worker_id=worker_id,
            account_id=job.account_id,
        )

        serializer = self.get_serializer(job)
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="complete")
    def complete(self, request, pk=None):
        """
        Called by MT5 worker: mark job SUCCESS or FAILED and store result/error.
        """
        job = self.get_object()
        status_value = request.data.get("status")
        result = request.data.get("result", {})
        error_message = request.data.get("error_message", "")

        if status_value not in (
            ExecutionJob.Status.SUCCESS,
            ExecutionJob.Status.FAILED,
        ):
            return Response({"detail": "invalid status"}, status=400)

        job.status = status_value
        job.result = result
        job.error_message = error_message
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "result", "error_message", "finished_at"])

        # Audit log
        log_execution_job_completed(
            request=None,  # Worker request, not user request
            job_id=str(job.id),
            success=(status_value == ExecutionJob.Status.SUCCESS),
            account_id=job.account_id,
            result=result if status_value == ExecutionJob.Status.SUCCESS else None,
            error_message=error_message if status_value == ExecutionJob.Status.FAILED else None,
        )

        serializer = self.get_serializer(job)
        return Response(serializer.data)


class CreateOpenTradeJobView(APIView):
    """
    Dev/operational endpoint to create an OPEN_TRADE execution job.

    It resolves:
    - account (must belong to the authenticated user),
    - strategy (must be owned by the user),
    - strategy assignment (if one exists for this account+strategy),
    - effective risk_per_trade_pct (params override > assignment override > strategy default > 1%).
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, *args, **kwargs):
        serializer = OpenTradeJobRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user

        try:
            account = TradingAccount.objects.get(id=data["account"], user=user)
        except TradingAccount.DoesNotExist:
            return Response(
                {"detail": "Account not found or not owned by current user."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            strategy = Strategy.objects.get(id=data["strategy"], owner=user)
        except Strategy.DoesNotExist:
            return Response(
                {"detail": "Strategy not found or not owned by current user."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        assignment = (
            StrategyAssignment.objects.filter(
                account=account, strategy=strategy, is_active=True
            ).first()
        )

        params = OpenTradeParams(
            account=account,
            strategy=strategy,
            assignment=assignment,
            created_by=user,
            symbol=data["symbol"],
            direction=data["direction"],
            timeframe=data["timeframe"],
            entry_type=data["entry_type"],
            entry_price=data.get("entry_price"),
            sl_price=data["sl_price"],
            tp_price=data.get("tp_price"),
            risk_per_trade_pct=data.get("risk_per_trade_pct"),
            comment=data.get("comment", ""),
        )

        job = create_open_trade_job(params)
        response_data = ExecutionJobSerializer(job).data
        return Response(response_data, status=status.HTTP_201_CREATED)


class WorkerAccountCredentialsView(APIView):
    """
    Internal endpoint for MT5 workers to fetch broker credentials for a given TradingAccount.
    Protected by a shared worker token (MT5_WORKER_TOKEN).
    """

    authentication_classes: list = []
    permission_classes: list = []

    def get(self, request, account_id: int):
        expected_token = os.getenv("MT5_WORKER_TOKEN")
        provided_token = request.headers.get("X-Worker-Token")

        if not expected_token or provided_token != expected_token:
            raise PermissionDenied("Invalid worker token")

        try:
            account = TradingAccount.objects.get(pk=account_id, is_active=True)
        except TradingAccount.DoesNotExist:
            return Response(
                {"detail": "Account not found or inactive"},
                status=status.HTTP_404_NOT_FOUND,
            )

        data = {
            "id": account.id,
            "broker_name": account.broker_name,
            "login": account.account_number,
            "password": account.broker_password,
            "is_demo": account.is_demo,
        }
        return Response(data)


# =============================================================================
# Demo Trade Execution (Safety-First)
# =============================================================================
#
# This endpoint allows creating a minimal demo trade job with strict safety rails:
# - Demo accounts only (fail closed if account is not marked demo)
# - Allowlisted symbols only (EURUSD)
# - Fixed lot size: 0.01 only
# - Max 3 trades per day per account
# - Global kill switch respected
# =============================================================================


def _is_execution_globally_disabled() -> bool:
    """
    Check if execution is globally disabled via environment variable.
    This is the kill switch for all execution.
    """
    return os.getenv("GUVFX_EXECUTION_DISABLED", "").lower() in ("true", "1", "yes")


class CreateDemoTradeJobView(APIView):
    """
    POST /api/execution/demo-trade/

    Create a demo trade job with strict safety rails.

    Safety checks (all must pass):
    1. Global kill switch not engaged
    2. Account must be demo (is_demo=True)
    3. Account must be owned by authenticated user
    4. Strategy must be owned by authenticated user
    5. Strategy assignment must exist and be active
    6. Daily trade limit not exceeded (max 3 per account per day)
    7. Symbol must be in allowlist (EURUSD only)

    On success, creates a PLACE_TEST_ORDER job with:
    - symbol: EURUSD (hard-coded)
    - lots: 0.01 (hard-coded)
    - side: BUY (hard-coded for demo)
    - comment: GUVFX_DEMO_JOB:<job_id>
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        # =====================================================================
        # Safety Check 1: Global kill switch
        # =====================================================================
        if _is_execution_globally_disabled():
            return Response(
                {
                    "ok": False,
                    "error": "execution_disabled",
                    "message": "Execution is currently disabled globally.",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # =====================================================================
        # Validate request data
        # =====================================================================
        serializer = DemoTradeJobRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        user = request.user
        account_id = data["account_id"]
        strategy_id = data["strategy_id"]

        # =====================================================================
        # Safety Check 2 & 3: Account exists, is demo, owned by user
        # =====================================================================
        try:
            account = TradingAccount.objects.get(id=account_id, user=user)
        except TradingAccount.DoesNotExist:
            # Build diagnostic info (only in DEBUG mode)
            debug_info = {}
            if django_settings.DEBUG:
                # Check if account exists at all (without user filter)
                any_account = TradingAccount.objects.filter(id=account_id).first()
                if any_account:
                    debug_info = {
                        "debug": {
                            "account_exists": True,
                            "owner_id": any_account.user_id,
                            "request_user_id": user.id,
                            "owner_match": any_account.user_id == user.id,
                        }
                    }
                else:
                    debug_info = {
                        "debug": {
                            "account_exists": False,
                            "request_user_id": user.id,
                        }
                    }

            return Response(
                {
                    "ok": False,
                    "error": "account_not_found",
                    "message": "Account not found or not owned by you.",
                    **debug_info,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not account.is_demo:
            debug_info = {}
            if django_settings.DEBUG:
                debug_info = {
                    "debug": {
                        "account_id": account.id,
                        "is_demo": account.is_demo,
                        "is_active": account.is_active,
                        "broker_server_env": (
                            account.broker_server.environment
                            if account.broker_server else None
                        ),
                    }
                }
            return Response(
                {
                    "ok": False,
                    "error": "account_not_demo",
                    "message": "Demo trading is only available on demo accounts. "
                               "This account is not marked as demo.",
                    **debug_info,
                },
                status=status.HTTP_403_FORBIDDEN,
            )

        if not account.is_active:
            debug_info = {}
            if django_settings.DEBUG:
                debug_info = {
                    "debug": {
                        "account_id": account.id,
                        "is_demo": account.is_demo,
                        "is_active": account.is_active,
                    }
                }
            return Response(
                {
                    "ok": False,
                    "error": "account_inactive",
                    "message": "This account is not active.",
                    **debug_info,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # =====================================================================
        # Safety Check 4: Strategy exists and owned by user
        # =====================================================================
        try:
            strategy = Strategy.objects.get(id=strategy_id, owner=user)
        except Strategy.DoesNotExist:
            return Response(
                {
                    "ok": False,
                    "error": "strategy_not_found",
                    "message": "Strategy not found or not owned by you.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # =====================================================================
        # Safety Check 5: Active assignment exists
        # =====================================================================
        assignment = StrategyAssignment.objects.filter(
            account=account,
            strategy=strategy,
            is_active=True,
        ).first()

        if not assignment:
            return Response(
                {
                    "ok": False,
                    "error": "no_active_assignment",
                    "message": "No active strategy assignment found for this account/strategy pair. "
                               "Please assign the strategy to the account first.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        # =====================================================================
        # Safety Check 6: Daily trade limit
        # =====================================================================
        today_count = ExecutionJob.count_today_demo_trades(account_id)
        if today_count >= DEMO_MAX_TRADES_PER_DAY:
            return Response(
                {
                    "ok": False,
                    "error": "daily_limit_exceeded",
                    "message": f"Daily demo trade limit reached ({DEMO_MAX_TRADES_PER_DAY} trades per day). "
                               "Please try again tomorrow.",
                    "today_count": today_count,
                    "limit": DEMO_MAX_TRADES_PER_DAY,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # =====================================================================
        # Safety Check 7: Symbol allowlist (enforced regardless of input)
        # =====================================================================
        symbol = "EURUSD"  # Hard-coded for safety

        # =====================================================================
        # Create the execution job with safety-enforced parameters
        # =====================================================================
        job = ExecutionJob.objects.create(
            job_type=ExecutionJob.JobType.PLACE_TEST_ORDER,
            account=account,
            strategy=strategy,
            assignment=assignment,
            status=ExecutionJob.Status.PENDING,
            created_by=user,
            payload={
                "symbol": symbol,
                "lots": DEMO_FIXED_LOT_SIZE,
                "side": "BUY",  # Hard-coded for demo
                "comment": f"GUVFX_DEMO_JOB:{0}",  # Placeholder, will update below
                "magic": strategy.id,  # Use strategy ID as magic number
                "is_demo": True,
                "safety_rails": {
                    "max_daily": DEMO_MAX_TRADES_PER_DAY,
                    "fixed_lots": DEMO_FIXED_LOT_SIZE,
                    "allowed_symbols": DEMO_ALLOWED_SYMBOLS,
                },
            },
        )

        # Update comment with actual job ID
        job.payload["comment"] = f"GUVFX_DEMO_JOB:{job.id}"
        job.save(update_fields=["payload"])

        # Audit log
        log_execution_job_created(
            request=request,
            job_id=str(job.id),
            job_type=job.job_type,
            account_id=account.id,
            strategy_id=strategy.id,
            metadata={
                "symbol": symbol,
                "lots": DEMO_FIXED_LOT_SIZE,
                "side": "BUY",
                "is_demo": True,
                "today_count": today_count + 1,
            },
        )

        return Response(
            {
                "ok": True,
                "job_id": job.id,
                "status": job.status,
                "message": "Demo trade job created successfully. "
                           "The trade will be executed shortly.",
                "payload": {
                    "symbol": symbol,
                    "lots": DEMO_FIXED_LOT_SIZE,
                    "side": "BUY",
                },
                "daily_trades": {
                    "used": today_count + 1,
                    "limit": DEMO_MAX_TRADES_PER_DAY,
                },
            },
            status=status.HTTP_201_CREATED,
        )


# =============================================================================
# Execution Control Stubs (501 Not Implemented)
# =============================================================================
#
# These endpoints exist to:
# 1. Document the intended API surface for execution controls
# 2. Log any attempts to call them for security monitoring
# 3. Return 501 Not Implemented to indicate feature is not yet available
#
# POST-MVP: These will be implemented with proper execution pipeline.
# =============================================================================


class ExecutionEnableView(APIView):
    """
    POST /api/execution/enable/<account_id>/

    Enable execution for a specific account.

    STATUS: 501 Not Implemented
    POST-MVP: Will enable strategy execution on the specified account's MT5 terminal.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, account_id: int):
        # Log attempt
        log_execution_attempt(
            request,
            event_type="EXECUTION_ENABLE_ATTEMPT",
            account_id=str(account_id),
            reason="Feature not implemented. Execution controls are disabled in MVP.",
        )

        return Response(
            {
                "ok": False,
                "error": "not_implemented",
                "message": "Execution controls are not yet available. "
                           "This feature is planned for a future release.",
                "account_id": account_id,
            },
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )


class ExecutionDisableView(APIView):
    """
    POST /api/execution/disable/<account_id>/

    Disable execution for a specific account.

    STATUS: 501 Not Implemented
    POST-MVP: Will disable strategy execution and remove EA from terminal.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, account_id: int):
        # Log attempt
        log_execution_attempt(
            request,
            event_type="EXECUTION_DISABLE_ATTEMPT",
            account_id=str(account_id),
            reason="Feature not implemented. Execution controls are disabled in MVP.",
        )

        return Response(
            {
                "ok": False,
                "error": "not_implemented",
                "message": "Execution controls are not yet available. "
                           "This feature is planned for a future release.",
                "account_id": account_id,
            },
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )


class ExecutionKillAllView(APIView):
    """
    POST /api/execution/kill-all/

    Emergency kill switch - disable all execution globally.

    STATUS: 501 Not Implemented
    POST-MVP: Will immediately disable execution on all accounts.
    Requires admin/staff permissions.
    """

    permission_classes = [permissions.IsAdminUser]

    def post(self, request):
        # Log attempt (even though not implemented)
        log_execution_attempt(
            request,
            event_type="EXECUTION_KILL_ATTEMPT",
            account_id="global",
            reason="Feature not implemented. Kill switch is disabled in MVP.",
        )

        return Response(
            {
                "ok": False,
                "error": "not_implemented",
                "message": "Kill switch is not yet available. "
                           "This feature is planned for a future release.",
                "scope": "global",
            },
            status=status.HTTP_501_NOT_IMPLEMENTED,
        )

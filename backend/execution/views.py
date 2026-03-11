import os

from django.conf import settings as django_settings
from django.db import transaction
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
from .auth import authenticate_worker, authenticate_legacy_worker
from .services import OpenTradeParams, create_open_trade_job
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount
from core.audit import (
    log_execution_attempt,
    log_execution_job_created,
    log_execution_job_claimed,
    log_execution_job_completed,
    log_trades_sync_queued,
    log_worker_auth_success,
    log_worker_auth_failed,
)
from billing.enforcement import require_entitlement

class IsAuthenticatedOrWorkerToken(permissions.BasePermission):
    """
    Allow access if:
    - the request has an authenticated user, OR
    - the request provides valid X-Worker-Id + X-Worker-Secret headers
      (validated against WorkerIdentity), OR
    - the request provides the legacy X-Worker-Token header, routed through
      the ``legacy-worker`` WorkerIdentity row for full trust validation.

    On successful worker auth (either path) the resolved ``WorkerIdentity``
    is attached to ``request._worker_identity`` for downstream use.
    """

    def has_permission(self, request, view):
        # Normal user auth path
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            return True

        # WorkerIdentity path (preferred)
        worker_id = request.headers.get("X-Worker-Id")
        worker_secret = request.headers.get("X-Worker-Secret")
        if worker_id and worker_secret:
            try:
                worker = authenticate_worker(worker_id, worker_secret)
                request._worker_identity = worker
                log_worker_auth_success(request, worker_id=worker_id)
                return True
            except PermissionDenied:
                log_worker_auth_failed(request, worker_id=worker_id, reason="credentials")
                return False

        # Legacy env-var token path — routed through WorkerIdentity trust
        provided_token = request.headers.get("X-Worker-Token")
        if provided_token:
            try:
                worker = authenticate_legacy_worker(provided_token)
                request._worker_identity = worker
                log_worker_auth_success(request, worker_id=worker.worker_id)
                return True
            except PermissionDenied:
                log_worker_auth_failed(
                    request, worker_id="legacy-worker", reason="legacy_token",
                )
                return False

        return False

class ExecutionJobViewSet(viewsets.ModelViewSet):
    serializer_class = ExecutionJobSerializer
    permission_classes = [IsAuthenticatedOrWorkerToken]

    def get_queryset(self):
        qs = ExecutionJob.objects.select_related("account", "strategy", "assignment").all()
        request = self.request
        user = getattr(request, "user", None)

        # Workers (either modern or legacy) see all jobs; non-superuser
        # authenticated users see only their own.
        is_worker = getattr(request, "_worker_identity", None) is not None

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

        # Atomic claim: lock the row so only one worker can claim it.
        # skip_locked=True causes concurrent claimants to skip the locked
        # row and try the next PENDING job (or get None).
        with transaction.atomic():
            job = (
                qs.order_by("created_at")
                .select_for_update(skip_locked=True)
                .first()
            )

            if not job:
                # 204 No Content – no jobs available (or all locked by other workers)
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

        For PLACE_TEST_ORDER and PLACE_ORDER jobs that complete successfully,
        automatically creates a SYNC_POSITIONS job to ingest the trade into the database.
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

        # Lifecycle guard: only RUNNING jobs may be completed
        if job.status != ExecutionJob.Status.RUNNING:
            return Response(
                {
                    "detail": f"job {job.id} is {job.status}, expected RUNNING",
                    "current_status": job.status,
                },
                status=409,
            )

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

        # =================================================================
        # Auto-sync: Queue SYNC_POSITIONS job after successful trade placement
        # Applies to both demo trades (PLACE_TEST_ORDER) and signal trades (PLACE_ORDER)
        # =================================================================
        sync_job_id = None
        if (
            job.job_type in (ExecutionJob.JobType.PLACE_TEST_ORDER, ExecutionJob.JobType.PLACE_ORDER)
            and status_value == ExecutionJob.Status.SUCCESS
        ):
            sync_job_id = self._queue_sync_positions_job(job)

        serializer = self.get_serializer(job)
        response_data = serializer.data
        if sync_job_id:
            response_data["sync_job_id"] = sync_job_id

        return Response(response_data)

    def _queue_sync_positions_job(self, trigger_job: ExecutionJob) -> int | None:
        """
        Create a SYNC_POSITIONS job to ingest trades after a trade placement completes.

        Works for both:
        - PLACE_TEST_ORDER (demo trades)
        - PLACE_ORDER (strategy signal trades)

        Returns the new job ID, or None if creation failed (graceful degradation).
        """
        try:
            # Get windows_username from the trigger job payload or account's mt5_instance
            windows_username = (trigger_job.payload or {}).get("windows_username")

            if not windows_username:
                # Try to get from account's mt5_instance
                account = trigger_job.account
                if account and account.mt5_instance:
                    windows_username = getattr(account.mt5_instance, "windows_username", None)

            if not windows_username:
                # Cannot sync without windows_username - fail gracefully
                return None

            # Create SYNC_POSITIONS job
            sync_job = ExecutionJob.objects.create(
                job_type=ExecutionJob.JobType.SYNC_POSITIONS,
                account=trigger_job.account,
                strategy=trigger_job.strategy,
                assignment=trigger_job.assignment,
                status=ExecutionJob.Status.PENDING,
                created_by=trigger_job.created_by,
                payload={
                    "windows_username": windows_username,
                    "trigger_job_id": trigger_job.id,
                    "auto_sync": True,
                },
            )

            # Audit log
            log_trades_sync_queued(
                request=None,
                account_id=trigger_job.account_id,
                trigger_job_id=trigger_job.id,
                sync_job_id=sync_job.id,
            )

            return sync_job.id

        except Exception:
            # Fail gracefully - don't block completion response
            return None


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
        require_entitlement(user, "can_deploy_automation")

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

    Protected by ``IsAuthenticatedOrWorkerToken`` which supports:
    - WorkerIdentity (X-Worker-Id + X-Worker-Secret headers) — preferred
    - Legacy env-var token (X-Worker-Token header) — backward-compatible
    """

    authentication_classes: list = []
    permission_classes = [IsAuthenticatedOrWorkerToken]

    def get(self, request, account_id: int):
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
        # Entitlement check (before any other logic)
        # =====================================================================
        require_entitlement(request.user, "can_deploy_automation")

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
        # Get windows_username for auto-sync after trade completion
        # =====================================================================
        windows_username = None
        if account.mt5_instance:
            windows_username = getattr(account.mt5_instance, "windows_username", None) or None

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
                "windows_username": windows_username,  # For auto-sync after completion
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

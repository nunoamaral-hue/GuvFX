import os

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, MethodNotAllowed
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import (
    ExecutionJob,
    DEMO_ALLOWED_SYMBOLS,
    DEMO_FIXED_LOT_SIZE,
    DEMO_MAX_TRADES_PER_DAY,
    order_creation_kill_reason,
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
                # Suppressed: WORKER_AUTH_SUCCESS generates ~40K events/day
                # from polling. Log only failures (see log_worker_auth_failed).
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
                # Suppressed: same as modern path — polling noise.
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

    # EXEC-HARDEN-JOBS: the generic CRUD write surface (create/update/delete) is
    # disabled. ExecutionJobs are created ONLY through sanctioned, gated paths —
    #   * strategy automation (strategies.signal_engine / schedulers, internal),
    #   * execution.services.create_open_trade_job (OpenTradeJobView: entitlement
    #     + ownership + kill-switch gated),
    #   * CreateDemoTradeJobView (demo-only, allowlisted, kill-switch gated),
    #   * admin_ops.AdminExecutionJobViewSet (staff retry).
    # Worker/operator mutations use the explicit @actions (next / complete /
    # set-status / assign-account). This closes the previously-ungated ability of
    # an ordinary authenticated user to POST/PATCH an order-bearing job directly.
    _DISABLED_MSG = (
        "Direct ExecutionJob create/update/delete is disabled. Jobs are created "
        "only through sanctioned, gated services (demo endpoint / strategy "
        "automation); use the documented action endpoints for mutations."
    )

    def create(self, request, *args, **kwargs):
        raise MethodNotAllowed("POST", detail=self._DISABLED_MSG)

    def update(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method, detail=self._DISABLED_MSG)

    def partial_update(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method, detail=self._DISABLED_MSG)

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method, detail=self._DISABLED_MSG)

    @action(detail=False, methods=["get"], url_path="next")
    def next_job(self, request):
        """
        Called by MT5 worker: claim the oldest PENDING job and mark it RUNNING.

        Query params:
        - worker_id: Identifier for the claiming worker (default: "mt5-worker")
        - account_id: Filter jobs by account ID
        - job_type: Filter by job type. If omitted, defaults to SYNC_POSITIONS only.
                    This prevents Linux ingest workers from claiming PLACE_TEST_ORDER jobs.

        Node-aware routing:
        - Workers with ``authorized_nodes`` in their permissions can only claim
          jobs whose ``terminal_node_id`` matches one of their authorized nodes.
        - Workers *without* ``authorized_nodes`` (legacy) can only claim jobs
          where ``terminal_node_id IS NULL``.
        - Jobs targeting a node in draining/offline/disabled status are skipped.
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

        # ------------------------------------------------------------------
        # Node-aware filtering
        # ------------------------------------------------------------------
        worker_identity = getattr(request, "_worker_identity", None)
        authorized_nodes = []
        if worker_identity:
            perms = worker_identity.worker_permissions or {}
            authorized_nodes = perms.get("authorized_nodes", [])

        # Determine routing mode for audit trail
        routing_mode = "node_aware" if authorized_nodes else "legacy_null_node"

        if authorized_nodes:
            # Node-aware worker: only claim jobs targeting one of its nodes,
            # AND only if the node is active.
            from execution.models import TerminalNode

            active_node_ids = list(
                TerminalNode.objects.filter(
                    hostname__in=authorized_nodes,
                    status=TerminalNode.Status.ACTIVE,
                ).values_list("id", flat=True)
            )
            qs = qs.filter(terminal_node_id__in=active_node_ids)
        else:
            # Legacy worker (no authorized_nodes): only claim NULL-node jobs.
            qs = qs.filter(terminal_node__isnull=True)

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
                # ---------------------------------------------------------
                # Wrong-node claim detection (Fix 1):
                # If the filtered queryset is empty but PENDING jobs exist
                # that the worker *cannot* claim due to node mismatch,
                # emit NODE_WRONG_CLAIM audit event.
                # ---------------------------------------------------------
                base_qs = ExecutionJob.objects.filter(
                    status=ExecutionJob.Status.PENDING,
                )
                if job_type:
                    base_qs = base_qs.filter(job_type=job_type)
                else:
                    base_qs = base_qs.filter(
                        job_type=ExecutionJob.JobType.SYNC_POSITIONS,
                    )
                if account_id:
                    base_qs = base_qs.filter(account_id=account_id)

                # Peek at the first excluded job (without locking) for
                # audit context.  Do NOT expose its payload to the worker.
                excluded_job = base_qs.exclude(
                    id__in=qs.values_list("id", flat=True)
                ).values("id", "terminal_node_id").first()

                if excluded_job:
                    from core.audit import log_event as _log_event
                    _log_event(
                        request=None,
                        event_type="NODE_WRONG_CLAIM",
                        severity="WARN",
                        entity_type="execution_job",
                        entity_id=str(excluded_job["id"]),
                        metadata={
                            "worker_id": worker_id,
                            "routing_mode": routing_mode,
                            "authorized_nodes": authorized_nodes or None,
                            "job_terminal_node_id": excluded_job["terminal_node_id"],
                        },
                    )

                # 204 No Content – no jobs available (or all locked by other workers)
                return Response({"detail": "no_jobs"}, status=204)

            from datetime import timedelta as _timedelta
            _now = timezone.now()
            job.status = ExecutionJob.Status.RUNNING
            job.worker_id = worker_id
            job.started_at = _now
            # RX-2E: mandatory lease on RUNNING. An expired/absent lease marks the
            # job an orphan for the reliability supervisor (detection only, Phase 1).
            _lease_ttl = int(os.getenv("EXECUTION_LEASE_TTL_SECONDS", "300"))
            job.lease_expires_at = _now + _timedelta(seconds=_lease_ttl)
            job.save(update_fields=["status", "worker_id", "started_at", "lease_expires_at"])

        # Audit log — includes routing_mode marker (Fix 2: mixed-mode containment)
        log_execution_job_claimed(
            request=None,  # Worker request, not user request
            job_id=str(job.id),
            worker_id=worker_id,
            account_id=job.account_id,
            routing_mode=routing_mode,
            terminal_node_id=job.terminal_node_id,
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
                terminal_node_id=trigger_job.terminal_node_id,  # propagate from trigger
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
        # EXEC-HARDEN-JOBS: the global kill switch is a system-wide gate and is
        # checked FIRST — before per-user entitlement — so an engaged switch fails
        # closed for everyone. The model-layer guard is the backstop for all paths.
        kill_reason = order_creation_kill_reason()
        if kill_reason:
            return Response(
                {
                    "ok": False,
                    "error": "execution_disabled",
                    "message": "Execution is currently disabled (kill switch engaged).",
                    "reason": kill_reason,
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

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


# Kill-switch state is resolved via execution.models.order_creation_kill_reason
# (env flag + DB ExecutionControl). The former local env-only helper was removed
# in EXEC-HARDEN-JOBS in favour of that single source of truth.


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
        # Safety Check 1: Global kill switch (env flag OR DB ExecutionControl)
        # =====================================================================
        kill_reason = order_creation_kill_reason()
        if kill_reason:
            return Response(
                {
                    "ok": False,
                    "error": "execution_disabled",
                    "message": "Execution is currently disabled globally.",
                    "reason": kill_reason,
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
            terminal_node_id=account.terminal_node_id,  # snapshot at creation
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

    Emergency kill switch — engage the global execution control (fail closed).

    EXEC-E1a: this is now FUNCTIONAL for the signal-proposal bridge. Engaging it
    sets ``ExecutionControl.kill_switch_engaged`` so the bridge refuses to create
    any ``ProposedSignalOrder``. It does not (and in E1a cannot) touch a broker —
    there is no live execution path to stop. Releasing the switch is intentionally
    NOT exposed over the API (admin-only, server-side) so the web surface can only
    fail safe. Requires admin/staff permissions.
    """

    permission_classes = [permissions.IsAdminUser]

    def post(self, request):
        # Local import keeps execution.views import-time free of the bridge.
        from execution.signal_proposals import engage_kill_switch

        reason = ""
        if isinstance(getattr(request, "data", None), dict):
            reason = str(request.data.get("reason", ""))[:500]

        engage_kill_switch(actor=request.user, reason=reason or "kill-all via API")

        log_execution_attempt(
            request,
            event_type="EXECUTION_KILL_ATTEMPT",
            account_id="global",
            reason="Kill switch engaged via API.",
        )

        return Response(
            {
                "ok": True,
                "scope": "global",
                "kill_switch_engaged": True,
                "message": "Execution kill switch engaged. Signal proposals are blocked.",
            },
            status=status.HTTP_200_OK,
        )


# =============================================================================
# Terminal Node — Heartbeat & Admin CRUD
# =============================================================================

from .models import TerminalNode
from .serializers import TerminalNodeSerializer


class TerminalNodeHeartbeatView(APIView):
    """
    POST /api/execution/nodes/<hostname>/heartbeat/

    Called by workers to report liveness.  Updates ``last_heartbeat`` ONLY —
    never auto-mutates ``status``.  Authenticated via worker credentials.
    """

    permission_classes = [IsAuthenticatedOrWorkerToken]

    def post(self, request, hostname):
        try:
            node = TerminalNode.objects.get(hostname=hostname)
        except TerminalNode.DoesNotExist:
            return Response(
                {"detail": f"Unknown node: {hostname}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        node.last_heartbeat = timezone.now()
        node.save(update_fields=["last_heartbeat"])

        return Response({
            "hostname": node.hostname,
            "status": node.status,
            "last_heartbeat": node.last_heartbeat.isoformat(),
            "active_accounts": node.computed_active_accounts,
            "max_accounts": node.max_accounts,
        })


class TerminalNodeViewSet(viewsets.ModelViewSet):
    """
    Admin CRUD for TerminalNode.  Staff-only.

    Provides list/create/retrieve/update/destroy plus custom actions
    for status transitions (which emit audit events).
    """

    queryset = TerminalNode.objects.all()
    serializer_class = TerminalNodeSerializer
    permission_classes = [permissions.IsAdminUser]
    lookup_field = "hostname"

    def perform_create(self, serializer):
        node = serializer.save()
        from core.audit import log_event
        log_event(
            request=self.request,
            event_type="NODE_CREATED",
            severity="INFO",
            entity_type="TerminalNode",
            entity_id=str(node.id),
            metadata={
                "hostname": node.hostname,
                "max_accounts": node.max_accounts,
            },
        )

    @action(detail=True, methods=["post"], url_path="assign-account")
    def assign_account(self, request, hostname=None):
        """
        POST /api/execution/nodes/<hostname>/assign-account/
        Body: {"account_id": <int>}

        Assigns a TradingAccount to this node.
        Enforces capacity — rejects if node is at/over max_accounts.
        Emits NODE_ACCOUNT_ASSIGNED audit event with before/after values.
        """
        node = self.get_object()
        account_id = request.data.get("account_id")
        if not account_id:
            return Response(
                {"detail": "account_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            account = TradingAccount.objects.get(id=account_id)
        except TradingAccount.DoesNotExist:
            return Response(
                {"detail": f"TradingAccount {account_id} not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        previous_node_id = account.terminal_node_id
        previous_hostname = None
        if previous_node_id:
            prev_node = TerminalNode.objects.filter(id=previous_node_id).first()
            previous_hostname = prev_node.hostname if prev_node else None

        # Skip capacity check if re-assigning to the same node
        if previous_node_id != node.id:
            # Capacity enforcement: use authoritative FK-derived count
            current_count = TradingAccount.objects.filter(
                terminal_node=node, is_active=True,
            ).exclude(id=account.id).count()
            if current_count >= node.max_accounts:
                return Response(
                    {
                        "detail": "Node at capacity.",
                        "hostname": node.hostname,
                        "max_accounts": node.max_accounts,
                        "current_accounts": current_count,
                    },
                    status=status.HTTP_409_CONFLICT,
                )

        account.terminal_node = node
        account.save(update_fields=["terminal_node", "updated_at"])

        from core.audit import log_event
        log_event(
            request=request,
            event_type="NODE_ACCOUNT_ASSIGNED",
            severity="INFO",
            entity_type="TradingAccount",
            entity_id=str(account.id),
            metadata={
                "trading_account_id": account.id,
                "previous_terminal_node_id": previous_node_id,
                "previous_terminal_node_hostname": previous_hostname,
                "new_terminal_node_id": node.id,
                "new_terminal_node_hostname": node.hostname,
            },
        )

        return Response({
            "ok": True,
            "account_id": account.id,
            "terminal_node": node.hostname,
            "computed_active_accounts": node.computed_active_accounts,
        })

    @action(detail=True, methods=["post"], url_path="unassign-account")
    def unassign_account(self, request, hostname=None):
        """
        POST /api/execution/nodes/<hostname>/unassign-account/
        Body: {"account_id": <int>}

        Removes terminal_node assignment from a TradingAccount.
        Emits NODE_ACCOUNT_UNASSIGNED audit event with before/after values.
        """
        node = self.get_object()
        account_id = request.data.get("account_id")
        if not account_id:
            return Response(
                {"detail": "account_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            account = TradingAccount.objects.get(id=account_id)
        except TradingAccount.DoesNotExist:
            return Response(
                {"detail": f"TradingAccount {account_id} not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if account.terminal_node_id != node.id:
            return Response(
                {"detail": f"Account {account_id} is not assigned to node {node.hostname}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        account.terminal_node = None
        account.save(update_fields=["terminal_node", "updated_at"])

        from core.audit import log_event
        log_event(
            request=request,
            event_type="NODE_ACCOUNT_UNASSIGNED",
            severity="INFO",
            entity_type="TradingAccount",
            entity_id=str(account.id),
            metadata={
                "trading_account_id": account.id,
                "previous_terminal_node_id": node.id,
                "previous_terminal_node_hostname": node.hostname,
                "new_terminal_node_id": None,
                "new_terminal_node_hostname": None,
            },
        )

        return Response({
            "ok": True,
            "account_id": account.id,
            "terminal_node": None,
        })

    @action(detail=True, methods=["post"], url_path="set-status")
    def set_status(self, request, hostname=None):
        """
        POST /api/execution/nodes/<hostname>/set-status/
        Body: {"status": "active|draining|offline|disabled"}

        Audit-logged status transition.
        """
        node = self.get_object()
        new_status = request.data.get("status")
        valid = [c[0] for c in TerminalNode.Status.choices]
        if new_status not in valid:
            return Response(
                {"detail": f"Invalid status. Must be one of: {valid}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_status = node.status
        if old_status == new_status:
            return Response(TerminalNodeSerializer(node).data)

        node.status = new_status
        node.save(update_fields=["status", "updated_at"])

        from core.audit import log_event
        log_event(
            request=request,
            event_type="NODE_STATUS_CHANGED",
            severity="WARN" if new_status in ("draining", "offline", "disabled") else "INFO",
            entity_type="TerminalNode",
            entity_id=str(node.id),
            metadata={
                "hostname": node.hostname,
                "old_status": old_status,
                "new_status": new_status,
            },
        )

        return Response(TerminalNodeSerializer(node).data)

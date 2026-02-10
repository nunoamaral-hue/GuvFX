import os

from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import ExecutionJob
from .serializers import ExecutionJobSerializer, OpenTradeJobRequestSerializer
from .services import OpenTradeParams, create_open_trade_job
from strategies.models import Strategy, StrategyAssignment
from trading.models import TradingAccount
from core.audit import log_execution_attempt

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
        """
        worker_id = request.query_params.get("worker_id", "mt5-worker")

        job = (
            ExecutionJob.objects
            .filter(status=ExecutionJob.Status.PENDING)
            .order_by("created_at")
            .first()
        )
        if not job:
            # 204 No Content – no jobs available
            return Response({"detail": "no_jobs"}, status=204)

        job.status = ExecutionJob.Status.RUNNING
        job.worker_id = worker_id
        job.started_at = timezone.now()
        job.save(update_fields=["status", "worker_id", "started_at"])

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

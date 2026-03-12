"""
Admin Operations Console — API views.

All views enforce RBAC server-side via permission classes.
Immutable objects are never exposed through write endpoints.
Every privileged action emits an AuditEvent.
"""

from __future__ import annotations

import secrets

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from billing.entitlements import resolve_entitlements
from billing.models import PaymentEvent, UserSubscriptionState
from core.audit import log_admin_override, log_event
from execution.models import ExecutionJob, WorkerIdentity
from reconciliation.reconciliation_models import ReconciliationEvent

from .models import EntitlementOverride
from .permissions import (
    ROLE_FINANCE_ADMIN,
    ROLE_OPS_ADMIN,
    ROLE_SUPER_ADMIN,
    IsAdminRole,
    IsSuperAdmin,
    IsSuperOrFinanceAdmin,
    IsSuperOrFinanceOrOpsAdmin,
    IsSuperOrOpsAdmin,
    user_has_any_role,
)
from .serializers import (
    AdminExecutionJobDetailSerializer,
    AdminExecutionJobListSerializer,
    EntitlementOverrideApplySerializer,
    EntitlementOverrideCancelSerializer,
    EntitlementOverrideRenewSerializer,
    EntitlementOverrideSerializer,
    EntitlementSummarySerializer,
    ExecutionJobCancelSerializer,
    ExecutionJobRetrySerializer,
    PaymentEventDetailSerializer,
    PaymentEventListSerializer,
    ReconciliationAcknowledgeSerializer,
    ReconciliationEventDetailSerializer,
    ReconciliationEventListSerializer,
    ReconciliationResolveSerializer,
    WorkerCreateSerializer,
    WorkerIdentityDetailSerializer,
    WorkerIdentityListSerializer,
    WorkerRevokeSerializer,
)

User = get_user_model()


# =========================================================================
# Helper: audit failed admin authorization attempts
# =========================================================================

def _audit_admin_denied(request, action_name: str, entity_type: str = "",
                        entity_id: str | None = None):
    """Emit audit event for a blocked admin action (authorization failure)."""
    log_event(
        request,
        event_type="ADMIN_OVERRIDE",
        severity="WARN",
        entity_type=entity_type,
        entity_id=entity_id,
        metadata={
            "action": f"admin_auth_denied:{action_name}",
            "attempted_by": getattr(request.user, "email", "unknown"),
        },
    )


# =========================================================================
# 3A — Reconciliation Dashboard
# =========================================================================


class AdminReconciliationViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Admin API for reconciliation discrepancies.

    Access:
      - super_admin: full (acknowledge + resolve)
      - finance_admin: full (acknowledge + resolve)
      - ops_admin: read-only + acknowledge only
    """
    permission_classes = [IsSuperOrFinanceOrOpsAdmin]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return ReconciliationEventDetailSerializer
        return ReconciliationEventListSerializer

    def get_queryset(self):
        qs = ReconciliationEvent.objects.select_related("account").all()
        # Apply filters
        params = self.request.query_params
        if params.get("account_id"):
            qs = qs.filter(account_id=params["account_id"])
        if params.get("severity"):
            qs = qs.filter(severity=params["severity"])
        if params.get("resolution_status"):
            qs = qs.filter(resolution_status=params["resolution_status"])
        if params.get("reconciliation_type"):
            qs = qs.filter(reconciliation_type=params["reconciliation_type"])
        if params.get("reconciliation_run_id"):
            qs = qs.filter(reconciliation_run_id=params["reconciliation_run_id"])
        return qs

    @action(detail=True, methods=["post"])
    def acknowledge(self, request, pk=None):
        """Acknowledge a discrepancy (workflow action — does NOT mutate raw data)."""
        event = self.get_object()
        if event.resolution_status != ReconciliationEvent.ResolutionStatus.OPEN:
            return Response(
                {"detail": f"Cannot acknowledge: current status is '{event.resolution_status}'."},
                status=status.HTTP_409_CONFLICT,
            )

        ser = ReconciliationAcknowledgeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        before = event.resolution_status
        event.resolution_status = ReconciliationEvent.ResolutionStatus.ACKNOWLEDGED
        event.save(update_fields=["resolution_status"])

        log_admin_override(
            request,
            action="reconciliation_discrepancy_acknowledged",
            entity_type="ReconciliationEvent",
            entity_id=str(event.id),
            metadata={
                "before_state": before,
                "after_state": event.resolution_status,
                "reason": ser.validated_data.get("reason", ""),
            },
        )
        return Response({"ok": True, "resolution_status": event.resolution_status})

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Resolve a discrepancy.  Requires super_admin or finance_admin."""
        if not user_has_any_role(request.user, ROLE_SUPER_ADMIN, ROLE_FINANCE_ADMIN):
            _audit_admin_denied(request, "reconciliation_resolve",
                                "ReconciliationEvent", str(pk))
            return Response(
                {"detail": "Resolve requires super_admin or finance_admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        event = self.get_object()
        if event.resolution_status == ReconciliationEvent.ResolutionStatus.RESOLVED:
            return Response(
                {"detail": "Already resolved."},
                status=status.HTTP_409_CONFLICT,
            )

        ser = ReconciliationResolveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        before = event.resolution_status
        event.resolution_status = ReconciliationEvent.ResolutionStatus.RESOLVED
        event.save(update_fields=["resolution_status"])

        log_admin_override(
            request,
            action="reconciliation_discrepancy_resolved",
            entity_type="ReconciliationEvent",
            entity_id=str(event.id),
            metadata={
                "before_state": before,
                "after_state": event.resolution_status,
                "reason": ser.validated_data.get("reason", ""),
            },
        )
        return Response({"ok": True, "resolution_status": event.resolution_status})


# =========================================================================
# 3B — Payment Event Viewer (strictly read-only)
# =========================================================================


class AdminPaymentEventViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Read-only admin view for PaymentEvent.

    - No write endpoints
    - No raw payload mutation
    - Payload re-sanitized on read
    """
    permission_classes = [IsSuperOrFinanceAdmin]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return PaymentEventDetailSerializer
        return PaymentEventListSerializer

    def get_queryset(self):
        qs = PaymentEvent.objects.all()
        params = self.request.query_params
        if params.get("provider"):
            qs = qs.filter(provider_name=params["provider"])
        if params.get("event_type"):
            qs = qs.filter(event_type=params["event_type"])
        if params.get("processing_status"):
            qs = qs.filter(processing_status=params["processing_status"])
        if params.get("subscription_reference"):
            qs = qs.filter(subscription_reference=params["subscription_reference"])
        return qs


# =========================================================================
# 3C — Worker Identity Management
# =========================================================================


class AdminWorkerViewSet(viewsets.ViewSet):
    """
    Worker identity management.

    Access:
      - super_admin: full (create, rotate, revoke)
      - ops_admin: full (create, rotate, revoke)
      - finance_admin: no access

    Secret lifecycle:
      - Secrets shown exactly once (on create and rotate responses)
      - Never retrievable after
    """
    permission_classes = [IsSuperOrOpsAdmin]

    def list(self, request):
        workers = WorkerIdentity.objects.all().order_by("-created_at")
        ser = WorkerIdentityListSerializer(workers, many=True)
        return Response(ser.data)

    def retrieve(self, request, pk=None):
        worker = get_object_or_404(WorkerIdentity, pk=pk)
        ser = WorkerIdentityDetailSerializer(worker)
        return Response(ser.data)

    def create(self, request):
        """Create a new worker identity.  Returns secret ONCE."""
        ser = WorkerCreateSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        worker_id = ser.validated_data["worker_id"]
        raw_secret = secrets.token_urlsafe(48)

        try:
            worker = WorkerIdentity.objects.create(
                worker_id=worker_id,
                worker_secret_hash=WorkerIdentity.hash_secret(raw_secret),
                worker_permissions=ser.validated_data.get("worker_permissions", {}),
                status=WorkerIdentity.Status.ACTIVE,
            )
        except IntegrityError:
            return Response(
                {"detail": f"Worker '{worker_id}' already exists."},
                status=status.HTTP_409_CONFLICT,
            )

        log_admin_override(
            request,
            action="worker_created",
            entity_type="WorkerIdentity",
            entity_id=str(worker.id),
            metadata={"worker_id": worker_id},
        )

        return Response(
            {
                "ok": True,
                "worker_id": worker.worker_id,
                "worker_secret": raw_secret,  # shown ONCE
                "message": "Store this secret securely. It cannot be retrieved again.",
            },
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"], url_path="rotate-secret")
    def rotate_secret(self, request, pk=None):
        """Rotate worker secret.  Invalidates previous secret immediately."""
        worker = get_object_or_404(WorkerIdentity, pk=pk)

        if worker.status != WorkerIdentity.Status.ACTIVE:
            return Response(
                {"detail": "Cannot rotate secret for a non-active worker."},
                status=status.HTTP_409_CONFLICT,
            )

        raw_secret = secrets.token_urlsafe(48)
        worker.worker_secret_hash = WorkerIdentity.hash_secret(raw_secret)
        worker.save(update_fields=["worker_secret_hash"])

        log_admin_override(
            request,
            action="worker_secret_rotated",
            entity_type="WorkerIdentity",
            entity_id=str(worker.id),
            metadata={"worker_id": worker.worker_id},
        )

        return Response({
            "ok": True,
            "worker_id": worker.worker_id,
            "worker_secret": raw_secret,  # shown ONCE
            "message": "Previous secret is now invalid. Store this new secret securely.",
        })

    @action(detail=True, methods=["post"])
    def revoke(self, request, pk=None):
        """Revoke/disable a worker (does NOT delete)."""
        worker = get_object_or_404(WorkerIdentity, pk=pk)

        if worker.status == WorkerIdentity.Status.REVOKED:
            return Response(
                {"detail": "Worker is already revoked."},
                status=status.HTTP_409_CONFLICT,
            )

        ser = WorkerRevokeSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        before = worker.status
        worker.status = WorkerIdentity.Status.REVOKED
        worker.save(update_fields=["status"])

        log_admin_override(
            request,
            action="worker_revoked",
            entity_type="WorkerIdentity",
            entity_id=str(worker.id),
            metadata={
                "worker_id": worker.worker_id,
                "before_state": before,
                "after_state": worker.status,
                "reason": ser.validated_data.get("reason", ""),
            },
        )

        return Response({"ok": True, "worker_id": worker.worker_id, "status": worker.status})


# =========================================================================
# 3D — Entitlement Override Tools
# =========================================================================


class AdminEntitlementSummaryView(APIView):
    """
    GET /api/admin/entitlements/<user_id>/summary/

    Returns effective entitlements for a user, including active overrides.
    """
    permission_classes = [IsSuperAdmin]

    def get(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        state = UserSubscriptionState.objects.filter(user=user).first()
        entitlements = resolve_entitlements(state)

        active_overrides = EntitlementOverride.objects.filter(
            user=user, is_active=True,
        )

        data = {
            "user_id": user.id,
            "email": user.email,
            "source_plan": entitlements.source_plan,
            "source_plan_status": entitlements.source_plan_status,
            "viewer_mode": entitlements.viewer_mode,
            "resolved_access_mode": entitlements.resolved_access_mode,
            "capabilities": {
                "can_view_dashboard": entitlements.can_view_dashboard,
                "can_browse_marketplace": entitlements.can_browse_marketplace,
                "can_run_backtests": entitlements.can_run_backtests,
                "can_assign_strategies": entitlements.can_assign_strategies,
                "can_deploy_automation": entitlements.can_deploy_automation,
                "max_trading_accounts": entitlements.max_trading_accounts,
                "max_active_strategies": entitlements.max_active_strategies,
                "historical_data_tier": entitlements.historical_data_tier,
            },
            "active_overrides": EntitlementOverrideSerializer(
                active_overrides, many=True,
            ).data,
        }
        return Response(data)


class AdminEntitlementOverrideViewSet(viewsets.ViewSet):
    """
    Entitlement override CRUD.

    super_admin only by default.
    """
    permission_classes = [IsSuperAdmin]

    def list(self, request):
        qs = EntitlementOverride.objects.select_related("user", "created_by").all()
        params = request.query_params
        if params.get("user_id"):
            qs = qs.filter(user_id=params["user_id"])
        if params.get("is_active"):
            qs = qs.filter(is_active=params["is_active"].lower() == "true")
        if params.get("capability"):
            qs = qs.filter(capability=params["capability"])
        ser = EntitlementOverrideSerializer(qs, many=True)
        return Response(ser.data)

    def retrieve(self, request, pk=None):
        override = get_object_or_404(
            EntitlementOverride.objects.select_related("user", "created_by"),
            pk=pk,
        )
        return Response(EntitlementOverrideSerializer(override).data)

    def create(self, request):
        """Apply a new override."""
        ser = EntitlementOverrideApplySerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        if data["expires_at"] <= timezone.now():
            return Response(
                {"detail": "expires_at must be in the future."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_user = get_object_or_404(User, pk=data["user_id"])

        try:
            override = EntitlementOverride.objects.create(
                user=target_user,
                capability=data["capability"],
                override_value=data.get("override_value", {}),
                reason=data["reason"],
                expires_at=data["expires_at"],
                created_by=request.user,
                is_active=True,
            )
        except IntegrityError:
            return Response(
                {
                    "detail": (
                        f"Active override already exists for user "
                        f"{target_user.id} capability '{data['capability']}'. "
                        f"Cancel the existing one first."
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )

        log_admin_override(
            request,
            action="entitlement_override_created",
            entity_type="EntitlementOverride",
            entity_id=str(override.id),
            metadata={
                "target_user_id": target_user.id,
                "capability": data["capability"],
                "override_value": data.get("override_value", {}),
                "reason": data["reason"],
                "expires_at": data["expires_at"].isoformat(),
            },
        )

        return Response(
            EntitlementOverrideSerializer(override).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["post"])
    def renew(self, request, pk=None):
        """Extend an active override's expiry."""
        override = get_object_or_404(EntitlementOverride, pk=pk)

        if not override.is_active:
            return Response(
                {"detail": "Override is not active."},
                status=status.HTTP_409_CONFLICT,
            )

        ser = EntitlementOverrideRenewSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        if data["expires_at"] <= timezone.now():
            return Response(
                {"detail": "expires_at must be in the future."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_expires = override.expires_at
        override.expires_at = data["expires_at"]
        override.save(update_fields=["expires_at", "updated_at"])

        log_admin_override(
            request,
            action="entitlement_override_renewed",
            entity_type="EntitlementOverride",
            entity_id=str(override.id),
            metadata={
                "target_user_id": override.user_id,
                "capability": override.capability,
                "previous_expires_at": old_expires.isoformat(),
                "new_expires_at": data["expires_at"].isoformat(),
                "reason": data["reason"],
            },
        )

        return Response(EntitlementOverrideSerializer(override).data)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancel (deactivate) an override."""
        override = get_object_or_404(EntitlementOverride, pk=pk)

        if not override.is_active:
            return Response(
                {"detail": "Override is already inactive."},
                status=status.HTTP_409_CONFLICT,
            )

        ser = EntitlementOverrideCancelSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        override.is_active = False
        override.save(update_fields=["is_active", "updated_at"])

        log_admin_override(
            request,
            action="entitlement_override_cancelled",
            entity_type="EntitlementOverride",
            entity_id=str(override.id),
            metadata={
                "target_user_id": override.user_id,
                "capability": override.capability,
                "reason": ser.validated_data.get("reason", ""),
            },
        )

        return Response({"ok": True, "id": override.id, "is_active": False})


# =========================================================================
# 3E — Execution Job Diagnostics
# =========================================================================


class AdminExecutionJobViewSet(viewsets.ReadOnlyModelViewSet):
    """
    Admin execution job diagnostics.

    Access:
      - super_admin: full (retry + cancel)
      - ops_admin: full (retry + cancel)
      - finance_admin: read-only
    """
    permission_classes = [IsSuperOrFinanceOrOpsAdmin]

    def get_serializer_class(self):
        if self.action == "retrieve":
            return AdminExecutionJobDetailSerializer
        return AdminExecutionJobListSerializer

    def get_queryset(self):
        qs = ExecutionJob.objects.select_related(
            "account", "strategy", "terminal_node",
        ).all()
        params = self.request.query_params
        if params.get("account_id"):
            qs = qs.filter(account_id=params["account_id"])
        if params.get("strategy_id"):
            qs = qs.filter(strategy_id=params["strategy_id"])
        if params.get("job_type"):
            qs = qs.filter(job_type=params["job_type"])
        if params.get("status"):
            qs = qs.filter(status=params["status"])
        if params.get("worker_id"):
            qs = qs.filter(worker_id=params["worker_id"])
        if params.get("terminal_node_id"):
            qs = qs.filter(terminal_node_id=params["terminal_node_id"])
        return qs

    @action(detail=True, methods=["post"])
    def retry(self, request, pk=None):
        """
        Retry a failed job by creating a NEW job with the same payload.

        Only allowed for FAILED jobs.  Original job record is preserved.
        """
        if not user_has_any_role(request.user, ROLE_SUPER_ADMIN, ROLE_OPS_ADMIN):
            _audit_admin_denied(request, "execution_job_retry",
                                "ExecutionJob", str(pk))
            return Response(
                {"detail": "Retry requires super_admin or ops_admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        original = self.get_object()

        if original.status != ExecutionJob.Status.FAILED:
            return Response(
                {"detail": f"Cannot retry: job status is '{original.status}', expected 'FAILED'."},
                status=status.HTTP_409_CONFLICT,
            )

        ser = ExecutionJobRetrySerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        # Create new job preserving the original's parameters
        new_job = ExecutionJob.objects.create(
            job_type=original.job_type,
            account=original.account,
            strategy=original.strategy,
            assignment=original.assignment,
            terminal_node=original.terminal_node,
            payload=original.payload,
            status=ExecutionJob.Status.PENDING,
            created_by=request.user,
        )

        log_admin_override(
            request,
            action="execution_job_retried",
            entity_type="ExecutionJob",
            entity_id=str(new_job.id),
            metadata={
                "original_job_id": original.id,
                "job_type": original.job_type,
                "account_id": original.account_id,
                "reason": ser.validated_data.get("reason", ""),
            },
        )

        return Response({
            "ok": True,
            "original_job_id": original.id,
            "new_job_id": new_job.id,
            "status": new_job.status,
        }, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """
        Cancel a pending job.

        Only allowed for PENDING jobs.
        """
        if not user_has_any_role(request.user, ROLE_SUPER_ADMIN, ROLE_OPS_ADMIN):
            _audit_admin_denied(request, "execution_job_cancel",
                                "ExecutionJob", str(pk))
            return Response(
                {"detail": "Cancel requires super_admin or ops_admin."},
                status=status.HTTP_403_FORBIDDEN,
            )

        job = self.get_object()

        if job.status != ExecutionJob.Status.PENDING:
            return Response(
                {"detail": f"Cannot cancel: job status is '{job.status}', expected 'PENDING'."},
                status=status.HTTP_409_CONFLICT,
            )

        ser = ExecutionJobCancelSerializer(data=request.data)
        ser.is_valid(raise_exception=True)

        before = job.status
        job.status = ExecutionJob.Status.FAILED
        job.error_message = f"Cancelled by admin: {ser.validated_data.get('reason', '')}"
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "error_message", "finished_at"])

        log_admin_override(
            request,
            action="execution_job_cancelled",
            entity_type="ExecutionJob",
            entity_id=str(job.id),
            metadata={
                "before_state": before,
                "after_state": job.status,
                "reason": ser.validated_data.get("reason", ""),
            },
        )

        return Response({
            "ok": True,
            "job_id": job.id,
            "status": job.status,
        })

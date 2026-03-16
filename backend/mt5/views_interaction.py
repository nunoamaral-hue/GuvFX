"""
DRF Views for Packet A — Terminal Interaction API.

All views delegate to the service layer only.  No adapter logic,
no Guacamole helper calls, no direct occupancy mutations.

Endpoints:
    POST /api/mt5-interaction/sessions/              (launch)
    GET  /api/mt5-interaction/sessions/{id}/          (status)
    POST /api/mt5-interaction/sessions/{id}/resume/
    POST /api/mt5-interaction/sessions/{id}/terminate/
    GET  /api/mt5-interaction/terminal-bindings/
"""
import logging

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from mt5.models import InteractionSession, TerminalBinding
from mt5.serializers import (
    InteractionSessionResponseSerializer,
    ResumableContextResponseSerializer,
    SessionLaunchRequestSerializer,
    SessionTerminateRequestSerializer,
    TerminalBindingListSerializer,
)

logger = logging.getLogger(__name__)


class SessionLaunchView(APIView):
    """
    POST /api/mt5-interaction/sessions/

    Launch a new terminal interaction session.
    Delegates entirely to session_launch_orchestration_service.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = SessionLaunchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        binding_id = serializer.validated_data["terminal_binding_id"]
        session_expires_at = serializer.validated_data.get("session_expires_at")

        try:
            from mt5.services.session_launch_orchestration_service import (
                orchestrate_launch,
            )

            result = orchestrate_launch(
                user_id=request.user.id,
                binding_id=binding_id,
                session_expires_at=session_expires_at,
            )
        except Exception as e:
            error_msg = str(e)
            logger.warning(
                "Session launch failed: user=%s binding=%s error=%s",
                request.user.id, binding_id, error_msg,
            )
            # Map known service exceptions to appropriate HTTP status
            status_code = _map_service_error(e)
            return Response(
                {"detail": error_msg},
                status=status_code,
            )

        response_data = InteractionSessionResponseSerializer(
            result.interaction_session,
        ).data
        return Response(response_data, status=status.HTTP_201_CREATED)


class SessionDetailView(APIView):
    """
    GET /api/mt5-interaction/sessions/{id}/

    Return persisted domain/session state.  Does NOT treat adapter
    get_status as authoritative — returns persisted truth only.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        try:
            session = InteractionSession.objects.select_related(
                "terminal_binding",
                "terminal_binding__terminal_node",
            ).get(pk=pk, user=request.user)
        except InteractionSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            InteractionSessionResponseSerializer(session).data,
        )


class SessionResumeView(APIView):
    """
    POST /api/mt5-interaction/sessions/{id}/resume/

    Validate and return resumable session context.
    Delegates to session_resume_service (validation/context only).
    Does NOT create new MT5Sessions or perform lifecycle mutation.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        try:
            session = InteractionSession.objects.select_related(
                "terminal_binding",
                "terminal_binding__terminal_node",
                "authorization",
            ).get(pk=pk, user=request.user)
        except InteractionSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            from mt5.services.session_resume_service import resolve_resumable

            context = resolve_resumable(
                session=session,
                user_id=request.user.id,
            )
        except Exception as e:
            error_msg = str(e)
            logger.warning(
                "Session resume validation failed: user=%s session=%s error=%s",
                request.user.id, pk, error_msg,
            )
            status_code = _map_service_error(e)
            return Response(
                {"detail": error_msg},
                status=status_code,
            )

        response_data = ResumableContextResponseSerializer({
            "interaction_session": context.interaction_session,
            "can_resume": True,
            "access_mode": context.authorization.access_mode if context.authorization else "",
        }).data
        return Response(response_data)


class SessionTerminateView(APIView):
    """
    POST /api/mt5-interaction/sessions/{id}/terminate/

    Terminate an interaction session.
    Delegates entirely to session_terminate_service.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        serializer = SessionTerminateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reason = serializer.validated_data.get("reason", "")

        try:
            session = InteractionSession.objects.select_related(
                "terminal_binding",
                "terminal_binding__terminal_node",
            ).get(pk=pk, user=request.user)
        except InteractionSession.DoesNotExist:
            return Response(
                {"detail": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            from mt5.services.session_terminate_service import terminate_session

            terminate_session(
                session=session,
                reason=reason,
                actor_user_id=request.user.id,
            )
        except Exception as e:
            error_msg = str(e)
            logger.warning(
                "Session terminate failed: user=%s session=%s error=%s",
                request.user.id, pk, error_msg,
            )
            status_code = _map_service_error(e)
            return Response(
                {"detail": error_msg},
                status=status_code,
            )

        # Re-fetch to return post-termination state
        session.refresh_from_db()
        return Response(
            InteractionSessionResponseSerializer(session).data,
        )


class TerminalBindingListView(APIView):
    """
    GET /api/mt5-interaction/terminal-bindings/

    List terminal bindings that the authenticated user has active
    authorization for.  Filtered through UserToTerminalAuthorization
    records — does NOT expose all bindings globally.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        now = timezone.now()

        # Get binding IDs the user is authorized for
        from mt5.models import UserToTerminalAuthorization

        authorized_binding_ids = (
            UserToTerminalAuthorization.objects
            .filter(
                user=request.user,
                revoked_at__isnull=True,
            )
            .exclude(
                expires_at__isnull=False,
                expires_at__lt=now,
            )
            .values_list("terminal_binding_id", flat=True)
            .distinct()
        )

        bindings = (
            TerminalBinding.objects
            .filter(pk__in=authorized_binding_ids)
            .select_related("terminal_node")
            .order_by("terminal_node", "terminal_identifier")
        )

        return Response(
            TerminalBindingListSerializer(bindings, many=True).data,
        )


# =========================================================================
# Helpers
# =========================================================================


def _map_service_error(exc: Exception) -> int:
    """
    Map known service-layer exceptions to HTTP status codes.

    Unknown exceptions default to 400.
    """
    from mt5.services.binding_resolution_service import BindingResolutionError
    from mt5.services.authorization_validation_service import AuthorizationDenied
    from mt5.services.binding_occupancy_enforcement_service import OccupancyError
    from mt5.services.session_launch_orchestration_service import LaunchError
    from mt5.services.session_resume_service import ResumeError
    from mt5.services.session_terminate_service import TerminateError

    if isinstance(exc, AuthorizationDenied):
        return status.HTTP_403_FORBIDDEN
    if isinstance(exc, BindingResolutionError):
        return status.HTTP_404_NOT_FOUND
    if isinstance(exc, OccupancyError):
        return status.HTTP_409_CONFLICT
    if isinstance(exc, (LaunchError, ResumeError, TerminateError)):
        return status.HTTP_409_CONFLICT
    return status.HTTP_400_BAD_REQUEST

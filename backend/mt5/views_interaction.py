"""
DRF Views for Packet A — Terminal Interaction API.

All views delegate to the service layer only.  No adapter logic,
no Guacamole helper calls, no direct ORM queries, no direct
occupancy mutations.

Endpoints:
    POST /api/mt5-interaction/sessions/              (launch)
    GET  /api/mt5-interaction/sessions/{id}/          (status)
    POST /api/mt5-interaction/sessions/{id}/resume/
    POST /api/mt5-interaction/sessions/{id}/terminate/
    GET  /api/mt5-interaction/terminal-bindings/
"""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from mt5.serializers import (
    InteractionSessionResponseSerializer,
    ResumableContextResponseSerializer,
    SafeLaunchDescriptorSerializer,
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

        # Invoke adapter to obtain embed credentials (service-layer only)
        safe_descriptor = _invoke_adapter_for_launch(result)

        response_data = InteractionSessionResponseSerializer(
            result.interaction_session,
        ).data
        response_data["launch_descriptor"] = SafeLaunchDescriptorSerializer(
            safe_descriptor,
        ).data
        return Response(response_data, status=status.HTTP_201_CREATED)


class SessionDetailView(APIView):
    """
    GET /api/mt5-interaction/sessions/{id}/

    Return persisted domain/session state.  Delegates to
    session_read_service.  Does NOT treat adapter get_status
    as authoritative — returns persisted truth only.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        from mt5.services.session_read_service import (
            get_user_session_detail,
            SessionNotFound,
        )

        try:
            session = get_user_session_detail(
                user_id=request.user.id,
                session_id=pk,
            )
        except SessionNotFound:
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
    Delegates to session_resume_service (validation/context only)
    with session retrieval via session_read_service.
    Does NOT create new MT5Sessions or perform lifecycle mutation.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        from mt5.services.session_read_service import (
            get_user_session_detail,
            SessionNotFound,
        )

        try:
            session = get_user_session_detail(
                user_id=request.user.id,
                session_id=pk,
            )
        except SessionNotFound:
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

        # Invoke adapter to obtain fresh embed credentials (service-layer only)
        safe_descriptor = _invoke_adapter_for_resume(context)

        response_data = ResumableContextResponseSerializer({
            "interaction_session": context.interaction_session,
            "can_resume": True,
            "access_mode": context.authorization.access_mode if context.authorization else "",
        }).data
        response_data["launch_descriptor"] = SafeLaunchDescriptorSerializer(
            safe_descriptor,
        ).data
        return Response(response_data)


class SessionTerminateView(APIView):
    """
    POST /api/mt5-interaction/sessions/{id}/terminate/

    Terminate an interaction session.
    Delegates to session_terminate_service with session retrieval
    via session_read_service.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        serializer = SessionTerminateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        reason = serializer.validated_data.get("reason", "")

        from mt5.services.session_read_service import (
            get_user_session_detail,
            SessionNotFound,
        )

        try:
            session = get_user_session_detail(
                user_id=request.user.id,
                session_id=pk,
            )
        except SessionNotFound:
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

        # Re-fetch via service to return post-termination state
        try:
            session = get_user_session_detail(
                user_id=request.user.id,
                session_id=pk,
            )
        except SessionNotFound:
            pass  # use pre-termination session object

        return Response(
            InteractionSessionResponseSerializer(session).data,
        )


class ActiveSessionView(APIView):
    """
    GET /api/mt5-interaction/sessions/active/

    Return the authenticated user's current resumable interaction session
    (state == "active", not ended, not expired), or null if none.

    PX-7A / INCIDENT-001: lets the Terminal Access page re-discover an
    existing session after a page reload / SPA navigation so the viewer can
    be reconnected instead of the binding being mislabelled "Unavailable".

    Read-only — delegates to the existing resume read service.  Creates no
    sessions, mutates no lifecycle/occupancy, touches no trading/execution.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from mt5.services.session_resume_service import find_resumable_sessions

        session = find_resumable_sessions(user_id=request.user.id).first()
        if session is None:
            return Response({"active_session": None})

        return Response({
            "active_session": InteractionSessionResponseSerializer(session).data,
        })


class TerminalBindingListView(APIView):
    """
    GET /api/mt5-interaction/terminal-bindings/

    List terminal bindings that the authenticated user has active
    authorization for.  Delegates to session_read_service —
    does NOT expose all bindings globally.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from mt5.services.session_read_service import (
            list_authorized_terminal_bindings,
        )

        bindings = list_authorized_terminal_bindings(
            user_id=request.user.id,
        )

        return Response(
            TerminalBindingListSerializer(bindings, many=True).data,
        )


# =========================================================================
# Adapter invocation helpers (delegate to service-layer bridge functions)
# =========================================================================

_EMPTY_DESCRIPTOR = {
    "transport_type": "",
    "embed_url": "",
    "session_token": "",
    "expiry": None,
}


def _invoke_adapter_for_launch(launch_result) -> dict:
    """Invoke adapter for launch via service bridge. Fail-safe."""
    try:
        from mt5.services.session_launch_orchestration_service import (
            invoke_adapter_launch,
        )
        return invoke_adapter_launch(launch_result)
    except Exception as e:
        logger.warning("Adapter launch invocation failed (non-blocking): %s", str(e))
        return dict(_EMPTY_DESCRIPTOR)


def _invoke_adapter_for_resume(context) -> dict:
    """Invoke adapter for resume via service bridge. Fail-safe."""
    try:
        from mt5.services.session_resume_service import invoke_adapter_resume
        return invoke_adapter_resume(context)
    except Exception as e:
        logger.warning("Adapter resume invocation failed (non-blocking): %s", str(e))
        return dict(_EMPTY_DESCRIPTOR)


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

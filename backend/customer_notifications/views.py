from __future__ import annotations

import logging
import secrets

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from .models import CustomerNotificationPreference
from .services import (
    InvalidConnectionToken,
    InvalidTelegramIdentity,
    TelegramChatAlreadyBound,
    TelegramConnectionError,
    TelegramUnavailable,
    create_connection_token,
    customer_telegram_available,
    disconnect_telegram,
    redeem_connection_token,
    request_workspace_readiness_notification,
    set_strategy_notification_preference,
    strategy_notification_settings_for,
    telegram_settings_for,
    workspace_readiness_settings_for,
)

logger = logging.getLogger("guvfx.customer_notifications")

# Deterministic customer/input rejections a retry can NEVER fix. Telegram delivers webhook updates strictly
# in order and re-delivers any non-2xx, so returning 4xx for one of these pins it at the head of the queue and
# blocks every later /start — a deadlock that made fresh connections impossible (the incident this fixes). The
# webhook ACKNOWLEDGES these with 200 (bind nothing, notify nothing, drop the update); only genuinely TRANSIENT
# failures return non-2xx so Telegram may usefully retry.
_PERMANENT_REDEEM_REJECTIONS = (InvalidConnectionToken, InvalidTelegramIdentity, TelegramChatAlreadyBound)


class CustomerTelegramWebhookThrottle(SimpleRateThrottle):
    scope = "customer_telegram_webhook"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class TelegramSettingsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(telegram_settings_for(request.user))


class TelegramConnectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        try:
            result = create_connection_token(request.user, language=request.data.get("language", "en"))
        except TelegramConnectionError as exc:
            return Response({"detail": exc.code}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(result, status=status.HTTP_201_CREATED)


class TelegramDisconnectView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        disconnect_telegram(request.user)
        return Response(telegram_settings_for(request.user))


class TelegramPreferencesView(APIView):
    permission_classes = [IsAuthenticated]
    _BOOL_FIELDS = {
        "winning_trades", "losing_trades", "tp_progress", "system_messages",
    }

    def patch(self, request):
        pref, _ = CustomerNotificationPreference.objects.get_or_create(user=request.user)
        unsupported = set(request.data) - self._BOOL_FIELDS - {"language"}
        if unsupported:
            return Response({"detail": "unsupported notification preference"}, status=400)
        changed = []
        for field in self._BOOL_FIELDS:
            if field in request.data:
                if not isinstance(request.data[field], bool):
                    return Response({"detail": f"{field} must be boolean"}, status=400)
                setattr(pref, field, request.data[field])
                changed.append(field)
        if "language" in request.data:
            if request.data["language"] not in ("en", "ja"):
                return Response({"detail": "language must be en or ja"}, status=400)
            pref.language = request.data["language"]
            changed.append("language")
        if changed:
            pref.save(update_fields=[*changed, "updated_at"])
        return Response(telegram_settings_for(request.user))


class StrategyNotificationPreferenceView(APIView):
    permission_classes = [IsAuthenticated]

    def _assignment(self, request, assignment_id):
        from strategies.models import StrategyAssignment
        return StrategyAssignment.objects.select_related("account", "strategy").filter(
            pk=assignment_id, account__user=request.user,
        ).first()

    def get(self, request, assignment_id):
        assignment = self._assignment(request, assignment_id)
        if assignment is None:
            return Response({"detail": "not_found"}, status=404)
        return Response(strategy_notification_settings_for(request.user, assignment))

    def patch(self, request, assignment_id):
        assignment = self._assignment(request, assignment_id)
        if assignment is None:
            return Response({"detail": "not_found"}, status=404)
        if set(request.data) != {"enabled"} or not isinstance(request.data.get("enabled"), bool):
            return Response({"detail": "enabled must be boolean"}, status=400)
        return Response(set_strategy_notification_preference(
            request.user, assignment, enabled=request.data["enabled"],
        ))


class WorkspaceReadinessNotificationView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(workspace_readiness_settings_for(request.user))

    def post(self, request):
        language = request.data.get("language", "en") if isinstance(request.data, dict) else "en"
        try:
            result = request_workspace_readiness_notification(request.user, language=language)
        except ValueError:
            return Response({"detail": "workspace_not_found"}, status=404)
        except TelegramConnectionError as exc:
            return Response({"detail": exc.code}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response(result, status=201)


class TelegramWebhookView(APIView):
    """Dedicated customer-bot webhook. It shares no provider-listener credentials or code."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [CustomerTelegramWebhookThrottle]

    def post(self, request):
        if not customer_telegram_available():
            return Response({"detail": "not_found"}, status=404)
        expected = str(getattr(settings, "CUSTOMER_TELEGRAM_WEBHOOK_SECRET", "") or "")
        supplied = str(request.META.get("HTTP_X_TELEGRAM_BOT_API_SECRET_TOKEN", "") or "")
        if not expected or not secrets.compare_digest(supplied, expected):
            return Response({"detail": "forbidden"}, status=403)

        # From here on, a deterministic customer/input rejection is ACKNOWLEDGED with 200 (nothing bound, nothing
        # notified, update dropped) so it cannot pin the in-order webhook queue and block later valid /start
        # attempts. NEVER log a chat id, token, or secret — only the sanitised reason code.
        def _ack(reason: str):
            logger.info("customer_telegram_webhook: acknowledged non-actionable update reason=%s", reason)
            return Response({"ok": True, "ignored": True})

        update = request.data if isinstance(request.data, dict) else {}
        message = update.get("message")
        if not isinstance(message, dict):
            return _ack("no_message")
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        if chat.get("type") != "private":
            return _ack("private_chat_required")
        try:
            chat_id = int(chat["id"])
            telegram_user_id = int(sender["id"])
        except (KeyError, TypeError, ValueError):
            return _ack("invalid_private_chat")
        if chat_id != telegram_user_id:
            return _ack("invalid_private_chat")

        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/start "):
            return _ack("unsupported_command")
        parts = text.split(" ", 1)
        raw_token = parts[1].strip() if len(parts) == 2 else ""
        try:
            redeem_connection_token(
                raw_token, chat_id=chat_id, telegram_user_id=telegram_user_id,
                username=sender.get("username", ""), first_name=sender.get("first_name", ""),
            )
        except _PERMANENT_REDEEM_REJECTIONS as exc:
            # Permanent: expired/invalid/consumed token, bad identity, or chat already owned by ANOTHER user
            # (ownership unchanged — cross-user rejection is preserved). Ack so Telegram drops it. Record an
            # operator-only event (DARK-gated, secret-free) so this otherwise-silent 200 is diagnosable —
            # ``telemetry_reason`` distinguishes expired vs replayed vs malformed where available.
            from . import telemetry
            telemetry.connect_rejected(reason=getattr(exc, "telemetry_reason", exc.code))
            return _ack(exc.code)
        except TelegramUnavailable as exc:
            # TRANSIENT: the customer-notification subsystem is temporarily unavailable — a retry can succeed,
            # so return a retryable non-2xx (never mistake this for a customer rejection).
            from . import telemetry
            telemetry.connect_transient(reason=exc.code)
            logger.warning("customer_telegram_webhook: transient unavailable reason=%s", exc.code)
            return Response({"detail": exc.code}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"ok": True})


class CustomerNotificationHealthView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        from .delivery import queue_health
        return Response(queue_health())

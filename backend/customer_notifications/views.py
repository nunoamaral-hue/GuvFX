from __future__ import annotations

import secrets

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle
from rest_framework.views import APIView

from .models import CustomerNotificationPreference
from .services import (
    TelegramConnectionError,
    create_connection_token,
    customer_telegram_available,
    disconnect_telegram,
    redeem_connection_token,
    telegram_settings_for,
)


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
        "telegram_enabled", "trade_opened", "trade_updated", "trade_closed", "strategy_changed",
        "execution_problem", "workspace_ready",
    }

    def patch(self, request):
        pref, _ = CustomerNotificationPreference.objects.get_or_create(user=request.user)
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

        update = request.data if isinstance(request.data, dict) else {}
        message = update.get("message")
        if not isinstance(message, dict):
            return Response({"ok": True, "ignored": True})
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        sender = message.get("from") if isinstance(message.get("from"), dict) else {}
        if chat.get("type") != "private":
            return Response({"detail": "private_chat_required"}, status=400)
        try:
            chat_id = int(chat["id"])
            telegram_user_id = int(sender["id"])
        except (KeyError, TypeError, ValueError):
            return Response({"detail": "invalid_private_chat"}, status=400)
        if chat_id != telegram_user_id:
            return Response({"detail": "invalid_private_chat"}, status=400)

        text = message.get("text")
        if not isinstance(text, str) or not text.startswith("/start "):
            return Response({"ok": True, "ignored": True})
        parts = text.split(" ", 1)
        raw_token = parts[1].strip() if len(parts) == 2 else ""
        try:
            redeem_connection_token(
                raw_token, chat_id=chat_id, telegram_user_id=telegram_user_id,
                username=sender.get("username", ""), first_name=sender.get("first_name", ""),
            )
        except TelegramConnectionError as exc:
            return Response({"detail": exc.code}, status=400)
        return Response({"ok": True})


class CustomerNotificationHealthView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        from .delivery import queue_health
        return Response(queue_health())

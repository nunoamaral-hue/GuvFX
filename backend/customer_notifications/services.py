from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    CustomerNotification,
    CustomerNotificationPreference,
    CustomerTelegramBinding,
    TelegramConnectionToken,
)

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_BOT_RE = re.compile(r"^[A-Za-z0-9_]{5,64}$")
_MAX_TELEGRAM_ID = (2 ** 63) - 1


class TelegramConnectionError(Exception):
    code = "connection_failed"


class TelegramUnavailable(TelegramConnectionError):
    code = "telegram_unavailable"


class InvalidConnectionToken(TelegramConnectionError):
    code = "invalid_or_expired_token"


class TelegramChatAlreadyBound(TelegramConnectionError):
    code = "chat_already_connected"


class InvalidTelegramIdentity(TelegramConnectionError):
    code = "invalid_private_chat"


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("ascii")).hexdigest()


def customer_telegram_available() -> bool:
    username = str(getattr(settings, "CUSTOMER_TELEGRAM_BOT_USERNAME", "") or "").lstrip("@")
    webhook_url = urlparse(str(getattr(settings, "CUSTOMER_TELEGRAM_WEBHOOK_URL", "") or ""))
    return bool(
        getattr(settings, "CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED", False)
        and getattr(settings, "CUSTOMER_TELEGRAM_WORKER_ENABLED", False)
        and _BOT_RE.fullmatch(username)
        and str(getattr(settings, "CUSTOMER_TELEGRAM_BOT_TOKEN", "") or "")
        and str(getattr(settings, "CUSTOMER_TELEGRAM_WEBHOOK_SECRET", "") or "")
        and webhook_url.scheme == "https"
        and webhook_url.netloc
    )


def create_connection_token(user, *, language: str = "en") -> dict:
    """Return a one-time Telegram deep link. The raw token is never persisted."""
    if not customer_telegram_available():
        raise TelegramUnavailable
    language = language if language in ("en", "ja") else "en"
    now = timezone.now()
    ttl = max(60, min(int(getattr(settings, "CUSTOMER_TELEGRAM_TOKEN_TTL_SECONDS", 600)), 1800))
    raw = secrets.token_urlsafe(32)  # 43 URL-safe chars, under Telegram's 64-char limit.
    with transaction.atomic():
        TelegramConnectionToken.objects.filter(
            user=user, consumed_at__isnull=True,
        ).update(consumed_at=now)
        token = TelegramConnectionToken.objects.create(
            user=user, token_digest=_digest(raw), expires_at=now + timedelta(seconds=ttl),
        )
        pref, _ = CustomerNotificationPreference.objects.get_or_create(user=user)
        if pref.language != language:
            pref.language = language
            pref.save(update_fields=["language", "updated_at"])
    username = str(settings.CUSTOMER_TELEGRAM_BOT_USERNAME).lstrip("@")
    return {
        "url": f"https://t.me/{username}?start={raw}",
        "expires_at": token.expires_at,
    }


def redeem_connection_token(
    raw_token: str, *, chat_id: int, telegram_user_id: int,
    username: str = "", first_name: str = "",
) -> CustomerTelegramBinding:
    """Atomically consume a token and bind its owner to Telegram's authoritative private chat."""
    if not isinstance(raw_token, str) or not _TOKEN_RE.fullmatch(raw_token):
        raise InvalidConnectionToken
    if (
        isinstance(chat_id, bool) or isinstance(telegram_user_id, bool)
        or not isinstance(chat_id, int) or not isinstance(telegram_user_id, int)
        or chat_id != telegram_user_id or chat_id <= 0 or chat_id > _MAX_TELEGRAM_ID
    ):
        raise InvalidTelegramIdentity
    now = timezone.now()
    try:
        with transaction.atomic():
            token = (
                TelegramConnectionToken.objects.select_for_update()
                .select_related("user")
                .filter(token_digest=_digest(raw_token))
                .first()
            )
            if (
                token is None or token.consumed_at is not None or token.expires_at <= now
                or not token.user.is_active
            ):
                raise InvalidConnectionToken

            conflict = (
                CustomerTelegramBinding.objects.select_for_update()
                .filter(telegram_chat_id=chat_id, is_active=True)
                .exclude(user_id=token.user_id)
                .exists()
            )
            if conflict:
                raise TelegramChatAlreadyBound

            binding, _ = CustomerTelegramBinding.objects.select_for_update().get_or_create(
                user=token.user,
                defaults={"telegram_chat_id": chat_id, "telegram_user_id": telegram_user_id},
            )
            binding.telegram_chat_id = chat_id
            binding.telegram_user_id = telegram_user_id
            binding.telegram_username = str(username or "")[:64]
            binding.telegram_first_name = str(first_name or "")[:128]
            binding.is_active = True
            binding.connected_at = now
            binding.disconnected_at = None
            binding.save()

            CustomerNotificationPreference.objects.get_or_create(user=token.user)
            token.consumed_at = now
            token.save(update_fields=["consumed_at"])

            transaction.on_commit(
                lambda: safe_enqueue_customer_notification(
                    user_id=token.user_id,
                    event_type=CustomerNotification.EventType.CONNECTION_CONFIRMED,
                    source_object_type="customer_notifications.CustomerTelegramBinding",
                    source_object_id=str(binding.pk),
                    dedupe_key=f"telegram-connected:{binding.pk}:{int(now.timestamp())}",
                    payload={}, force=True,
                ),
                robust=True,
            )
            return binding
    except IntegrityError as exc:
        raise TelegramChatAlreadyBound from exc


def disconnect_telegram(user) -> bool:
    now = timezone.now()
    with transaction.atomic():
        binding = CustomerTelegramBinding.objects.select_for_update().filter(user=user).first()
        changed = bool(binding and binding.is_active)
        if binding:
            binding.is_active = False
            binding.disconnected_at = now
            binding.save(update_fields=["is_active", "disconnected_at", "updated_at"])
        TelegramConnectionToken.objects.filter(
            user=user, consumed_at__isnull=True,
        ).update(consumed_at=now)
    return changed


_PREFERENCE_FIELD = {
    CustomerNotification.EventType.TRADE_OPENED: "trade_opened",
    CustomerNotification.EventType.TRADE_UPDATED: "trade_updated",
    CustomerNotification.EventType.TRADE_CLOSED: "trade_closed",
    CustomerNotification.EventType.STRATEGY_ENABLED: "strategy_changed",
    CustomerNotification.EventType.STRATEGY_DISABLED: "strategy_changed",
    CustomerNotification.EventType.EXECUTION_PROBLEM: "execution_problem",
    CustomerNotification.EventType.WORKSPACE_READY: "workspace_ready",
}

_PAYLOAD_ALLOWLIST = {
    CustomerNotification.EventType.CONNECTION_CONFIRMED: set(),
    CustomerNotification.EventType.TRADE_OPENED: {
        "strategy", "symbol", "side", "volume", "entry", "stop_loss", "take_profit",
        "account_kind", "account_number", "occurred_at",
    },
    CustomerNotification.EventType.TRADE_UPDATED: {
        "strategy", "symbol", "side", "result", "currency", "outcome",
        "progress_label", "progress_closed", "progress_total", "account_kind",
        "account_number", "occurred_at",
    },
    CustomerNotification.EventType.TRADE_CLOSED: {
        "strategy", "symbol", "side", "result", "currency", "outcome",
        "progress_closed", "progress_total", "account_kind", "account_number", "occurred_at",
    },
    CustomerNotification.EventType.STRATEGY_ENABLED: {"strategy", "account_kind", "account_number"},
    CustomerNotification.EventType.STRATEGY_DISABLED: {"strategy", "account_kind", "account_number"},
    CustomerNotification.EventType.EXECUTION_PROBLEM: {"message_code", "strategy"},
    CustomerNotification.EventType.WORKSPACE_READY: {"account_kind", "account_number"},
}


def _safe_payload(event_type: str, payload: dict) -> dict:
    allowed = _PAYLOAD_ALLOWLIST.get(event_type, set())
    out = {}
    for key in allowed:
        value = payload.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, (int, float)):
            out[key] = value
        else:
            out[key] = str(value)[:160]
    return out


def enqueue_customer_notification(
    *, user, event_type: str, source_object_type: str, source_object_id: str,
    dedupe_key: str, payload: dict, account=None, occurred_at=None, force: bool = False,
):
    """Create one isolated outbox row. Never accepts a recipient identifier from a caller."""
    if not getattr(settings, "CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED", False):
        return None
    if not user.is_active:
        return None
    if account is not None and account.user_id != user.id:
        return None
    pref = CustomerNotificationPreference.objects.filter(user=user).first()
    if pref is None:
        return None
    binding = CustomerTelegramBinding.objects.filter(user=user, is_active=True).first()
    if occurred_at is not None and binding is not None and occurred_at < binding.connected_at:
        return None

    allowed = binding is not None and (
        force or (
            pref.telegram_enabled
            and bool(getattr(pref, _PREFERENCE_FIELD.get(event_type, "telegram_enabled"), False))
        )
    )
    status = CustomerNotification.Status.PENDING if allowed else CustomerNotification.Status.SUPPRESSED
    row, _ = CustomerNotification.objects.get_or_create(
        dedupe_key=str(dedupe_key)[:200],
        defaults={
            "user": user,
            "account": account,
            "event_type": event_type,
            "source_object_type": str(source_object_type)[:80],
            "source_object_id": str(source_object_id)[:80],
            "payload": _safe_payload(event_type, payload),
            "language": pref.language if pref.language in ("en", "ja") else "en",
            "status": status,
        },
    )
    return row


def safe_enqueue_customer_notification(*, user_id: int, **kwargs):
    """Fail-open wrapper used by post-commit observers; never raises into an originating workflow."""
    try:
        from django.contrib.auth import get_user_model
        user = get_user_model().objects.filter(pk=user_id).first()
        if user is None:
            return None
        return enqueue_customer_notification(user=user, **kwargs)
    except Exception:
        # Deliberately no payload/token/recipient logging. The reconciler can backfill durable events.
        return None


def telegram_settings_for(user) -> dict:
    binding = CustomerTelegramBinding.objects.filter(user=user, is_active=True).first()
    pref = CustomerNotificationPreference.objects.filter(user=user).first()
    if pref is None:
        pref = CustomerNotificationPreference(user=user)
    return {
        "available": customer_telegram_available(),
        "connected": binding is not None,
        "display": {
            "username": binding.telegram_username if binding else "",
            "first_name": binding.telegram_first_name if binding else "",
        },
        "preferences": {
            "telegram_enabled": pref.telegram_enabled,
            "trade_opened": pref.trade_opened,
            "trade_updated": pref.trade_updated,
            "trade_closed": pref.trade_closed,
            "strategy_changed": pref.strategy_changed,
            "execution_problem": pref.execution_problem,
            "workspace_ready": pref.workspace_ready,
            "language": pref.language,
        },
    }

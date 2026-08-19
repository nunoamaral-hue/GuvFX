from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .messages import render_customer_message
from .models import (
    CustomerNotification,
    CustomerNotificationAttempt,
    CustomerNotificationPreference,
    CustomerNotificationWorkerState,
    CustomerTelegramBinding,
)

logger = logging.getLogger(__name__)

_PREFERENCE_FIELD = {
    CustomerNotification.EventType.TRADE_OPENED: "trade_opened",
    CustomerNotification.EventType.TRADE_UPDATED: "trade_updated",
    CustomerNotification.EventType.TRADE_CLOSED: "trade_closed",
    CustomerNotification.EventType.STRATEGY_ENABLED: "strategy_changed",
    CustomerNotification.EventType.STRATEGY_DISABLED: "strategy_changed",
    CustomerNotification.EventType.EXECUTION_PROBLEM: "execution_problem",
    CustomerNotification.EventType.WORKSPACE_READY: "workspace_ready",
}


class TelegramDeliveryError(Exception):
    def __init__(self, code: str, *, retryable: bool = False, ambiguous: bool = False):
        self.code = code
        self.retryable = retryable
        self.ambiguous = ambiguous
        super().__init__(code)


@dataclass(frozen=True)
class TelegramAck:
    message_id: str


class CustomerTelegramBotClient:
    def __init__(self, token: str | None = None, timeout: int = 10):
        self._token = token if token is not None else str(
            getattr(settings, "CUSTOMER_TELEGRAM_BOT_TOKEN", "") or "")
        self._timeout = timeout

    def send_message(self, chat_id: int, text: str) -> TelegramAck:
        if not self._token:
            raise TelegramDeliveryError("bot_token_missing", retryable=True)
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        body = json.dumps({
            "chat_id": chat_id, "text": text, "disable_web_page_preview": True,
            "protect_content": True,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST", headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as response:
                payload = json.loads((response.read() or b"{}").decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise TelegramDeliveryError(
                f"telegram_http_{exc.code}", retryable=(exc.code == 429 or exc.code >= 500),
            ) from None
        except (urllib.error.URLError, TimeoutError):
            # Ambiguous: Telegram may have accepted the request before the connection failed.
            # Fail terminally so an automatic retry can never duplicate a customer message.
            raise TelegramDeliveryError("telegram_network_ambiguous", ambiguous=True) from None
        except (ValueError, UnicodeError):
            raise TelegramDeliveryError("telegram_response_ambiguous", ambiguous=True) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
            raise TelegramDeliveryError("telegram_response_ambiguous", ambiguous=True)
        if not payload["ok"]:
            code = int(payload.get("error_code") or 0)
            raise TelegramDeliveryError(
                f"telegram_rejected_{code or 'unknown'}", retryable=(code == 429 or code >= 500),
            )
        result = payload.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if message_id is None or isinstance(message_id, bool):
            raise TelegramDeliveryError("telegram_response_ambiguous", ambiguous=True)
        return TelegramAck(message_id=str(message_id))


def _allowed_now(notification) -> tuple[bool, CustomerTelegramBinding | None, str]:
    if notification.user_id is None:
        return False, None, "user_deleted"
    if not notification.user.is_active:
        return False, None, "user_inactive"
    binding = CustomerTelegramBinding.objects.filter(
        user_id=notification.user_id, is_active=True,
    ).first()
    if binding is None:
        return False, None, "disconnected"
    pref = CustomerNotificationPreference.objects.filter(user_id=notification.user_id).first()
    if pref is None:
        return False, binding, "preferences_missing"
    if notification.event_type == CustomerNotification.EventType.CONNECTION_CONFIRMED:
        return True, binding, ""
    if not pref.telegram_enabled:
        return False, binding, "master_disabled"
    field = _PREFERENCE_FIELD.get(notification.event_type)
    if not field or not getattr(pref, field, False):
        return False, binding, "event_disabled"
    if notification.account_id:
        owner = notification.account.user_id if notification.account else None
        if owner != notification.user_id:
            return False, binding, "account_owner_mismatch"
    return True, binding, ""


def _attempt(notification, result: str, *, binding=None, provider_message_id="", error_code=""):
    CustomerNotificationAttempt.objects.create(
        notification=notification, attempt=notification.attempts, result=result,
        recipient_chat_id=(binding.telegram_chat_id if binding else None),
        provider_message_id=str(provider_message_id or "")[:64],
        error_code=str(error_code or "")[:64],
    )


def dispatch_customer_notifications(*, client=None, limit: int = 100) -> dict:
    """Deliver an isolated queue batch. PROCESSING is never automatically reclaimed.

    This deliberate at-most-once posture closes the provider-ack/DB-crash duplicate window: a worker
    crash after Telegram acknowledges leaves PROCESSING for operator review instead of re-sending.
    """
    enabled = bool(getattr(settings, "CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED", False))
    worker_enabled = bool(getattr(settings, "CUSTOMER_TELEGRAM_WORKER_ENABLED", False))
    counts = {"enabled": enabled, "worker_enabled": worker_enabled, "claimed": 0,
              "delivered": 0, "failed": 0, "retrying": 0, "suppressed": 0}
    if not enabled or not worker_enabled:
        return counts
    client = client or CustomerTelegramBotClient()
    now = timezone.now()
    ids = list(CustomerNotification.objects.filter(
        status__in=[CustomerNotification.Status.PENDING, CustomerNotification.Status.RETRYING],
        next_attempt_at__lte=now,
    ).order_by("created_at", "id").values_list("id", flat=True)[:limit])
    max_attempts = max(1, min(int(getattr(settings, "CUSTOMER_NOTIFICATION_MAX_ATTEMPTS", 5)), 10))

    for notification_id in ids:
        claimed = CustomerNotification.objects.filter(
            id=notification_id,
            status__in=[CustomerNotification.Status.PENDING, CustomerNotification.Status.RETRYING],
        ).update(status=CustomerNotification.Status.PROCESSING, attempts=F("attempts") + 1,
                 updated_at=timezone.now())
        if not claimed:
            continue
        counts["claimed"] += 1
        notification = CustomerNotification.objects.select_related("account", "user").get(pk=notification_id)
        allowed, binding, reason = _allowed_now(notification)
        if not allowed:
            with transaction.atomic():
                notification.status = CustomerNotification.Status.SUPPRESSED
                notification.last_error = reason
                notification.save(update_fields=["status", "last_error", "updated_at"])
                _attempt(notification, CustomerNotificationAttempt.Result.SUPPRESSED,
                         binding=binding, error_code=reason)
            counts["suppressed"] += 1
            continue

        try:
            text = render_customer_message(notification)
            ack = client.send_message(binding.telegram_chat_id, text)
        except TelegramDeliveryError as exc:
            retry = exc.retryable and not exc.ambiguous and notification.attempts < max_attempts
            final_status = (CustomerNotification.Status.RETRYING if retry
                            else CustomerNotification.Status.FAILED)
            error_code = exc.code
            if exc.retryable and not exc.ambiguous and not retry:
                error_code = f"retry_exhausted:{exc.code}"[:64]
            with transaction.atomic():
                notification.status = final_status
                notification.last_error = error_code
                if retry:
                    delay = min(3600, 30 * (2 ** max(0, notification.attempts - 1)))
                    notification.next_attempt_at = timezone.now() + timedelta(seconds=delay)
                notification.save(update_fields=[
                    "status", "last_error", "next_attempt_at", "updated_at",
                ])
                _attempt(
                    notification,
                    CustomerNotificationAttempt.Result.RETRY if retry
                    else CustomerNotificationAttempt.Result.FAILED,
                    binding=binding, error_code=error_code,
                )
            counts["retrying" if retry else "failed"] += 1
            continue
        except Exception:
            # Rendering/programming failures are terminal and secret-safe. They do not escape the worker.
            with transaction.atomic():
                notification.status = CustomerNotification.Status.FAILED
                notification.last_error = "render_or_worker_error"
                notification.save(update_fields=["status", "last_error", "updated_at"])
                _attempt(notification, CustomerNotificationAttempt.Result.FAILED,
                         binding=binding, error_code="render_or_worker_error")
            counts["failed"] += 1
            continue

        # Persist the acknowledgement in one transaction. If this DB write fails after Telegram accepted
        # the message, PROCESSING remains and is never automatically retried: at-most-once beats duplication.
        try:
            with transaction.atomic():
                notification.status = CustomerNotification.Status.DELIVERED
                notification.delivered_at = timezone.now()
                notification.last_error = ""
                notification.save(update_fields=[
                    "status", "delivered_at", "last_error", "updated_at",
                ])
                _attempt(notification, CustomerNotificationAttempt.Result.DELIVERED,
                         binding=binding, provider_message_id=ack.message_id)
            counts["delivered"] += 1
        except Exception:
            counts["failed"] += 1
    return counts


def record_worker_heartbeat(state: str) -> bool:
    """Record liveness without ever making notification delivery depend on monitoring storage."""
    try:
        CustomerNotificationWorkerState.objects.update_or_create(
            key="delivery",
            defaults={"last_heartbeat_at": timezone.now(), "last_cycle_state": state},
        )
        return True
    except Exception:
        logger.exception("customer notification heartbeat write failed")
        return False


def queue_health() -> dict:
    now = timezone.now()
    base = CustomerNotification.objects
    pending_states = [
        CustomerNotification.Status.PENDING,
        CustomerNotification.Status.PROCESSING,
        CustomerNotification.Status.RETRYING,
    ]
    oldest = base.filter(status__in=pending_states).order_by("created_at").values_list(
        "created_at", flat=True).first()
    queue_depth = base.filter(status__in=pending_states).count()
    delivered = base.filter(status=CustomerNotification.Status.DELIVERED).count()
    failed = base.filter(status=CustomerNotification.Status.FAILED).count()
    oldest_age = max(0, int((now - oldest).total_seconds())) if oldest else 0
    ambiguous = CustomerNotificationAttempt.objects.filter(
        error_code__in=["telegram_network_ambiguous", "telegram_response_ambiguous"],
    ).count()
    exhausted = CustomerNotificationAttempt.objects.filter(
        error_code__startswith="retry_exhausted:",
    ).count()
    worker = CustomerNotificationWorkerState.objects.filter(key="delivery").first()
    heartbeat = worker.last_heartbeat_at if worker else None
    heartbeat_age = max(0, int((now - heartbeat).total_seconds())) if heartbeat else None
    return {
        "feature_enabled": bool(getattr(settings, "CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED", False)),
        "worker_enabled": bool(getattr(settings, "CUSTOMER_TELEGRAM_WORKER_ENABLED", False)),
        "binding_count": CustomerTelegramBinding.objects.count(),
        "active_binding_count": CustomerTelegramBinding.objects.filter(is_active=True).count(),
        "worker_last_heartbeat_at": heartbeat.isoformat() if heartbeat else None,
        "worker_heartbeat_age_seconds": heartbeat_age,
        "worker_last_cycle_state": worker.last_cycle_state if worker else None,
        "queue_depth": queue_depth,
        "pending": queue_depth,
        "processing": base.filter(status=CustomerNotification.Status.PROCESSING).count(),
        "delivered": delivered,
        "delivery_success_count": delivered,
        "failed": failed,
        "delivery_failure_count": failed,
        "retrying": base.filter(status=CustomerNotification.Status.RETRYING).count(),
        "suppressed": base.filter(status=CustomerNotification.Status.SUPPRESSED).count(),
        "ambiguous_delivery_count": ambiguous,
        "retry_exhaustion_count": exhausted,
        "oldest_queued_age_seconds": oldest_age,
        "oldest_pending_age_seconds": oldest_age,
    }

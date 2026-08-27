from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    CustomerNotification,
    CustomerNotificationPreference,
    CustomerStrategyNotificationPreference,
    CustomerTelegramBinding,
    TelegramConnectionToken,
    WorkspaceReadinessNotificationIntent,
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

    def __init__(self, telemetry_reason: str = ""):
        # ``code`` (and thus the customer-facing/HTTP behaviour) is unchanged; ``telemetry_reason``
        # only distinguishes expired vs replayed vs malformed vs unknown for the operator timeline.
        super().__init__()
        self.telemetry_reason = telemetry_reason or self.code


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
    from . import telemetry
    telemetry.token_created(user_id=user.id, token_pk=token.pk, ttl_seconds=ttl,
                            actor=str(getattr(user, "email", "") or ""))
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
        raise InvalidConnectionToken("token_malformed")
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
            # Distinct telemetry reasons (same exception type, same ``code``, same HTTP behaviour), in
            # the original short-circuit order (None first — guards the attribute reads below).
            if token is None:
                raise InvalidConnectionToken("token_unknown")
            if token.consumed_at is not None:
                raise InvalidConnectionToken("token_replayed")
            if token.expires_at <= now:
                raise InvalidConnectionToken("token_expired")
            if not token.user.is_active:
                raise InvalidConnectionToken("user_inactive")

            conflict = (
                CustomerTelegramBinding.objects.select_for_update()
                .filter(telegram_chat_id=chat_id, is_active=True)
                .exclude(user_id=token.user_id)
                .exists()
            )
            if conflict:
                raise TelegramChatAlreadyBound

            binding, created = CustomerTelegramBinding.objects.select_for_update().get_or_create(
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
                lambda: _after_binding_connected(token.user_id, binding.pk, now, created=created),
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


_PROHIBITED_EVENT_TYPES = {
    CustomerNotification.EventType.TRADE_OPENED,
    "SIGNAL_RECEIVED",
    "RAW_SIGNAL",
    "TRADE_ENTRY",
}
_ALLOWED_EVENT_TYPES = {
    CustomerNotification.EventType.CONNECTION_CONFIRMED,
    CustomerNotification.EventType.TRADE_UPDATED,
    CustomerNotification.EventType.TRADE_CLOSED,
    CustomerNotification.EventType.STRATEGY_ENABLED,
    CustomerNotification.EventType.STRATEGY_DISABLED,
    CustomerNotification.EventType.EXECUTION_PROBLEM,
    CustomerNotification.EventType.WORKSPACE_READY,
}
_STRATEGY_RESULT_EVENTS = {
    CustomerNotification.EventType.TRADE_UPDATED,
    CustomerNotification.EventType.TRADE_CLOSED,
}
_SYSTEM_EVENTS = {
    CustomerNotification.EventType.STRATEGY_ENABLED,
    CustomerNotification.EventType.STRATEGY_DISABLED,
    CustomerNotification.EventType.EXECUTION_PROBLEM,
}
_SAFE_PROGRESS_RE = re.compile(r"^TP[1-9][0-9]*$")
_SAFE_SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,23}$")
_SAFE_CURRENCY_RE = re.compile(r"^[A-Z]{3,8}$")
_MAX_ABSOLUTE_AMOUNT = Decimal("1000000000000000")

_PAYLOAD_ALLOWLIST = {
    CustomerNotification.EventType.CONNECTION_CONFIRMED: set(),
    CustomerNotification.EventType.TRADE_UPDATED: {
        "strategy", "symbol", "result", "currency", "outcome",
        "progress_label", "progress_closed", "progress_total", "account_kind",
        "account_number", "occurred_at",
    },
    CustomerNotification.EventType.TRADE_CLOSED: {
        "strategy", "symbol", "result", "currency", "outcome", "volume",
        "progress_closed", "progress_total", "account_kind", "account_number", "occurred_at",
    },
    CustomerNotification.EventType.STRATEGY_ENABLED: {"strategy", "account_kind", "account_number"},
    CustomerNotification.EventType.STRATEGY_DISABLED: {"strategy", "account_kind", "account_number"},
    CustomerNotification.EventType.EXECUTION_PROBLEM: {"message_code"},
    CustomerNotification.EventType.WORKSPACE_READY: {"continue_url"},
}


def _decimal_result(payload: dict) -> Decimal | None:
    try:
        value = Decimal(str(payload.get("result")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and abs(value) <= _MAX_ABSOLUTE_AMOUNT else None


def _decimal_text(value, *, positive: bool = False) -> str | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or abs(parsed) > _MAX_ABSOLUTE_AMOUNT:
        return None
    if positive and parsed <= 0:
        return None
    return format(parsed, "f")


def _safe_symbol(value) -> str | None:
    text = str(value or "").strip()
    return text if _SAFE_SYMBOL_RE.fullmatch(text) else None


def _safe_currency(value) -> str | None:
    text = str(value or "").strip().upper()
    return text if _SAFE_CURRENCY_RE.fullmatch(text) else None


def _safe_occurred_at(value) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.isoformat()


def _classified_outcome(payload: dict) -> str | None:
    value = _decimal_result(payload)
    if value is None:
        return None
    if value > 0:
        return "WIN"
    if value < 0:
        return "LOSS"
    return "BREAKEVEN"


def _safe_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def event_safety_decision(event_type: str, payload: dict, *, force: bool = False) -> tuple[bool, str]:
    """Server-side product policy. Unknown and live-entry events always fail closed."""
    if event_type in _PROHIBITED_EVENT_TYPES:
        return False, "live_signal_prohibited"
    if event_type not in _ALLOWED_EVENT_TYPES:
        return False, "event_not_permitted"
    if not isinstance(payload, dict):
        return False, "payload_invalid"
    if event_type == CustomerNotification.EventType.CONNECTION_CONFIRMED:
        return (True, "") if force else (False, "essential_event_requires_authority")
    if event_type == CustomerNotification.EventType.WORKSPACE_READY:
        url = str(payload.get("continue_url") or "")
        expected = str(getattr(settings, "FRONTEND_BASE_URL", "https://guvfx.com")).rstrip("/")
        safe = force and url == f"{expected}/onboarding/hosted"
        return (True, "") if safe else (False, "readiness_intent_required")
    if event_type in _STRATEGY_RESULT_EVENTS:
        if not str(payload.get("strategy") or "").strip() or _safe_symbol(payload.get("symbol")) is None:
            return False, "durable_result_incomplete"
        if _decimal_result(payload) is None:
            return False, "durable_result_incomplete"
        if payload.get("currency") not in (None, "") and _safe_currency(payload.get("currency")) is None:
            return False, "durable_metadata_invalid"
        if payload.get("occurred_at") not in (None, "") and _safe_occurred_at(payload.get("occurred_at")) is None:
            return False, "durable_metadata_invalid"
        if payload.get("volume") not in (None, "") and _decimal_text(payload.get("volume"), positive=True) is None:
            return False, "durable_metadata_invalid"
        closed = _safe_int(payload.get("progress_closed"))
        total = _safe_int(payload.get("progress_total"))
        if closed is None or total is None or total <= 0 or closed <= 0 or closed > total:
            return False, "durable_progress_incomplete"
        if event_type == CustomerNotification.EventType.TRADE_UPDATED:
            label = str(payload.get("progress_label") or "")
            if closed >= total or not _SAFE_PROGRESS_RE.fullmatch(label):
                return False, "unsafe_tp_progress"
        elif closed != total:
            return False, "strategy_outcome_not_final"
    return True, ""


def _event_preference_enabled(pref, event_type: str, payload: dict) -> bool:
    if event_type == CustomerNotification.EventType.TRADE_UPDATED:
        return bool(pref.tp_progress)
    if event_type == CustomerNotification.EventType.TRADE_CLOSED:
        return bool(pref.winning_trades if _classified_outcome(payload) == "WIN" else pref.losing_trades)
    if event_type in _SYSTEM_EVENTS:
        return bool(pref.system_messages)
    return True


def _safe_payload(event_type: str, payload: dict, *, account=None, strategy_assignment=None) -> dict:
    if not isinstance(payload, dict):
        return {}
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
    if event_type in _STRATEGY_RESULT_EVENTS:
        outcome = _classified_outcome(payload)
        if outcome:
            out["outcome"] = outcome
        out["result"] = _decimal_text(payload.get("result"))
        out["symbol"] = _safe_symbol(payload.get("symbol"))
        currency = _safe_currency(payload.get("currency"))
        occurred_at = _safe_occurred_at(payload.get("occurred_at"))
        volume = _decimal_text(payload.get("volume"), positive=True)
        if currency:
            out["currency"] = currency
        else:
            out.pop("currency", None)
        if occurred_at:
            out["occurred_at"] = occurred_at
        else:
            out.pop("occurred_at", None)
        if volume and event_type == CustomerNotification.EventType.TRADE_CLOSED:
            out["volume"] = volume
        else:
            out.pop("volume", None)
        out["progress_closed"] = _safe_int(payload.get("progress_closed"))
        out["progress_total"] = _safe_int(payload.get("progress_total"))
        if event_type == CustomerNotification.EventType.TRADE_UPDATED:
            out["progress_label"] = str(payload.get("progress_label") or "")
    # Customer/account identity is never caller-authored. When an event carries these
    # fields, replace them with the owner-scoped durable records before persistence.
    if account is not None:
        if "currency" in allowed:
            out["currency"] = _safe_currency(account.account_currency) or "USD"
        if "account_kind" in allowed:
            out["account_kind"] = "demo" if account.is_demo else "trading"
        if "account_number" in allowed:
            number = str(account.account_number or "")[:64]
            if number:
                out["account_number"] = number
            else:
                out.pop("account_number", None)
    if "strategy" in allowed:
        strategy = (
            str(strategy_assignment.strategy.name or "")[:160]
            if strategy_assignment is not None else "GuvFX"
        )
        if strategy:
            out["strategy"] = strategy
        else:
            out.pop("strategy", None)
    if event_type == CustomerNotification.EventType.EXECUTION_PROBLEM:
        code = str(payload.get("message_code") or "")
        out["message_code"] = code if code in {
            "workspace_attention", "trade_not_placed", "temporarily_unavailable",
        } else "temporarily_unavailable"
    return out


def enqueue_customer_notification(
    *, user, event_type: str, source_object_type: str, source_object_id: str,
    dedupe_key: str, payload: dict, account=None, strategy_assignment=None,
    occurred_at=None, force: bool = False,
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

    safe, reason = event_safety_decision(event_type, payload, force=force)
    if event_type in _STRATEGY_RESULT_EVENTS:
        assignment_valid = bool(
            strategy_assignment is not None and account is not None
            and strategy_assignment.account_id == account.id
            and strategy_assignment.account.user_id == user.id
        )
        strategy_enabled = bool(
            assignment_valid and CustomerStrategyNotificationPreference.objects.filter(
                user=user, assignment=strategy_assignment, enabled=True,
            ).exists()
        )
        if not assignment_valid:
            safe, reason = False, "strategy_assignment_required"
        elif not strategy_enabled:
            safe, reason = False, "strategy_notifications_disabled"
    allowed = bool(
        binding is not None and safe
        and (force or (pref.telegram_enabled and _event_preference_enabled(pref, event_type, payload)))
    )
    if not allowed and not reason:
        reason = "disconnected" if binding is None else "event_disabled"
    status = CustomerNotification.Status.PENDING if allowed else CustomerNotification.Status.SUPPRESSED
    row, _ = CustomerNotification.objects.get_or_create(
        dedupe_key=str(dedupe_key)[:200],
        defaults={
            "user": user,
            "account": account,
            "strategy_assignment": strategy_assignment,
            "event_type": event_type,
            "source_object_type": str(source_object_type)[:80],
            "source_object_id": str(source_object_id)[:80],
            "payload": _safe_payload(
                event_type, payload, account=account, strategy_assignment=strategy_assignment,
            ),
            "language": pref.language if pref.language in ("en", "ja") else "en",
            "status": status,
            "last_error": "" if allowed else reason,
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
            "winning_trades": pref.winning_trades,
            "losing_trades": pref.losing_trades,
            "tp_progress": pref.tp_progress,
            "system_messages": pref.system_messages,
            "language": pref.language,
        },
    }


def _after_binding_connected(user_id: int, binding_id: int, connected_at, *, created: bool = True) -> None:
    from . import telemetry
    telemetry.binding_established(user_id=user_id, binding_id=binding_id, created=created,
                                 connected_at=connected_at)
    safe_enqueue_customer_notification(
        user_id=user_id,
        event_type=CustomerNotification.EventType.CONNECTION_CONFIRMED,
        source_object_type="customer_notifications.CustomerTelegramBinding",
        source_object_id=str(binding_id),
        dedupe_key=f"telegram-connected:{binding_id}:{int(connected_at.timestamp())}",
        payload={}, force=True,
    )
    activate_pending_strategy_notifications(user_id)
    fulfill_pending_workspace_readiness(user_id=user_id)


def strategy_notification_settings_for(user, assignment) -> dict:
    pref = CustomerStrategyNotificationPreference.objects.filter(
        user=user, assignment=assignment,
    ).first()
    connected = CustomerTelegramBinding.objects.filter(user=user, is_active=True).exists()
    return {
        "assignment_id": assignment.id,
        "enabled": bool(pref and pref.enabled),
        "pending_enable": bool(pref and pref.pending_enable),
        "telegram_connected": connected,
    }


def set_strategy_notification_preference(user, assignment, *, enabled: bool) -> dict:
    if assignment.account.user_id != user.id:
        raise ValueError("assignment_not_owned")
    connected = CustomerTelegramBinding.objects.filter(user=user, is_active=True).exists()
    pref, _ = CustomerStrategyNotificationPreference.objects.get_or_create(
        user=user, assignment=assignment,
    )
    pref.enabled = bool(enabled and connected)
    pref.pending_enable = bool(enabled and not connected)
    pref.save(update_fields=["enabled", "pending_enable", "updated_at"])
    return strategy_notification_settings_for(user, assignment)


def activate_pending_strategy_notifications(user_id: int) -> int:
    if not CustomerTelegramBinding.objects.filter(user_id=user_id, is_active=True).exists():
        return 0
    return CustomerStrategyNotificationPreference.objects.filter(
        user_id=user_id, assignment__account__user_id=user_id, pending_enable=True,
    ).update(enabled=True, pending_enable=False, updated_at=timezone.now())


def _workspace_ready(workspace) -> bool:
    account = workspace.trading_account
    return bool(
        account.user_id is not None
        and account.workspace_confirmed_at is not None
        and workspace.proj_account_match is True
        and str(workspace.canonical_state) in {"CONNECTED", "EXECUTION_READY", "EXECUTING"}
    )


def enqueue_workspace_readiness_intent(intent_id: int):
    intent = WorkspaceReadinessNotificationIntent.objects.select_related(
        "user", "workspace__trading_account",
    ).filter(pk=intent_id, fulfilled_at__isnull=True).first()
    if intent is None or intent.workspace.trading_account.user_id != intent.user_id:
        return None
    if not _workspace_ready(intent.workspace):
        return None
    binding = CustomerTelegramBinding.objects.filter(user_id=intent.user_id, is_active=True).first()
    if binding is None:
        return None
    prior = CustomerNotification.objects.filter(
        source_object_type="customer_notifications.WorkspaceReadinessNotificationIntent",
        source_object_id=str(intent.pk),
        status__in=[
            CustomerNotification.Status.PENDING,
            CustomerNotification.Status.PROCESSING,
            CustomerNotification.Status.DELIVERED,
        ],
    ).order_by("-id").first()
    if prior is not None:
        if prior.status == CustomerNotification.Status.DELIVERED and intent.fulfilled_at is None:
            intent.fulfilled_at = prior.delivered_at or timezone.now()
            intent.save(update_fields=["fulfilled_at", "updated_at"])
        return prior
    base = str(getattr(settings, "FRONTEND_BASE_URL", "https://guvfx.com")).rstrip("/")
    generation = int(binding.connected_at.timestamp())
    return enqueue_customer_notification(
        user=intent.user,
        account=intent.workspace.trading_account,
        event_type=CustomerNotification.EventType.WORKSPACE_READY,
        source_object_type="customer_notifications.WorkspaceReadinessNotificationIntent",
        source_object_id=str(intent.pk),
        dedupe_key=f"customer-workspace-ready:{intent.pk}:{generation}",
        payload={"continue_url": f"{base}/onboarding/hosted"},
        force=True,
    )


def fulfill_pending_workspace_readiness(*, user_id: int | None = None, workspace_id: int | None = None) -> int:
    intents = WorkspaceReadinessNotificationIntent.objects.filter(fulfilled_at__isnull=True)
    if user_id is not None:
        intents = intents.filter(user_id=user_id)
    if workspace_id is not None:
        intents = intents.filter(workspace_id=workspace_id)
    count = 0
    for intent_id in intents.order_by("id").values_list("id", flat=True):
        if enqueue_workspace_readiness_intent(intent_id) is not None:
            count += 1
    return count


def workspace_readiness_settings_for(user) -> dict:
    from hosted_workspace.models import HostedMt5Workspace

    workspace = HostedMt5Workspace.objects.select_related("trading_account").filter(
        trading_account__user=user,
    ).order_by("id").first()
    intent = WorkspaceReadinessNotificationIntent.objects.filter(
        user=user, workspace=workspace,
    ).first() if workspace else None
    return {
        "available": customer_telegram_available(),
        "has_workspace": workspace is not None,
        "requested": intent is not None,
        "fulfilled": bool(intent and intent.fulfilled_at),
        "workspace_ready": bool(workspace and _workspace_ready(workspace)),
        "telegram_connected": CustomerTelegramBinding.objects.filter(user=user, is_active=True).exists(),
    }


def request_workspace_readiness_notification(user, *, language: str) -> dict:
    from hosted_workspace.models import HostedMt5Workspace

    workspace = HostedMt5Workspace.objects.select_related("trading_account").filter(
        trading_account__user=user,
    ).order_by("id").first()
    if workspace is None:
        raise ValueError("workspace_not_found")
    intent, _ = WorkspaceReadinessNotificationIntent.objects.get_or_create(
        user=user, workspace=workspace,
    )
    pref, _ = CustomerNotificationPreference.objects.get_or_create(user=user)
    language = language if language in ("en", "ja") else "en"
    if pref.language != language:
        pref.language = language
        pref.save(update_fields=["language", "updated_at"])
    connected = CustomerTelegramBinding.objects.filter(user=user, is_active=True).exists()
    connect = None
    if connected:
        enqueue_workspace_readiness_intent(intent.id)
    else:
        connect = create_connection_token(user, language=language)
    return {**workspace_readiness_settings_for(user), "connect_url": connect["url"] if connect else None}

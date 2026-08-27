"""Operator-facing, secret-free telemetry for the customer Telegram *connection* lifecycle.

Reuses the single operational-event recorder (``operational_events.events.record_event``): DARK behind
``OPERATIONS_EVENTS_ENABLED`` (default OFF -> byte-for-byte no-op), fail-open, secret-scrubbed. The
*delivery* half of the lifecycle (enqueued / delivered / suppressed+reason / failed+reason) is already
durably observable via ``CustomerNotification`` + the immutable ``CustomerNotificationAttempt``; this
module fills the previously-silent *connection* half (token created, binding established, redeem
rejected+reason, transient).

Two load-bearing safety rules, both enforced here so callers cannot get them wrong:

  * ``customer_visible=False`` on EVERY emit. ``CATEGORY_CONNECTIVITY`` defaults ``customer_visible=True``,
    and a ``customer_visible`` + non-INFO ``OperationalEvent`` would fire a spurious customer
    "trading needs attention" Telegram message via ``customer_notifications.signals``. Connection
    lifecycle is operator-only.
  * NEVER a raw token, token digest, chat id, telegram user id, or username in metadata — only opaque
    row ids (``user_id``, ``binding_id``) and small counts/flags. (The recorder additionally scrubs any
    key containing ``token``/``auth``/``secret``/... as defence-in-depth, so such keys are avoided here.)
"""
from __future__ import annotations

SOURCE_CUSTOMER_TELEGRAM = "customer_telegram"


def _emit(*, event_type: str, severity: str = "INFO", reason_code: str = "",
          actor: str = "", metadata: dict | None = None, dedup_key: str = "") -> None:
    """DARK-gated, fail-open, operator-only connectivity emit. Never raises into the connection flow."""
    try:
        from operational_events.constants import CATEGORY_CONNECTIVITY
        from operational_events.events import record_event
        record_event(
            category=CATEGORY_CONNECTIVITY, event_type=event_type, severity=severity,
            source=SOURCE_CUSTOMER_TELEGRAM, reason_code=reason_code, actor=actor,
            customer_visible=False, metadata=metadata or {}, dedup_key=dedup_key,
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the connection flow
        pass


def token_created(*, user_id: int, token_pk: int, ttl_seconds: int, actor: str = "") -> None:
    _emit(event_type="telegram_token_created", severity="INFO", actor=actor,
          metadata={"user_id": user_id, "ttl_seconds": ttl_seconds},
          dedup_key=f"tg-token-created:{token_pk}")


def binding_established(*, user_id: int, binding_id: int, created: bool, connected_at=None) -> None:
    # Key by the connect instant (microseconds) so a retried on_commit for the SAME connect collapses,
    # but each genuine reconnect (a new connected_at) is its own event — repeated disconnect/reconnect
    # cycles stay visible on the operator timeline instead of collapsing into one event forever.
    ts = ""
    try:
        if connected_at is not None:
            ts = str(int(connected_at.timestamp() * 1_000_000))
    except Exception:  # noqa: BLE001 — never let a bad timestamp break telemetry
        ts = ""
    _emit(event_type=("telegram_binding_created" if created else "telegram_binding_reconnected"),
          severity="INFO",
          metadata={"user_id": user_id, "binding_id": binding_id, "created": bool(created)},
          dedup_key=f"tg-binding:{binding_id}:{ts}")


def connect_rejected(*, reason: str) -> None:
    """A deterministic redemption rejection the customer cannot see server-side (expired / replayed /
    malformed token, or the chat is already bound to another GuvFX account). Operator-visible only."""
    _emit(event_type="telegram_connect_rejected", severity="WARNING", reason_code=str(reason or ""))


def connect_transient(*, reason: str) -> None:
    _emit(event_type="telegram_connect_transient", severity="ERROR", reason_code=str(reason or ""))

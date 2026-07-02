"""
SIGNAL-ACQUISITION-MVP — the dispatcher (Phase 1, repo-only, no order).

``acquire_message(provider, message)`` is the single place message policy lives:
dedup → provider-armed → staleness window → edit/media guard → parser dispatch →
route (tradeable signal → intake / update → recorded / else → quarantine) →
watermark + last_signal_at. It is FAIL-CLOSED: anything untrusted, stale, edited,
media, unknown, malformed, or from a non-armed provider becomes a QUARANTINED /
STALE / DROPPED_NOT_ARMED ledger row — never a silently-processed signal, never an
order. The Phase-3 Telethon listener merely supplies the ``message`` dict.

BOUNDARY: this module imports ONLY ``signal_intake`` (+ the shared parser). It does
NOT import ``execution`` and cannot place an order (enforced by tests).

``message`` dict shape (listener supplies; tests use fixtures):
    {message_id, chat_id, text, date, reply_to_message_id, edit_date, media}
"""

from __future__ import annotations

import datetime as _dt

from django.utils import timezone

from intelligence.telegram_source import Kind

from .models import AcquiredMessage, SignalProvider, SignalUpdate
from .parsers import get_parser
from . import services


def _to_aware(value):
    """Coerce an epoch int/float or datetime into an aware datetime, else None."""
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            return _dt.datetime.fromtimestamp(value, _dt.timezone.utc)
        if timezone.is_naive(value):
            return timezone.make_aware(value, _dt.timezone.utc)
        return value
    except Exception:
        return None


def _record_update(provider, message, parsed, mid, chat_id):
    kind_map = {"TP_HIT": SignalUpdate.Kind.TP_HIT, "MOVE_SL": SignalUpdate.Kind.MOVE_SL}
    SignalUpdate.objects.create(
        provider=provider, chat_id=chat_id, message_id=mid,
        reply_to_message_id=str(message.get("reply_to_message_id") or ""),
        kind=kind_map.get(getattr(parsed, "update_type", ""), SignalUpdate.Kind.OTHER),
        raw_payload={"raw_text": getattr(parsed, "raw_text", "")},
    )


def _classify(provider, message, mid, chat_id, tg_date, now):
    """Return (outcome, reason, approval). FAIL-CLOSED — any error → QUARANTINED."""
    O = AcquiredMessage.Outcome
    try:
        if not provider.is_armed():
            return O.DROPPED_NOT_ARMED, f"provider status={provider.status}", None

        # Staleness window (Nuno's 5-10 min rule; per-provider override).
        window = provider.acquisition_window_seconds or 600
        if tg_date is not None and (now - tg_date).total_seconds() > window:
            return O.STALE, f"age>{window}s", None

        # Edit guard — an edited signal is suspicious; never mutate the original.
        if message.get("edit_date"):
            return O.QUARANTINED, "edited_message", None
        # Media / screenshot — no OCR in MVP.
        if message.get("media"):
            return O.QUARANTINED, "media", None

        text = message.get("text") or ""
        if not text.strip():
            return O.QUARANTINED, "empty_text", None

        parsed = get_parser(provider.parser_profile.slug)(text, mid)  # never raises here
        if parsed.kind == Kind.SIGNAL and parsed.is_tradeable_shape():
            approval = services.intake_parsed(parsed, provider=provider)
            return O.INTAKEN, "", approval
        if parsed.kind == Kind.UPDATE:
            _record_update(provider, message, parsed, mid, chat_id)
            return O.UPDATE, getattr(parsed, "update_type", "") or "update", None
        return O.QUARANTINED, getattr(parsed, "reason", "") or "not_tradeable", None
    except Exception as exc:  # unknown parser / malformed → fail closed
        return O.QUARANTINED, f"dispatch_error:{type(exc).__name__}", None


def acquire_message(provider: SignalProvider, message: dict, *, now=None) -> AcquiredMessage:
    """Acquire one provider message into the ledger (idempotent, fail-closed).

    Returns the ``AcquiredMessage`` row (existing one on a duplicate). Places no
    order and never raises for a bad message.
    """
    now = now or timezone.now()
    mid = str(message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or "")
    tg_date = _to_aware(message.get("date"))

    # Dedup: one ledger row per (provider, message_id). A re-sent message (catch-up
    # replay) returns the existing row and is NOT reprocessed.
    existing = AcquiredMessage.objects.filter(provider=provider, message_id=mid).first()
    if existing is not None:
        return existing

    outcome, reason, approval = _classify(provider, message, mid, chat_id, tg_date, now)

    acq = AcquiredMessage.objects.create(
        provider=provider, chat_id=chat_id, message_id=mid,
        outcome=outcome, reason=reason, telegram_date=tg_date,
        raw_payload={
            "text": message.get("text") or "",
            "edit_date": bool(message.get("edit_date")),
            "reply_to_message_id": message.get("reply_to_message_id"),
            "media": bool(message.get("media")),
        },
        approval=approval,
    )

    # Bookkeeping: advance watermark + provider health, except for a non-armed drop
    # (a paused/retired provider isn't a live signal source).
    if outcome != AcquiredMessage.Outcome.DROPPED_NOT_ARMED:
        provider.watermark_last_message_id = mid
        provider.last_signal_at = now
        provider.save(update_fields=["watermark_last_message_id", "last_signal_at", "updated_at"])

    return acq

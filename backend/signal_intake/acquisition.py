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
import hashlib
import logging

from django.db import IntegrityError, transaction
from django.utils import timezone

from intelligence.telegram_source import Kind

from .models import AcquiredMessage, MessageAmendment, SignalProvider, SignalUpdate
from .parsers import get_parser
from . import services

logger = logging.getLogger(__name__)

# Whitelisted, scalar media-reference fields the listener may supply — everything
# else is dropped so raw image BYTES / large blobs never reach the DB (data.md).
_MEDIA_REF_FIELDS = ("file_id", "file_unique_id", "type", "mime_type",
                     "width", "height", "duration", "size")


def _media_evidence(media):
    """Coerce listener-supplied media into a SAFE, bounded REFERENCE (never bytes).

    Retains only a small reference so the media is evidence, not stored content
    (WAYOND-EDIT-MEDIA policy / data.md: no bulk/binary in the DB)."""
    if not media:
        return None
    if isinstance(media, bool):
        return True
    if isinstance(media, (int, str)):
        return str(media)[:256]
    if isinstance(media, dict):
        ref = {}
        for k in _MEDIA_REF_FIELDS:
            v = media.get(k)
            if isinstance(v, (bool, int)):
                ref[k] = v
            elif isinstance(v, str):
                ref[k] = v[:256]
        return ref or {"present": True}
    return {"present": True}  # unknown shape -> record presence only, never the blob


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


def _record_update(provider, message, parsed, mid, chat_id, *, edited=False):
    kind_map = {"TP_HIT": SignalUpdate.Kind.TP_HIT, "MOVE_SL": SignalUpdate.Kind.MOVE_SL}
    reply_to = str(message.get("reply_to_message_id") or "")
    # Link to the originating signal's acquired-message where the reply metadata
    # resolves (soft link — never fabricated; None when the original isn't present).
    origin_id = None
    if reply_to:
        origin_id = (AcquiredMessage.objects
                     .filter(provider=provider, message_id=reply_to)
                     .values_list("id", flat=True).first())
    SignalUpdate.objects.create(
        provider=provider, chat_id=chat_id, message_id=mid,
        reply_to_message_id=reply_to,
        kind=kind_map.get(getattr(parsed, "update_type", ""), SignalUpdate.Kind.OTHER),
        raw_payload={"raw_text": getattr(parsed, "raw_text", ""),
                     "edited": bool(edited),
                     "origin_acquired_id": origin_id},
    )


def _record_amendment(provider, original, message, mid, tg_date, now):
    """Record an immutable amendment for an edit to an ALREADY-acquired message.

    Never overwrites the original. Re-parses the edited text; if tradeable values
    (entry/SL/TP) changed vs the original approval, flags that approval for human
    RE-REVIEW (never auto-applies the edited values); an edited update records an
    amended SignalUpdate (record-only). Idempotent per (original, edit content).
    """
    edited_text = message.get("text") or ""
    edit_hash = hashlib.sha256(edited_text.encode("utf-8")).hexdigest()
    if MessageAmendment.objects.filter(original=original, edit_hash=edit_hash).exists():
        return None  # same edit re-delivered → already recorded

    try:
        parsed = get_parser(provider.parser_profile.slug)(edited_text, mid)
    except Exception:  # unknown parser / malformed → record the amendment, parse UNKNOWN
        parsed = None

    changed_fields, reflagged, amended_update = {}, False, False
    approval = original.approval
    if (parsed is not None and parsed.kind == Kind.SIGNAL
            and parsed.is_tradeable_shape() and approval is not None):
        new_tp = parsed.take_profits[0] if parsed.take_profits else ""
        for field, old, new in (("entry", approval.entry, parsed.entry),
                                 ("stop_loss", approval.stop_loss, parsed.stop_loss),
                                 ("take_profit", approval.take_profit, new_tp)):
            if str(old or "") != str(new or ""):
                changed_fields[field] = [old, new]
        if changed_fields and not approval.source_edited:
            # Flag for HUMAN RE-REVIEW — never auto-apply the edited values.
            approval.source_edited = True
            approval.save(update_fields=["source_edited"])
        reflagged = bool(changed_fields)
    if parsed is not None and parsed.kind == Kind.UPDATE:
        _record_update(provider, message, parsed, mid, str(message.get("chat_id") or ""),
                       edited=True)
        amended_update = True

    logger.warning("wayond edit-after-acquisition amendment: provider=%s message_id=%s "
                   "changed=%s reflagged=%s", getattr(provider, "slug", "?"), mid,
                   sorted(changed_fields), reflagged)
    try:
        with transaction.atomic():
            return MessageAmendment.objects.create(
                provider=provider, original=original, message_id=mid,
                edit_hash=edit_hash, edited_text=edited_text,
                edit_date=_to_aware(message.get("edit_date")),
                reparsed_kind=(parsed.kind if parsed is not None else "UNKNOWN"),
                changed_fields=changed_fields, approval_reflagged=reflagged,
                raw_payload={"amended_update": amended_update,
                             "reason": (getattr(parsed, "reason", "") if parsed else "parse_error")},
            )
    except IntegrityError:  # concurrent identical edit won the race
        return MessageAmendment.objects.filter(original=original, edit_hash=edit_hash).first()


def _classify(provider, message, mid, chat_id, tg_date, now):
    """Return (outcome, reason, approval). FAIL-CLOSED — any error → QUARANTINED.

    WAYOND-EDIT-MEDIA policy (ratified, PR #72): media is EVIDENCE retained in
    raw_payload, NOT a hard blocker — a text-bearing media message is parsed, a
    screenshot-only message (media with no parseable text) is quarantined. An EDITED
    message is never auto-intaken: a tradeable signal is surfaced to the existing
    human-approval gate FLAGGED as edited (still requires human approval — never
    auto-traded); an edited update is recorded (never acted). Originals are immutable
    (data.md): an edit produces a new record, never an overwrite.
    """
    O = AcquiredMessage.Outcome
    try:
        if not provider.is_armed():
            return O.DROPPED_NOT_ARMED, f"provider status={provider.status}", None

        # Staleness window (Nuno's 5-10 min rule; per-provider override).
        # FAIL-CLOSED: a message whose date cannot be determined has indeterminate
        # freshness → dismiss as STALE rather than parse it.
        window = provider.acquisition_window_seconds or 600
        if tg_date is None:
            return O.STALE, "indeterminate_date", None
        if (now - tg_date).total_seconds() > window:
            return O.STALE, f"age>{window}s", None

        text = message.get("text") or ""
        has_text = bool(text.strip())
        edited = bool(message.get("edit_date"))
        # Media is EVIDENCE, not a blocker: only screenshot-only / empty messages
        # quarantine here (no OCR in MVP); text-bearing media is parsed below.
        if not has_text:
            return O.QUARANTINED, ("media_only" if message.get("media") else "empty_text"), None

        parsed = get_parser(provider.parser_profile.slug)(text, mid)  # never raises here
        if parsed.kind == Kind.SIGNAL and parsed.is_tradeable_shape():
            # Human-gated intake either way; an edited entry is FLAGGED so the reviewer
            # verifies entry/SL/TP (never auto-applied — approval still required).
            approval = services.intake_parsed(parsed, provider=provider, edited=edited)
            return O.INTAKEN, ("edited_review" if edited else ""), approval
        if parsed.kind == Kind.UPDATE:
            _record_update(provider, message, parsed, mid, chat_id, edited=edited)
            base = getattr(parsed, "update_type", "") or "update"
            return O.UPDATE, (base + "_edited" if edited else base), None
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
        # WAYOND-EDIT-DIFF: an EDIT (edit_date) or a CHANGED body for an already-acquired
        # message records an immutable linked amendment (the original is NEVER
        # overwritten). A true unchanged duplicate just dedups. Never re-processes the
        # original, never auto-applies edited values, never places an order.
        stored_text = ((existing.raw_payload or {}).get("text") or "")
        incoming_text = message.get("text") or ""
        if message.get("edit_date") or incoming_text.strip() != stored_text.strip():
            _record_amendment(provider, existing, message, mid, tg_date, now)
        return existing

    outcome, reason, approval = _classify(provider, message, mid, chat_id, tg_date, now)

    try:
        with transaction.atomic():
            acq = AcquiredMessage.objects.create(
                provider=provider, chat_id=chat_id, message_id=mid,
                outcome=outcome, reason=reason, telegram_date=tg_date,
                raw_payload={
                    "text": message.get("text") or "",
                    "edit_date": bool(message.get("edit_date")),
                    "reply_to_message_id": message.get("reply_to_message_id"),
                    "media": bool(message.get("media")),
                    # WAYOND-EDIT-MEDIA: retain a SAFE, bounded media REFERENCE as
                    # evidence — image BYTES / large blobs are never stored (data.md).
                    "media_evidence": _media_evidence(message.get("media")),
                },
                approval=approval,
            )
    except IntegrityError:
        # A concurrent caller won the (provider, message_id) race — return their
        # row (idempotent; downstream intake_parsed is itself dedup-safe so no
        # duplicate approval results). Never raises for a duplicate.
        return AcquiredMessage.objects.get(provider=provider, message_id=mid)

    # Bookkeeping: advance watermark + provider health, except for a non-armed drop
    # (a paused/retired provider isn't a live signal source).
    if outcome != AcquiredMessage.Outcome.DROPPED_NOT_ARMED:
        provider.watermark_last_message_id = mid
        provider.last_signal_at = now
        provider.save(update_fields=["watermark_last_message_id", "last_signal_at", "updated_at"])

    return acq

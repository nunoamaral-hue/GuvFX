"""
Normalise a Telegram message (Telethon object OR a fixture dict) into the dispatcher
message dict: ``{message_id, chat_id, text, date, reply_to_message_id, edit_date,
media}``. Pure — reads only attributes/keys, NEVER downloads media bytes.
"""

from __future__ import annotations


def _field(raw, *names, default=None):
    """Read the first present attribute/key from ``raw`` (object or dict)."""
    for n in names:
        if isinstance(raw, dict):
            if n in raw and raw[n] is not None:
                return raw[n]
        else:
            v = getattr(raw, n, None)
            if v is not None:
                return v
    return default


def _reply_id(raw):
    # Fixture key, Telethon Message.reply_to_msg_id, or Message.reply_to.reply_to_msg_id.
    rid = _field(raw, "reply_to_message_id", "reply_to_msg_id")
    if rid is None:
        reply_to = _field(raw, "reply_to")
        if reply_to is not None:
            rid = getattr(reply_to, "reply_to_msg_id", None)
    return rid


def _media_ref(raw):
    """A SMALL media REFERENCE (never bytes). Fixtures may already carry a ref; a
    Telethon media object is reduced to ``{type, id}`` without any download."""
    media = _field(raw, "media")
    if not media:
        return None
    if isinstance(media, (bool, str, int, dict)):
        return media  # fixture-supplied reference / flag
    ref = {"type": type(media).__name__}
    for holder in ("photo", "document", "webpage"):
        obj = getattr(media, holder, None)
        obj_id = getattr(obj, "id", None) if obj is not None else None
        if obj_id is not None:
            ref["id"] = obj_id
            break
    return ref


def normalize_message(raw) -> dict:
    """Map a raw Telegram message (Telethon object or fixture dict) to the dispatcher
    message dict. Text/media are read-only; media is a reference, never bytes."""
    return {
        "message_id": _field(raw, "id", "message_id"),
        "chat_id": _field(raw, "chat_id"),
        "text": _field(raw, "message", "text", default="") or "",
        "date": _field(raw, "date"),
        "reply_to_message_id": _reply_id(raw),
        "edit_date": _field(raw, "edit_date"),
        "media": _media_ref(raw),
    }

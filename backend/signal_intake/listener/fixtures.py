"""
Derive listener fixtures from the CERTIFIED Wayond corpus (real message text + demo
transport metadata). Pure — no I/O, no Telegram. Used by the dry-run validation and the
``dump_wayond_fixture`` command. Nothing is fabricated: the text is the certified real
corpus; only harness metadata (sequential ids, a chat id, a timestamp) is synthesised.
"""

from __future__ import annotations


def corpus_to_fixtures(entries, *, chat_id, base_message_id=1000, timestamp=None):
    """Map certified corpus entries to listener message dicts.

    ``timestamp`` (epoch or datetime) is applied as each message's date so a replay is
    fresh (not STALE); pass a recent value for a non-dry-run replay.
    """
    fixtures = []
    for i, e in enumerate(entries):
        meta = e.get("meta") or {}
        fixtures.append({
            "message_id": base_message_id + i,
            "chat_id": chat_id,
            "text": e.get("text", ""),
            "date": timestamp,
            "edit_date": timestamp if meta.get("is_edit") else None,
            "media": {"type": "photo"} if meta.get("media") else None,
            "reply_to_message_id": None,   # corpus records is_reply, not the target id
        })
    return fixtures

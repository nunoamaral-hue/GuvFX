"""
GFX-PKT-SIGNAL-ACQUISITION-LISTENER-BUILD — read-only Telegram listener (repo-only).

A thin, Telethon-FREE adapter that normalises Telegram messages and feeds them to
``signal_intake.acquire_message`` (the only sink). It NEVER sends a Telegram message,
NEVER downloads media bytes, and NEVER imports execution. The single lazy Telethon
import lives in the ``run_wayond_listener`` management command; this package is pure
and fully testable with a fake client.
"""

from .adapter import ListenerNotAuthorized, WayondListener
from .normalize import normalize_message

__all__ = ["WayondListener", "ListenerNotAuthorized", "normalize_message"]

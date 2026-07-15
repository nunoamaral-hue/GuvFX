"""
GFX-PKT-REAL-TELEGRAM-TRANSPORT — the REAL Telegram transport (the ONE network-capable
notification surface) + the transport selector.

This is the ONLY file in ``execution/notifications`` permitted to make a network call. Everything
else (dispatcher, dry-run transport, contracts) stays network-free — the boundary test enforces
that split. Even so, this transport does NOTHING by default: the dispatcher is gated by
``NOTIFICATION_DISPATCH_ENABLED`` (default OFF) AND the selector defaults to the dry-run transport,
so a real message is sent only when an operator BOTH enables dispatch AND selects the real
transport AND supplies a token + chat id. Its PRIMARY output is the visual result CARD
(``build_stakeholder_card`` -> ``sendPhoto``); the ``TelegramRenderer`` text
(``build_telegram_envelope``) is the fallback + the audit record.

Security: the bot token appears only in the Telegram API URL and is NEVER logged, never put in a
``DeliveryResult``/detail, and never raised. Failures carry only the HTTP status code or the
exception type. It sends WIN candidates only (WIN-only is enforced upstream; re-checked here),
fails closed on missing credentials (no network call), and is idempotent (skips a candidate that
was already transmitted). It places no order and publishes nothing to WIMS.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from execution.notifications.contracts import build_telegram_envelope
from execution.notifications.transport import (
    DeliveryResult,
    NotificationTransport,
    TelegramDryRunTransport,
)

_TELEGRAM_API = "https://api.telegram.org"
_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_SEND_TIMEOUT_SECONDS", "10") or 10)
_REAL_CHOICES = {"real", "telegram-real", "telegram"}


class RealTelegramTransport(NotificationTransport):
    """Sends a rendered ``NotificationCandidate`` to the Stakeholder Review Channel via the
    Telegram Bot API. Marks SENT only on API success; FAILED otherwise. Never prints the token."""

    name = "telegram-real"

    def __init__(self, *, token=None, chat_id=None, timeout=None):
        # Credentials come from the environment/secret store — never a literal, never logged.
        self._token = token if token is not None else os.getenv("TELEGRAM_BOT_TOKEN", "")
        self._chat_id = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
        self._timeout = timeout or _TIMEOUT_SECONDS

    def render(self, candidate):
        return build_telegram_envelope(candidate)

    def deliver(self, candidate) -> DeliveryResult:
        envelope = self.render(candidate)
        text = envelope.rendered_message

        # Defence in depth — WIN-only. Candidates are WIN-only upstream (outcome_router); refuse
        # anything else here so a real message can never carry a LOSS/BREAKEVEN.
        outcome = getattr(getattr(candidate, "outcome_record", None), "outcome", None)
        if outcome is not None and str(outcome).upper() != "WIN":
            return self._failed(text, f"refused non-WIN outcome ({outcome})")

        # Fail closed on missing credentials — NO network call is made.
        if not self._token or not self._chat_id:
            return self._failed(text, "missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

        # Idempotency belt: never re-transmit a candidate already transmitted once.
        try:
            if candidate.deliveries.filter(transmitted=True).exists():
                return DeliveryResult(
                    ok=True, status="SENT", transmitted=False, rendered_message=text,
                    detail="already transmitted (idempotent skip)",
                )
        except Exception:  # pragma: no cover - defensive (never block on the dedup read)
            pass

        # The primary stakeholder output is the visual CARD (sendPhoto). If the card cannot be
        # rendered for any reason, fall back to the text message so a WIN always notifies.
        card = None
        try:
            from execution.notifications.contracts import build_stakeholder_card
            card = build_stakeholder_card(candidate)   # (png_bytes, caption)
        except Exception:  # pragma: no cover - defensive; card render must never block a WIN
            card = None

        try:
            if card is not None:
                payload = self._send_photo(card[0], card[1])
                mode = "card"
            else:
                payload = self._send(text)
                mode = "text-fallback"
        except urllib.error.HTTPError as exc:            # status code only — NEVER the token/URL
            return self._failed(text, f"telegram HTTP {exc.code}")
        except urllib.error.URLError as exc:
            return self._failed(text, f"telegram network error: {type(exc.reason).__name__}")
        except Exception as exc:                         # never raise out of a transport
            return self._failed(text, f"telegram send error: {type(exc).__name__}")

        if not payload.get("ok"):
            return self._failed(text, f"telegram api error_code={payload.get('error_code')}")
        # B2: capture the Telegram message id (an integer, no secret) for durable proof-of-delivery.
        message_id = str((payload.get("result") or {}).get("message_id") or "")
        return DeliveryResult(
            ok=True, status="SENT", transmitted=True, rendered_message=text,
            detail=f"sent {mode} to stakeholder review channel", message_id=message_id,
        )

    def _send(self, text: str) -> dict:
        # The token lives ONLY in this URL string and is never logged/returned/raised.
        url = f"{_TELEGRAM_API}/bot{self._token}/sendMessage"
        data = json.dumps(
            {"chat_id": self._chat_id, "text": text, "disable_web_page_preview": True}
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST", headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads((resp.read() or b"{}").decode("utf-8") or "{}")

    def _send_photo(self, png_bytes: bytes, caption: str = "") -> dict:
        """Send the result CARD (a PNG) with a short caption via Telegram ``sendPhoto`` (multipart).

        This is the IMAGE primitive — the polished stakeholder card is the photo, the short caption
        rides alongside. It is the PRIMARY output of ``deliver()`` (WIN notifications send the card;
        ``_send`` text is the fallback), invoked only AFTER deliver()'s WIN-only / credential /
        idempotency gates. The token lives ONLY in the URL and is never logged/returned/raised."""
        url = f"{_TELEGRAM_API}/bot{self._token}/sendPhoto"
        boundary = "----GuvFXCardBoundaryZ7Xq2fVn"

        def _field(name: str, value: str) -> bytes:
            return (f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{name}"\r\n\r\n{value}\r\n').encode("utf-8")

        body = _field("chat_id", str(self._chat_id))
        if caption:
            body += _field("caption", caption)
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                 f"filename=\"trade_card.png\"\r\nContent-Type: image/png\r\n\r\n").encode("utf-8")
        body += png_bytes + b"\r\n" + f"--{boundary}--\r\n".encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return json.loads((resp.read() or b"{}").decode("utf-8") or "{}")

    @staticmethod
    def _failed(text: str, detail: str) -> DeliveryResult:
        return DeliveryResult(
            ok=False, status="FAILED", transmitted=False, rendered_message=text, detail=detail,
        )


def select_transport() -> NotificationTransport:
    """The dispatch transport factory. DEFAULT = dry-run.

    The real transport is chosen ONLY when ``NOTIFICATION_DISPATCH_TRANSPORT`` is explicitly one of
    ``real`` / ``telegram-real`` / ``telegram``. Dispatch is STILL separately gated by
    ``NOTIFICATION_DISPATCH_ENABLED`` (default OFF), so the default posture sends nothing.
    """
    choice = os.getenv("NOTIFICATION_DISPATCH_TRANSPORT", "").strip().lower()
    if choice in _REAL_CHOICES:
        return RealTelegramTransport()
    return TelegramDryRunTransport()

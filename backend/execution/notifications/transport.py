"""TELEGRAM-TRANSPORT-FOUNDATION — the transport-agnostic interface + a DRY-RUN Telegram adapter.

``NotificationTransport`` is the pluggable interface; future adapters (Discord/Slack/Email)
implement it. ``TelegramDryRunTransport`` is the only adapter: it RENDERS the message but NEVER
transmits it — no network, no HTTP client, no Telegram API, no bot token, no chat id. A real,
credential-gated transport is a separate future packet.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from execution.notifications.contracts import (
    TelegramMessageEnvelope,
    build_telegram_envelope,
)


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    status: str          # "SENT" or "FAILED"
    transmitted: bool    # False for dry-run; True only for a real transmission
    rendered_message: str
    detail: str = ""
    message_id: str = ""  # B2: the provider (Telegram) message id of a real transmission, else ""


class NotificationTransport(ABC):
    """A pluggable notification transport.

    ``deliver(candidate)`` renders + (in a real transport) sends, returning a DeliveryResult.
    Implementations MUST NOT place an order, mutate a Trade / TradeOutcomeRecord, or — in this
    foundation — transmit anything.
    """

    name: str = "abstract"

    @abstractmethod
    def deliver(self, candidate) -> DeliveryResult:  # pragma: no cover - interface
        ...


class TelegramDryRunTransport(NotificationTransport):
    """Renders a ``TelegramMessageEnvelope`` and returns SENT WITHOUT transmitting anything.

    It opens no network client, reads no token/chat id, makes no HTTP request. ``transmitted``
    is always False. Swapping in a real transport is a separate, credential-gated packet.
    """

    name = "telegram-dryrun"

    def render(self, candidate) -> TelegramMessageEnvelope:
        return build_telegram_envelope(candidate)

    def deliver(self, candidate) -> DeliveryResult:
        envelope = self.render(candidate)
        # DRY-RUN: the message is rendered but deliberately NOT transmitted.
        return DeliveryResult(
            ok=True,
            status="SENT",
            transmitted=False,
            rendered_message=envelope.rendered_message,
            detail="dry-run: rendered, not transmitted",
        )

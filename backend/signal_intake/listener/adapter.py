"""
WayondListener — the read-only listener core (Telethon-FREE, fully testable).

Feeds normalised messages to ``signal_intake.acquire_message`` (the ONLY sink):
provider lookup by chat_id, watermark catch-up, flood-wait handling, heartbeat, and a
dry-run preview. It never sends a Telegram message, never downloads media bytes, never
imports execution. The real Telethon client + events are BUILT in the management
command and passed into ``run`` — this module stays pure for fake-client testing.
"""

from __future__ import annotations

import logging
import time

from ..acquisition import acquire_message
from ..certification import classify
from ..models import SignalProvider
from .normalize import normalize_message

logger = logging.getLogger(__name__)

_INACTIVE = (SignalProvider.Status.INACTIVE, SignalProvider.Status.RETIRED)


class ListenerNotAuthorized(RuntimeError):
    """The supplied session is not an authorised user session (provision + age first)."""


def _as_int(chat_id):
    try:
        return int(chat_id)
    except (TypeError, ValueError):
        return chat_id


class WayondListener:
    def __init__(self, *, dry_run: bool = False):
        self.dry_run = dry_run

    # --- provider lookup ---------------------------------------------------
    def provider_for_chat(self, chat_id):
        return SignalProvider.objects.filter(telegram_chat_id=str(chat_id)).first()

    def subscribed_chat_ids(self):
        """Chat ids the listener should subscribe to — every non-inactive provider
        with a chat id. (acquire_message still fail-closes on non-armed providers.)"""
        return list(
            SignalProvider.objects.exclude(status__in=_INACTIVE)
            .exclude(telegram_chat_id="")
            .values_list("telegram_chat_id", flat=True)
        )

    # --- single message ----------------------------------------------------
    def acquire_raw(self, raw):
        """Normalise one raw message and feed it to acquire_message (or preview it in
        dry-run). Returns the AcquiredMessage, or None (dry-run / no provider)."""
        msg = normalize_message(raw)
        provider = self.provider_for_chat(msg["chat_id"])
        if provider is None:
            logger.info("wayond listener: no provider for chat_id=%s — ignored",
                        msg["chat_id"])
            return None
        if self.dry_run:
            outcome = classify(msg["text"], is_edit=bool(msg["edit_date"]),
                               media=bool(msg["media"]))
            logger.info("wayond listener DRY-RUN: provider=%s message_id=%s would→%s",
                        provider.slug, msg["message_id"], outcome)
            return None
        return acquire_message(provider, msg)

    def _on_event(self, message):
        return self.acquire_raw(message)

    # --- watermark catch-up ------------------------------------------------
    def catch_up(self, client, provider, *, limit: int = 200):
        """Read messages NEWER than the provider watermark and feed each. Read-only —
        uses ``iter_messages(min_id=…)`` so already-seen messages are skipped."""
        try:
            min_id = int(provider.watermark_last_message_id or 0)
        except (TypeError, ValueError):
            min_id = 0
        entity = _as_int(provider.telegram_chat_id)
        n = 0
        for raw in self._floodwait(
                client.iter_messages, entity, min_id=min_id, limit=limit, reverse=True):
            self.acquire_raw(raw)
            n += 1
        logger.info("wayond listener catch-up: provider=%s from min_id=%s processed=%s",
                    provider.slug, min_id, n)
        return n

    def catch_up_all(self, client):
        total = 0
        for cid in self.subscribed_chat_ids():
            provider = self.provider_for_chat(cid)
            if provider is not None:
                total += self.catch_up(client, provider)
        return total

    # --- flood-wait / rate-limit ------------------------------------------
    def _floodwait(self, fn, *args, **kwargs):
        """Call ``fn`` and honour Telethon FloodWaitError by sleeping the requested
        seconds (detected by class name to avoid importing Telethon)."""
        while True:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ != "FloodWaitError":
                    raise
                secs = int(getattr(exc, "seconds", 0) or 0)
                logger.warning("wayond listener flood-wait: sleeping %ss", secs)
                time.sleep(secs)

    # --- health ------------------------------------------------------------
    def _heartbeat(self, state, **kw):
        logger.info("wayond listener heartbeat: state=%s %s", state,
                    " ".join(f"{k}={v}" for k, v in kw.items()))

    # --- fixture / dry replay ---------------------------------------------
    def replay(self, messages):
        """Feed a list of raw messages (fixture mode). Returns count processed."""
        n = 0
        for raw in messages:
            self.acquire_raw(raw)
            n += 1
        self._heartbeat("replay_done", processed=n, dry_run=self.dry_run)
        return n

    # --- live run (client + events injected by the command) ---------------
    def run(self, client, events):
        """Attach read-only NewMessage + MessageEdited handlers, catch up from the
        watermark, then block on the client. ``client``/``events`` are built (lazily)
        by the management command; here they are just used — never Telethon-imported."""
        if not client.is_user_authorized():
            raise ListenerNotAuthorized(
                "session not authorised — provision + age the GFX account first")
        chats = [_as_int(c) for c in self.subscribed_chat_ids()]

        async def _handler(event):
            import asyncio
            # READ-ONLY: offload the sync ORM work to a thread; never send anything.
            await asyncio.to_thread(self._on_event, getattr(event, "message", event))

        client.add_event_handler(_handler, events.NewMessage(chats=chats))
        client.add_event_handler(_handler, events.MessageEdited(chats=chats))
        processed = self.catch_up_all(client)
        self._heartbeat("listening", chats=len(chats), caught_up=processed)
        client.run_until_disconnected()

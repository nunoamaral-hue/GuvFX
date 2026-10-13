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
import os
import time

from django.utils import timezone

from ..acquisition import acquire_message
from ..certification import classify
from ..models import AcquiredMessage, SignalProvider
from .normalize import normalize_message

logger = logging.getLogger(__name__)

_INACTIVE = (SignalProvider.Status.INACTIVE, SignalProvider.Status.RETIRED)

# CATCH-UP COMPLETENESS (T1): bounded, resumable pagination. A pass fetches pages of
# ``_CATCHUP_PAGE_SIZE`` NEWER-than-watermark messages in chronological order until the
# backlog is exhausted OR the safety ceiling ``_CATCHUP_MAX_PAGES`` is reached. Hitting the
# ceiling yields an explicit INCOMPLETE status — it is NEVER reported as a complete catch-up.
def _positive_env_int(name, default):
    """A POSITIVE int from env, else the default. A 0/negative/invalid value falls back to
    the default — a zero page size would otherwise fetch an empty page and be mis-reported
    as a COMPLETE no-op (fail-open); this fails closed to a safe default."""
    try:
        v = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


_CATCHUP_PAGE_SIZE = _positive_env_int("WAYOND_CATCHUP_PAGE_SIZE", 200)
_CATCHUP_MAX_PAGES = _positive_env_int("WAYOND_CATCHUP_MAX_PAGES", 500)


class CatchupStatus:
    COMPLETE = "COMPLETE"                 # backlog fully drained within the ceiling
    INCOMPLETE_CEILING = "INCOMPLETE_CEILING"  # safety ceiling hit, more may remain
    INCOMPLETE_ERROR = "INCOMPLETE_ERROR"      # a page/processing error stopped the pass


class CatchupResult:
    """Secret-free per-provider catch-up summary. ``status`` distinguishes a genuinely
    complete pass from an incomplete one; the counters separate *retrieved* from
    *durably acquired* from *stale* — a COMPLETE catch-up may legitimately contain STALE
    messages, so completeness never implies every message became a tradeable signal."""
    __slots__ = ("provider", "chat_id", "status", "from_watermark", "last_processed_id",
                 "pages", "retrieved", "newly_acquired", "duplicates", "stale",
                 "tradeable_approvals", "error")

    def __init__(self, provider, from_watermark):
        self.provider = provider.slug
        self.chat_id = provider.telegram_chat_id
        self.status = CatchupStatus.COMPLETE
        self.from_watermark = from_watermark
        self.last_processed_id = from_watermark
        self.pages = 0
        self.retrieved = 0
        self.newly_acquired = 0
        self.duplicates = 0
        self.stale = 0
        self.tradeable_approvals = 0
        self.error = ""

    @property
    def complete(self) -> bool:
        return self.status == CatchupStatus.COMPLETE

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


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

    # --- watermark catch-up (bounded, resumable, observable) ---------------
    def _fetch_page(self, client, entity, min_id, limit):
        """Fetch one page as a fully-materialised list, FloodWait-guarded around BOTH
        construction AND consumption of the iterator (Telethon's ``iter_messages`` is lazy
        — a FloodWaitError can surface while consuming, not only when constructing). A
        flood-wait retries the whole page fetch; no message has been processed yet, so the
        retry is clean (idempotent)."""
        def _materialise():
            return list(client.iter_messages(entity, min_id=min_id, limit=limit, reverse=True))
        return self._floodwait(_materialise)

    @staticmethod
    def _raw_id(raw):
        """The integer message id of a raw/normalised message, or None."""
        rid = getattr(raw, "id", None)
        if rid is None and isinstance(raw, dict):
            rid = raw.get("id", raw.get("message_id"))
        return _as_int(rid) if isinstance(_as_int(rid), int) else None

    def catch_up(self, client, provider, *, page_size=None, max_pages=None):
        """Drain the provider's backlog (messages NEWER than its watermark) in
        chronological order, PAGE BY PAGE, until exhausted or the safety ceiling is hit.

        Completeness is established from the retrieval contract — not from the watermark
        scalar. A LOCAL ``floor`` (the highest id this pass has durably processed) drives
        pagination; it is NEVER re-read from ``provider.watermark_last_message_id`` mid-pass,
        so a concurrent live NewMessage event that advances the shared watermark can never
        make catch-up skip an unprocessed historical message (T1). Each page fetch is
        FloodWait-guarded (flood-wait can occur while consuming the iterator, not only when
        constructing it). Processing is idempotent (``acquire_message`` dedups); the 600 s
        staleness policy is unchanged (an aged message is STALE, never a fresh approval).
        Returns a ``CatchupResult`` whose ``status`` is COMPLETE only when the backlog was
        fully drained — a ceiling/error pass is reported INCOMPLETE, never as success."""
        # Clamp to positive bounds: a misconfigured 0/negative must NOT make page_size fall
        # to an empty fetch that yields a false COMPLETE no-op (it would process zero of the
        # backlog yet report success). 0/invalid → the configured default.
        page_size = page_size if (isinstance(page_size, int) and page_size > 0) else _CATCHUP_PAGE_SIZE
        max_pages = max_pages if (isinstance(max_pages, int) and max_pages > 0) else _CATCHUP_MAX_PAGES
        try:
            floor = int(provider.watermark_last_message_id or 0)
        except (TypeError, ValueError):
            floor = 0
        entity = _as_int(provider.telegram_chat_id)
        run_start = timezone.now()
        res = CatchupResult(provider, floor)
        seen = set()  # message ids processed THIS pass — intra-pass re-delivery = duplicate
        while True:
            try:
                page = self._fetch_page(client, entity, floor, page_size)
            except Exception as exc:  # noqa: BLE001 — a page fetch failed; do NOT claim complete
                res.status, res.error = CatchupStatus.INCOMPLETE_ERROR, f"fetch:{type(exc).__name__}"
                break
            if not page:
                break  # backlog exhausted → COMPLETE (fetch-first: this confirming fetch proves it)
            if res.pages >= max_pages:
                # Already processed max_pages and a FURTHER non-empty page exists → genuinely
                # more remains. Report INCOMPLETE without processing beyond the ceiling. (A
                # backlog of EXACTLY page_size*max_pages instead hits the empty confirming
                # fetch above and is correctly COMPLETE.)
                res.status = CatchupStatus.INCOMPLETE_CEILING
                break
            res.pages += 1
            try:
                floor = self._process_page(page, floor, run_start, seen, res)
            except Exception as exc:  # noqa: BLE001 — processing failed mid-page; fail closed
                res.status, res.error = CatchupStatus.INCOMPLETE_ERROR, f"process:{type(exc).__name__}"
                break
            if len(page) < page_size:
                break  # last (partial) page → exhausted
        logger.info("wayond listener catch-up: provider=%s status=%s from=%s last=%s pages=%s "
                    "retrieved=%s acquired=%s dup=%s stale=%s approvals=%s%s",
                    res.provider, res.status, res.from_watermark, res.last_processed_id, res.pages,
                    res.retrieved, res.newly_acquired, res.duplicates, res.stale,
                    res.tradeable_approvals, (f" error={res.error}" if res.error else ""))
        return res

    def _process_page(self, page, floor, run_start, seen, res):
        """Process one page in chronological order; advance and return the LOCAL floor by
        the highest id durably processed, updating ``res.last_processed_id`` per message so a
        mid-page error still reports the true durable checkpoint. A re-delivery (same id
        earlier this pass, or a row pre-existing before this pass) is counted as a duplicate,
        never double-counted into stale/approvals."""
        for raw in page:
            acq = self.acquire_raw(raw)   # acquire_message commits per message (durable)
            res.retrieved += 1
            rid = self._raw_id(raw)
            dup = (rid is not None and rid in seen) or (
                acq is not None and acq.acquired_at is not None and acq.acquired_at < run_start)
            if acq is not None:
                if dup:
                    res.duplicates += 1
                else:
                    res.newly_acquired += 1
                    if acq.outcome == AcquiredMessage.Outcome.STALE:
                        res.stale += 1
                    if acq.outcome == AcquiredMessage.Outcome.INTAKEN and acq.approval_id:
                        res.tradeable_approvals += 1
            if rid is not None:
                seen.add(rid)
                if rid > floor:
                    floor = rid
                    res.last_processed_id = floor   # durable checkpoint, kept current per message
        return floor

    def catch_up_all(self, client):
        """Catch up EVERY subscribed provider independently. One provider's incomplete/
        failed pass is isolated and recorded — it never aborts the others. Returns the list
        of per-provider CatchupResult."""
        results = []
        for cid in self.subscribed_chat_ids():
            provider = self.provider_for_chat(cid)
            if provider is None:
                continue
            try:
                results.append(self.catch_up(client, provider))
            except Exception:  # noqa: BLE001 — isolate a provider-level failure, keep going
                logger.exception("wayond listener catch-up: provider chat=%s crashed", cid)
                res = CatchupResult(provider, provider.watermark_last_message_id or "")
                res.status, res.error = CatchupStatus.INCOMPLETE_ERROR, "provider_crash"
                results.append(res)
        return results

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
                try:
                    secs = int(getattr(exc, "seconds", 0) or 0)
                except (TypeError, ValueError):
                    secs = 60  # malformed seconds → a safe default, never crash
                secs = max(secs, 0)
                logger.warning("wayond listener flood-wait: sleeping %ss", secs)
                time.sleep(secs)

    # --- health ------------------------------------------------------------
    def _heartbeat(self, state, **kw):
        logger.info("wayond listener heartbeat: state=%s %s", state,
                    " ".join(f"{k}={v}" for k, v in kw.items()))

    def write_health(self, path):
        """Write a liveness timestamp to ``path`` (atomic) for the container
        healthcheck. Local file only — never Telegram, never a download."""
        try:
            tmp = f"{path}.tmp"
            with open(tmp, "w") as fh:
                fh.write(str(time.time()))
            os.replace(tmp, path)
        except OSError as exc:  # never crash the listener over a health write
            logger.warning("wayond listener: health write failed (%s): %s", path, exc)

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
        # Catch-up runs to completion BEFORE run_until_disconnected dispatches any live
        # event — so each provider's watermark is a contiguous checkpoint at hand-off, and
        # no live event races the historical pages (invariant relied on by the local-floor
        # pagination above).
        results = self.catch_up_all(client)
        incomplete = [r.provider for r in results if not r.complete]
        self._heartbeat("listening", chats=len(chats), providers=len(results),
                        caught_up=sum(r.newly_acquired for r in results),
                        incomplete=(",".join(incomplete) or "none"))
        if incomplete:
            # Liveness != acquisition completeness: surface an INCOMPLETE catch-up loudly;
            # an operator must re-run/resolve it (the container healthcheck will still be green).
            logger.warning("wayond listener: catch-up INCOMPLETE for providers=%s — "
                           "historical backlog may remain; re-run required", incomplete)
        client.run_until_disconnected()

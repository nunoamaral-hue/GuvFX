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
# ``_CATCHUP_PAGE_SIZE`` messages NEWER than the durable CHECKPOINT (not the contaminable
# watermark), in chronological order, until the backlog is exhausted OR a safety bound is hit
# (page ceiling, wall-clock budget, or flood-wait budget). Any bound hit yields an explicit
# INCOMPLETE status — it is NEVER reported as a complete catch-up.
def _bounded_env_int(name, default, *, minimum=1, maximum):
    """A bounded int from env, else the default. A value below ``minimum`` (incl. 0/negative/
    invalid — a zero page size would otherwise fetch an empty page and be mis-reported as a
    COMPLETE no-op) falls back to the default; a value above ``maximum`` is clamped down
    (an excessive page size / page count must not exhaust memory or run unbounded)."""
    try:
        v = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default
    if v < minimum:
        return default
    return min(v, maximum)


_CATCHUP_PAGE_SIZE = _bounded_env_int("WAYOND_CATCHUP_PAGE_SIZE", 200, maximum=1000)
_CATCHUP_MAX_PAGES = _bounded_env_int("WAYOND_CATCHUP_MAX_PAGES", 500, maximum=10000)
_CATCHUP_TIME_BUDGET_S = _bounded_env_int("WAYOND_CATCHUP_TIME_BUDGET_SECONDS", 300, maximum=86400)
_CATCHUP_FLOODWAIT_MAX_WAITS = _bounded_env_int("WAYOND_CATCHUP_FLOODWAIT_MAX_WAITS", 3, maximum=100)
# PERMITTED single wait: a server-requested FloodWait LONGER than this is REJECTED (fail closed
# → INCOMPLETE), never shortened-and-retried early (rate-limit compliance, T1 R2 MEDIUM).
_CATCHUP_FLOODWAIT_MAX_SINGLE_S = _bounded_env_int(
    "WAYOND_CATCHUP_FLOODWAIT_MAX_SINGLE_SECONDS", 300, maximum=3600)


class CatchupStatus:
    COMPLETE = "COMPLETE"                       # backlog fully drained within all bounds
    INCOMPLETE_CEILING = "INCOMPLETE_CEILING"   # page ceiling hit, more remains
    INCOMPLETE_ERROR = "INCOMPLETE_ERROR"       # fetch/process error, flood-wait or time budget


class CatchupResult:
    """Secret-free per-provider catch-up summary. ``status`` is the RETRIEVAL outcome
    (COMPLETE = the Telegram iterator was exhausted within all bounds; INCOMPLETE_* = a bound
    was hit). The counters separate *retrieved* (fetched) from *ignored* (no matching provider)
    from *newly_acquired* (durable rows) from *duplicates*/*stale*/*non_genuine*/
    *tradeable_approvals*.

    Two DISTINCT positions (T1 R2): ``retrieval_position`` is the highest message id retrieved
    this pass; ``checkpoint`` is the highest SAFELY-ACCOUNTED resume boundary — it never
    advances past the earliest UNACCOUNTED retrieval (a non-genuine DROP / chat-refusal, an
    ignored no-provider message, or a message whose durable disposition is unresolved), held
    at ``unaccounted_floor``. So an exhausted retrieval (COMPLETE) does NOT by itself mean
    complete durable acquisition — use ``durably_complete`` for that."""
    __slots__ = ("provider", "chat_id", "status", "from_checkpoint", "checkpoint",
                 "last_processed_id", "retrieval_position", "unaccounted_floor", "pages",
                 "retrieved", "ignored", "newly_acquired", "duplicates", "stale",
                 "non_genuine", "tradeable_approvals", "error")

    def __init__(self, provider, from_checkpoint):
        self.provider = provider.slug
        self.chat_id = provider.telegram_chat_id
        self.status = CatchupStatus.COMPLETE
        self.from_checkpoint = from_checkpoint
        self.checkpoint = from_checkpoint
        self.last_processed_id = from_checkpoint
        self.retrieval_position = from_checkpoint  # highest id RETRIEVED this pass
        self.unaccounted_floor = 0                 # earliest UNACCOUNTED retrieval id (0 = none)
        self.pages = 0
        self.retrieved = 0
        self.ignored = 0
        self.newly_acquired = 0
        self.duplicates = 0
        self.stale = 0
        self.non_genuine = 0
        self.tradeable_approvals = 0
        self.error = ""

    @property
    def complete(self) -> bool:
        """Retrieval exhausted within bounds (the Telegram iterator reached the end)."""
        return self.status == CatchupStatus.COMPLETE

    @property
    def durably_complete(self) -> bool:
        """Retrieval exhausted AND every retrieved message is safely accounted — i.e. the
        durable checkpoint reached the retrieval position with no unaccounted boundary held
        back. This is the only honest 'nothing left to safely acquire' signal (T1 R2)."""
        return self.complete and self.unaccounted_floor == 0

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
        # Explicit, stable resolution. SignalProvider.Meta already orders by slug, so .first()
        # was slug-deterministic; we make that ordering explicit here (not reliant on Meta) so a
        # shared chat id always resolves to the SAME provider. catch_up_all additionally
        # de-dupes + warns so a shared chat is not caught up twice (T1 R1 adversarial, LOW).
        # NOTE: this is NOT status-aware (a per-chat uniqueness / status-aware owner rule is a
        # tracked follow-up — KNOWN_ISSUES); in production every provider has a distinct chat.
        return SignalProvider.objects.filter(
            telegram_chat_id=str(chat_id)).order_by("slug").first()

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
    def _fetch_page(self, client, entity, min_id, limit, deadline=None):
        """Fetch one page as a fully-materialised list, FloodWait-guarded around BOTH
        construction AND consumption of the iterator (Telethon's ``iter_messages`` is lazy
        — a FloodWaitError can surface while consuming, not only when constructing). A
        flood-wait retries the whole page fetch; no message has been processed yet, so the
        retry is clean (idempotent). ``deadline`` (monotonic) bounds the flood-wait sleeps so
        a single page can never block the catch-up past the wall-clock budget."""
        def _materialise():
            return list(client.iter_messages(entity, min_id=min_id, limit=limit, reverse=True))
        return self._floodwait(_materialise, deadline=deadline)

    @staticmethod
    def _raw_id(raw):
        """The integer message id of a raw/normalised message, or None."""
        rid = getattr(raw, "id", None)
        if rid is None and isinstance(raw, dict):
            rid = raw.get("id", raw.get("message_id"))
        return _as_int(rid) if isinstance(_as_int(rid), int) else None

    def catch_up(self, client, provider, *, page_size=None, max_pages=None, time_budget_s=None):
        """Drain the provider's backlog PAGE BY PAGE until exhausted or a safety bound is hit.

        Resume floor = the durable ``catchup_checkpoint_message_id`` (NOT the contaminable
        watermark): because telethon.sync iter_messages dispatches live events during a
        synchronous catch-up, a live high-id message can advance the shared watermark, so
        only the checkpoint — advanced ONLY by this pass, monotonically, and ONLY past
        DURABLY-ACQUIRED messages — is a safe resume point (T1 R1 HIGH-1). The checkpoint is
        persisted after each page, so a crash resumes without skipping or regressing.

        A LOCAL pagination ``floor`` (highest id SEEN this pass) drives paging and is never
        re-read from the shared watermark. Each page fetch is FloodWait-guarded around both
        construction AND consumption, with a bounded wait budget. Bounds (page size/count,
        wall-clock, flood-wait) are finite; exceeding any yields an explicit INCOMPLETE
        status — never a false COMPLETE. Idempotent (acquire_message dedups); the staleness
        policy is unchanged (aged → STALE, never a fresh approval).

        T1 R2: the durable checkpoint is the highest SAFELY-ACCOUNTED boundary — it never
        advances past the EARLIEST unaccounted retrieval of the pass (``res.unaccounted_floor``:
        a non-genuine DROP/refusal, or an ignored no-provider message), so a later genuine
        message can NEVER mask an earlier unaccounted one. The checkpoint may still cross
        legitimate gaps in Telegram numbering. An exhausted retrieval with an unaccounted
        boundary held back is COMPLETE but NOT ``durably_complete``."""
        page_size = page_size if (isinstance(page_size, int) and page_size > 0) else _CATCHUP_PAGE_SIZE
        max_pages = max_pages if (isinstance(max_pages, int) and max_pages > 0) else _CATCHUP_MAX_PAGES
        time_budget_s = time_budget_s if (isinstance(time_budget_s, int) and time_budget_s > 0) else _CATCHUP_TIME_BUDGET_S
        try:
            checkpoint = int(provider.catchup_checkpoint_message_id or 0)
        except (TypeError, ValueError):
            checkpoint = 0
        floor = checkpoint
        entity = _as_int(provider.telegram_chat_id)
        deadline = time.monotonic() + time_budget_s
        res = CatchupResult(provider, checkpoint)
        seen = set()  # message ids seen THIS pass — intra-pass re-delivery = duplicate
        while True:
            if time.monotonic() > deadline:
                res.status, res.error = CatchupStatus.INCOMPLETE_ERROR, "time_budget_exceeded"
                break
            try:
                # deadline bounds flood-wait sleeps to the wall-clock budget (no overshoot).
                page = self._fetch_page(client, entity, floor, page_size, deadline=deadline)
            except Exception as exc:  # noqa: BLE001 — fetch/flood-wait budget failed; NOT complete
                res.status, res.error = CatchupStatus.INCOMPLETE_ERROR, f"fetch:{type(exc).__name__}"
                break
            if not page:
                break  # backlog exhausted → COMPLETE (the empty confirming fetch proves it)
            if res.pages >= max_pages:
                res.status = CatchupStatus.INCOMPLETE_CEILING
                break
            res.pages += 1
            try:
                floor, checkpoint = self._process_page(page, floor, checkpoint, seen, res)
            except Exception as exc:  # noqa: BLE001 — processing failed mid-page; fail closed
                res.status, res.error = CatchupStatus.INCOMPLETE_ERROR, f"process:{type(exc).__name__}"
                break
            # Persist the durable checkpoint after each page (monotonic). A crash before this
            # resumes from the previous page boundary → idempotent re-process, never a skip.
            if checkpoint > res.checkpoint:
                res.checkpoint = res.last_processed_id = checkpoint
                provider.catchup_checkpoint_message_id = str(checkpoint)
                provider.save(update_fields=["catchup_checkpoint_message_id"])
            if len(page) < page_size:
                break  # last (partial) page → exhausted
        logger.info("wayond listener catch-up: provider=%s status=%s durably_complete=%s from=%s "
                    "checkpoint=%s retrieval_pos=%s unaccounted_floor=%s pages=%s retrieved=%s "
                    "acquired=%s ignored=%s non_genuine=%s dup=%s stale=%s approvals=%s%s",
                    res.provider, res.status, res.durably_complete, res.from_checkpoint,
                    res.checkpoint, res.retrieval_position, res.unaccounted_floor, res.pages,
                    res.retrieved, res.newly_acquired, res.ignored, res.non_genuine,
                    res.duplicates, res.stale, res.tradeable_approvals,
                    (f" error={res.error}" if res.error else ""))
        return res

    def _process_page(self, page, floor, checkpoint, seen, res):
        """Process one page chronologically. Returns (pagination_floor, durable_checkpoint).

        The pagination ``floor`` advances past EVERY retrieved id (so paging always
        progresses). A retrieved message is ACCOUNTED iff it became a GENUINE durable
        acquisition (``acq is not None`` AND ``not acq.non_genuine`` — INTAKEN / STALE /
        armed-QUARANTINED); it is UNACCOUNTED iff ignored (no provider) or non-genuine (a
        paused/onboarding DROP or a chat refusal), because such a message may still need a
        genuine acquisition later (e.g. once the provider is re-armed — the ``non_genuine``
        partition contract deliberately allows that).

        The durable ``checkpoint`` (T1 R2) is the highest ACCOUNTED id that is strictly BELOW
        the earliest unaccounted retrieval of the pass (``res.unaccounted_floor``). Because
        the page is processed in ascending id order, once an unaccounted id U is seen every
        later id is > U and is therefore barred from advancing the checkpoint — a later
        genuine message can NEVER mask an earlier unaccounted one (the T1 R2 HIGH fix). The
        checkpoint still crosses legitimate numbering gaps. Re-delivery (same id this pass, or
        a row pre-existing this acquire call) is a duplicate, not double-tallied."""
        for raw in page:
            t_before = timezone.now()     # distinguishes a row WE create now from one a
                                          # concurrent live handler already created (see dup)
            acq = self.acquire_raw(raw)   # acquire_message commits per durable message
            res.retrieved += 1
            rid = self._raw_id(raw)
            accounted = acq is not None and not acq.non_genuine
            if acq is None:
                res.ignored += 1          # retrieved but not durably acquired (no provider)
            else:
                if acq.non_genuine:
                    res.non_genuine += 1  # DROPPED_NOT_ARMED / chat-refusal — not safely accounted
                # Duplicate if seen already THIS pass, OR the row PRE-EXISTED this acquire call
                # — a prior pass OR a concurrent live NewMessage/MessageEdited that acquired the
                # same id during this telethon.sync catch-up (acquire_message returns that
                # existing row on dedup). acquired_at < t_before ⇒ not created by this call.
                dup = (rid is not None and rid in seen) or (
                    acq.acquired_at is not None and acq.acquired_at < t_before)
                if dup:
                    res.duplicates += 1
                else:
                    res.newly_acquired += 1
                    if acq.outcome == AcquiredMessage.Outcome.STALE:
                        res.stale += 1
                    if acq.outcome == AcquiredMessage.Outcome.INTAKEN and acq.approval_id:
                        res.tradeable_approvals += 1
            if rid is not None:
                # Track the EARLIEST unaccounted retrieval (ignored OR non-genuine). The durable
                # checkpoint must never reach or pass it (T1 R2 §3C/D/E).
                if not accounted and (res.unaccounted_floor == 0 or rid < res.unaccounted_floor):
                    res.unaccounted_floor = rid
                # Advance the checkpoint ONLY past an accounted id that is strictly below the
                # earliest unaccounted boundary of the pass.
                if (accounted and rid > checkpoint
                        and (res.unaccounted_floor == 0 or rid < res.unaccounted_floor)):
                    checkpoint = rid
                seen.add(rid)
                if rid > floor:
                    floor = rid               # pagination floor advances past everything retrieved
                if rid > res.retrieval_position:
                    res.retrieval_position = rid   # highest id retrieved (vs the accounted checkpoint)
        return floor, checkpoint

    def catch_up_all(self, client):
        """Catch up EVERY subscribed provider independently. One provider's incomplete/
        failed pass is isolated and recorded — it never aborts the others. Returns the list
        of per-provider CatchupResult."""
        results = []
        done_chats = set()
        for cid in self.subscribed_chat_ids():
            if cid in done_chats:
                # Two active providers share a chat id → ambiguous attribution. provider_for_chat
                # resolves deterministically (lowest id); warn so the operator fixes it, and do
                # NOT catch the chat up twice (which would starve the other provider).
                logger.warning("wayond listener catch-up: multiple active providers share "
                               "chat_id=%s — caught up once (lowest-id); resolve the duplicate", cid)
                continue
            done_chats.add(cid)
            provider = self.provider_for_chat(cid)
            if provider is None:
                continue
            try:
                results.append(self.catch_up(client, provider))
            except Exception:  # noqa: BLE001 — isolate a provider-level failure, keep going
                logger.exception("wayond listener catch-up: provider chat=%s crashed", cid)
                res = CatchupResult(provider, provider.catchup_checkpoint_message_id or "")
                res.status, res.error = CatchupStatus.INCOMPLETE_ERROR, "provider_crash"
                results.append(res)
        return results

    # --- flood-wait / rate-limit (BOUNDED) --------------------------------
    def _floodwait(self, fn, *args, max_waits=None, max_single_s=None, deadline=None, **kwargs):
        """Call ``fn`` and honour Telethon FloodWaitError, but BOUNDED and RATE-LIMIT-COMPLIANT:
        at most ``max_waits`` flood-waits, and we always sleep the FULL server-requested wait —
        never a shortened one. If the server demands MORE than ``max_single_s`` (the permitted
        single wait) or more than the remaining wall-clock ``deadline``, we do NOT shorten it
        and retry early (that would hammer Telegram before its required period elapsed); instead
        the FloodWaitError is re-raised so the caller records an explicit INCOMPLETE (T1 R2
        MEDIUM). Exceeding the retry budget also re-raises. Total recovery time is bounded by
        ``max_waits * max_single_s`` and by the deadline."""
        max_waits = max_waits if (isinstance(max_waits, int) and max_waits > 0) else _CATCHUP_FLOODWAIT_MAX_WAITS
        max_single_s = max_single_s if (isinstance(max_single_s, int) and max_single_s > 0) else _CATCHUP_FLOODWAIT_MAX_SINGLE_S
        waits = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if type(exc).__name__ != "FloodWaitError":
                    raise
                waits += 1
                if waits > max_waits:
                    raise  # bounded — do not retry indefinitely; caller → INCOMPLETE_ERROR
                try:
                    requested = int(getattr(exc, "seconds", 0) or 0)
                except (TypeError, ValueError):
                    requested = 60  # malformed seconds → a safe default, never crash
                requested = max(requested, 0)  # the FULL server-requested wait (never shortened)
                if requested > max_single_s:
                    raise  # oversized wait → fail closed; NEVER shorten-and-retry (rate-limit)
                if deadline is not None and (time.monotonic() + requested) > deadline:
                    raise  # honouring it would overshoot the wall-clock budget → fail closed now
                logger.warning("wayond listener flood-wait: sleeping %ss (wait %s/%s)",
                               requested, waits, max_waits)
                time.sleep(requested)

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
        # COMPLETE retrieval but a held-back accounted boundary (unaccounted retrievals below the
        # checkpoint — e.g. a paused provider's DROPs). NOT loss: re-evaluated on the next pass
        # once accounted (e.g. after arm). Surfaced for observability (T1 R2 §3J).
        held = [f"{r.provider}@{r.unaccounted_floor}"
                for r in results if r.complete and not r.durably_complete]
        self._heartbeat("listening", chats=len(chats), providers=len(results),
                        caught_up=sum(r.newly_acquired for r in results),
                        incomplete=(",".join(incomplete) or "none"),
                        unaccounted=(",".join(held) or "none"))
        if incomplete:
            # Liveness != acquisition completeness: surface an INCOMPLETE catch-up loudly;
            # an operator must re-run/resolve it (the container healthcheck will still be green).
            logger.warning("wayond listener: catch-up INCOMPLETE for providers=%s — "
                           "historical backlog may remain; re-run required", incomplete)
        if held:
            logger.info("wayond listener: accounted boundary held below an unaccounted retrieval "
                        "for %s — those ids are re-evaluated next pass (e.g. after arm); not loss", held)
        client.run_until_disconnected()

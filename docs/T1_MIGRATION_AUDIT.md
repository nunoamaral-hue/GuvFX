# T1 Telegram Source-Migration — Read-Only Safety Audit (2026-10-07)

**Status:** READ-ONLY audit for the Sponsor. **No change made.** The migration remains **STOPPED** (correctly).
Method: static code audit (multi-agent, adversarially verified) + a read-only prod-DB fact read. No signal
manufactured, no history replayed, no trading config altered.

Evidence base: `backend/signal_intake/{models,acquisition,services}.py`, `listener/adapter.py`,
`listener/normalize.py`, `management/commands/onboard_provider.py`, `execution/models.py`,
`intelligence/ti_signals_source.py`, migrations; plus a read-only `SignalProvider`/`AcquiredMessage` query.

---

## 1. Existing immutable T1 chat id
- **`-1004480146594`** — `SignalProvider` id=2 ("TI Signals", ARMED, `watermark_last_message_id=889`,
  `acquisition_window_seconds=600`, parser `ti_signals_v1`). (Wayond is id=1, chat `-1003842321905`.)
- Stored in `SignalProvider.telegram_chat_id` (CharField; `models.py:181`). It is the **only** field the listener
  uses to bind a message to a provider (`adapter.py:44` `filter(telegram_chat_id=str(chat_id))`); `chat_title` is
  descriptive and **never** an identity key. Read-only way to confirm live: Django admin or
  `manage.py shell` over `SignalProvider`.

## 2. Proposed new immutable chat id
- **UNKNOWN / not inventable from the repo.** The code has no dialog-list/`get_entity` resolver; `onboard_provider`
  takes an **operator-supplied** value and makes no Telegram call (`onboard_provider.py:45,50`).
- It **must be resolved read-only from the GuvFX ingestion session** (its dialog list / `get_entity`) and
  **never** from the display name — several channels share "TI Signals". **Sponsor must supply/confirm the
  numeric id.** I will not guess it.

## 3. Exact deduplication-key structure (TWO chat-blind layers + one chat-aware)
1. **Ledger — `AcquiredMessage`:** `uniq_provider_message = (provider, message_id)` (`models.py:241-243`;
   migration `0004:90`). Lookup is the FIRST thing `acquire_message()` does: `filter(provider=provider,
   message_id=mid)` (`acquisition.py:281`). **`message_id` is a string** (`acquisition.py:275`; CharField). A
   `chat_id` column EXISTS on the row and is **fully populated** (live: 997/997 rows, 0 null/blank, 1:1 with
   provider) **but is NOT in the key.**
2. **Approval — `PendingSignalApproval`:** `uniq_source_message = (source, message_id)` (`models.py:87-89`).
   In the **live listener path `source = provider.slug`** (`services.py:49-50` overrides the `WAYOND_TELEGRAM`
   constant whenever a provider is passed; the constant applies only to the legacy `provider=None` file-intake
   route). `slug` is unique, so this key is effectively **per-provider-slug**, not global. `PendingSignalApproval`
   has **no `chat_id` field at all** → chat-blind. Lookup `services.py:53`.
3. **Plan — `SignalExecutionPlan`:** `uniq_plan_source_chat_message_account = (source, chat_id, message_id,
   account)` (`execution/models.py:918-921`) — **already chat-aware**, so the plan layer is NOT a collision point.

## 4. Collision scenario (silent DROP of a genuine new signal)
Telegram message ids are a **per-channel counter**, so a new channel restarts low (1..N) and its string ids overlap
the current channel's recorded ids (provider 2 holds 875 rows). Behaviour depends on migration shape:

- **MUTATE in place** (change provider 2's `telegram_chat_id`, same row/slug/pk): **COLLISION → silent DROP (MISS).**
  `acquire_message()` finds the OLD channel's `(provider, message_id)` row (`acquisition.py:281`) and short-circuits
  **before** `_classify` (`:282`). If the new body differs it is **misfiled as a cross-channel "amendment"** of the
  unrelated old message (`_record_amendment`, `:289-291`) and no approval is created. The approval layer
  `(slug, message_id)` would also collide (slug unchanged) — but the drop already happens one layer earlier.
  *(Secondary hazard: if the colliding old row had `approval=None`, the misfiled amendment can mint an approval that
  bypasses the acquisition-window staleness gate — a spurious-intake risk in the opposite direction.)*
- **FRESH provider row (new slug):** **collision-free at every layer** — different provider FK (ledger), different
  `slug`→`source` (approval), and the plan key is already chat-aware. *(This corrects an earlier claim that a fresh
  row still collided; verification showed `source=provider.slug` in the live path, so a new slug cannot match old
  approvals.)*

**Verdict:** numeric id overlap can only ever **DROP** a new signal (a MISS), **never REPLAY** one — and only on the
MUTATE shape. By construction a pre-existing `(provider, message_id)` is what *suppresses* processing, so it can
never re-execute.

## 5. Historical replay risk
**A source switch cannot replay the new channel's backlog as fresh orders.** Protection is the **staleness windows +
arming + source-enablement**, NOT the watermark:
- Acquisition staleness: a message older than `acquisition_window_seconds` (600s) → **STALE** ledger row, no
  approval, no order (`acquisition.py`). STALE/non-INTAKEN never fires `signal_acquired`, so the auto-router never
  sees it.
- Plan staleness: even a fresh-enough message faces a tighter **120s** wall (`SIGNAL_MAX_AGE_SECONDS`) → VOIDED, no
  legs, no job.
- Source-enablement: a brand-new `source` has no `SignalSourceConfig` row and auto-exec is **default OFF** → an
  independent fail-closed gate.
- **Watermark must be RESET on a MUTATE switch:** it is per-provider-row and currently 889; if left, catch-up uses
  `min_id=889` and **skips the entire new channel** (which restarts at id 1). Resetting it is required *and* safe
  (the staleness windows make the resulting catch-up replay-safe).

## 6. Safest migration options (ranked) + recommendation
The hard constraint is the Sponsor's: a **pure source switch** that does **not** alter trading config
(strategy/accounts/magic/sizing/thresholds/rules). The routing key is `source = provider.slug`.

1. **RECOMMENDED — mutate `telegram_chat_id` in place (keep the slug) + make BOTH dedup layers chat-aware + reset
   watermark.** Only this both *strengthens* dedup and preserves routing (slug/source unchanged ⇒ assignments,
   `SignalSourceConfig`, the 0.40 sizing cap, breakeven/TP-protection source filters all untouched).
   **Critical correction:** the originally-identified "add `chat_id` to `AcquiredMessage` only" is **INSUFFICIENT** —
   under mutate-in-place the **approval** key `(slug, message_id)` *also* collides (slug unchanged). Both layers must
   become chat-aware.
2. **REJECTED — fresh provider row with a NEW slug.** Collision-free and needs no dedup schema change, **but it
   changes `source`** (the routing key) ⇒ would require re-pointing `StrategyAssignment.signal_source`,
   `SignalSourceConfig`, sizing, and source filters = **altering trading config** (out of scope / forbidden). Reusing
   the old slug is impossible (slug is unique; and a reused `source` string reintroduces the approval-layer collision).
3. **REJECTED — mutate in place WITHOUT strengthening dedup** = the currently-blocked path = the collision itself.

## 7. Schema migration vs code change (for the recommended option)
**Both are required; it is AMBER** (mutates unique constraints on an append-only ledger + shared structure) ⇒ needs a
documented decision/ADR before merge — not an in-passing edit.
- **`AcquiredMessage`:** `RemoveConstraint uniq_provider_message` + `AddConstraint (provider, chat_id, message_id)`.
  **No backfill** — `chat_id` is already populated on every row (verified 997/997), and each row's own recorded
  `chat_id` is correct. **Ordering rule:** any touch must use the row's *own* recorded `chat_id`, never
  `provider.telegram_chat_id`, and must happen **before** the chat_id is switched.
- **`PendingSignalApproval`:** has **no `chat_id` field** → must **add** `chat_id`, **persist** it on intake
  (`services.intake_parsed`), **backfill** existing rows (from their linked ledger row's chat_id), change
  `uniq_source_message` → `(source, chat_id, message_id)`, and update the `services.py:53` lookup.
- **Code:** `acquire_message()` dedup lookup (`acquisition.py:281`, `:315`), `intake_parsed` lookup + chat_id
  persistence, and an explicit watermark reset/handling on switch (no reset path exists today).

## 8. Isolation (the switch touches only the TI source)
- `telegram_chat_id` couples to exactly two things — the shared listener's subscription set and inbound
  message→provider dispatch (`adapter.py:44-53`) — and nothing else. It is **not** a dedup key, routing key, sizing
  key or magic key.
- Account routing is by `StrategyAssignment.signal_source = slug` (unchanged under the recommended option) → same
  accounts reached identically. **Magic** = `ASSIGNMENT_MAGIC_BASE + assignment.id` (immutable). **Sizing** =
  `SignalSourceConfig`/assignment leg-sizing keyed on `source`. None depend on `chat_id`.
- `signal_intake` never imports `execution`/`trading`/`strategies` (one-way boundary) → a source switch cannot reach
  a `TradingAccount` directly; account effects occur only later via the slug-keyed router (unchanged).
- **Accounts 25/35/36 and the wayond provider are unaffected** — wayond is an independent provider row with its own
  subscription/parser/dedup namespace. (A `telegram_chat_id` change requires a listener restart to re-read
  subscriptions.)

---

## Bottom line for the Sponsor
- The migration is **correctly blocked.** The real fix is **larger** than first thought: a pure source switch
  (mutate chat_id, keep slug) requires making **both** the `AcquiredMessage` **and** `PendingSignalApproval` dedup
  keys chat-aware (the latter needs a new `chat_id` field + backfill), plus a watermark reset — all **AMBER / ADR**.
- **Replay is not the danger; a silent DROP of genuine new signals is.** No path replays backlog as orders (staleness
  + arming + source-enablement are all fail-closed).
- **Two Sponsor decisions needed to proceed:** (a) supply/confirm the **new immutable chat id** (session-resolved,
  not by name); (b) approve the **two-layer dedup-strengthening + watermark-reset** change (ADR) as the governed
  pure-source-switch. Until both, the source stays as-is. **This work is independent of Withdrawal V1.**

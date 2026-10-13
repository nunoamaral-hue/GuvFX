# T1 — Source-Aware Telegram Message Identity & Dedup

Packet: `T1_SOURCE_AWARE_IDENTITY_AND_DEDUP`. Baseline commit `387cf2c`. **DARK** — not deployed, no channel
switch, no listener restart, no watermark reset, no second Telethon client. Authority: Production NONE /
Telegram-switch NONE / Trading NONE.

## Problem (the structural defect)

Telegram `message_id` is **chat-scoped**, not global. Two channels can each emit `message_id=5`. The acquisition
ledger's canonical identity is `(provider, message_id)` (and the approval's is `(source, message_id)`), so after a
future channel switch a **new-channel** message collides with a **historical old-channel** row of the same
`message_id` under the same provider/source:

- **Dedup lookup** `AcquiredMessage.objects.filter(provider, message_id)` finds the *historical old-chat* row →
  the new-chat message is wrongly treated as an already-seen duplicate → **the signal is silently lost.**
- The approval dedup `(source, message_id)` fails the same way → no `PendingSignalApproval` is created.

The fix makes the canonical acquisition identity **`(provider, chat_id, message_id)`** (and the approval's
**`(source, chat_id, message_id)`**), so a same-`message_id` message in a different chat is correctly distinct.

## GATE A — Identity-dependency inventory (complete graph)

`chat_id` already EXISTS as a stored, non-key column on `AcquiredMessage`, `SignalUpdate`, `ProviderCommand`
(populated by acquisition). It is NOT part of any uniqueness constraint or lookup. `PendingSignalApproval` has no
`chat_id` field at all.

| # | Load-bearing path | Current key | Change |
|---|---|---|---|
| 1 | `models.AcquiredMessage` `uniq_provider_message` | (provider, message_id) | → (provider, chat_id, message_id) |
| 2 | `acquisition.acquire_message` dedup `.filter(provider, message_id)` | (provider, message_id) | + chat_id |
| 3 | `acquisition.acquire_message` race `.get(provider, message_id)` | (provider, message_id) | + chat_id |
| 4 | `models.ProviderCommand` `uniq_provider_command` | (provider, message_id) | → (provider, chat_id, message_id) |
| 5 | `acquisition._maybe_record_command` `get_or_create(provider, message_id)` | (provider, message_id) | + chat_id in the KEY (was defaults-only) |
| 6 | `models.PendingSignalApproval` `uniq_source_message` + **no chat_id field** | (source, message_id) | add `chat_id`; → (source, chat_id, message_id) |
| 7 | `services.intake_parsed` dedup `.filter(source, message_id)` | (source, message_id) | + chat_id (threaded from acquisition) |
| 8 | `acquisition._record_update` reply-origin `.filter(provider, message_id=reply_to)` | (provider, message_id) | + chat_id (reply is same-chat) |
| 9 | `acquisition._record_amendment` original = the chat-scoped `existing` row (via #2); dedup `(original, edit_hash)` | (original FK, edit_hash) | inherits #2; FK unchanged |
| 10 | Source allowlist `listener/adapter.provider_for_chat(telegram_chat_id)` | by chat | exists; + defense-in-depth chat-vs-provider guard in `acquire_message` |
| 11 | `onboard_provider` — the only non-test writer of `telegram_chat_id` (the chat trust boundary) | set/update chat | + fail-closed watermark guard on a chat switch (see below) |

**Writers of the 5 identity models (complete):** `services.py` (2× `PendingSignalApproval.create`), `acquisition.py`
(`AcquiredMessage`, `SignalUpdate`, `ProviderCommand`, `MessageAmendment`). No other app writes them.

### Fail-closed guard (§6-G, R1-hardened)

`acquire_message` applies a chat-identity guard. It fires ONLY for an **ARMED** provider (a live source); a
non-armed provider — ONBOARDING / PAUSED / INACTIVE / RETIRED — falls through to the normal arm gate
(`DROPPED_NOT_ARMED`), preserving legacy/onboarding behaviour. For an armed provider:
- **Unconfigured (R1 HIGH-1)** — armed with **no** established chat trust boundary (blank `telegram_chat_id`,
  e.g. config drift: a chat cleared after arming, or an admin edit) → `QUARANTINED`, reason
  `provider_chat_unconfigured`. An armed source without an allowlist must refuse, never accept.
- **Missing** — armed with a configured chat but a blank message `chat_id` → `QUARANTINED`, `chat_identity_missing`.
- **Mismatch** — armed, provider chat and message chat both set but `str()`-unequal → `QUARANTINED`,
  `chat_identity_mismatch`.

A refusal never intakes, never creates an approval/command, never places an order, and never advances the
watermark. The **edit/amendment path is gated on a trusted chat too**, so an untrusted-chat edit can never run
the amendment side-effects. All three are proven inert on the live path (the listener routes by chat, so a
delivered message's chat equals the armed provider's configured chat). No fabrication: a blank chat is never
back-filled at runtime.

### Non-genuine records are not part of genuine identity (R1 HIGH-2 + R2 HIGH-A)

A row that did NOT genuinely acquire the identity — because the source was not live+trusted — is recorded with
`non_genuine=True`. Two cases produce it: a fail-closed **chat-identity refusal** (a message from a then-untrusted
chat, HIGH-2) and a **`DROPPED_NOT_ARMED`** record (the provider was disabled/paused/onboarding/retired, R2 HIGH-A).
A genuine acquisition by an armed, trusted source (INTAKEN / UPDATE / STALE / content-QUARANTINED) is
`non_genuine=False`.

`non_genuine` **partitions** the uniqueness key `(provider, chat_id, message_id, non_genuine)`, so a non-genuine
record and a later **genuine** row for the same `(provider, chat_id, message_id)` **coexist**. Consequently, once
the provider is re-armed (or an authorized cutover makes the chat trusted), the genuine message is a distinct
`non_genuine=False` row and is **never suppressed** by the earlier non-genuine record — which is preserved as audit,
never deleted, never promoted into a signal, and never advances the watermark. Records stay idempotent (a
re-delivered disabled-source / untrusted message dedups within its own partition). Only `AcquiredMessage` holds
non-genuine records (approvals/commands are never created on those paths), so the flag is `AcquiredMessage`-only.

### Edit/amendment re-applies the staleness window (R2)

The edit/amendment path now re-applies the provider's acquisition staleness window (`acquisition_window_seconds`,
Nuno's 5–10 min rule) in addition to the parser-expiry check, so an edit can never resurrect a signal the fresh
path already dismissed as STALE into a fresh approval. The amendment (immutable audit) is still recorded; only the
new-approval side-effect is gated.

### Channel-switch watermark guard (`onboard_provider`, GATE-E HIGH finding)

The dedup fix is necessary but **not sufficient** for channel-switch safety. The per-provider
`watermark_last_message_id` is a chat-blind scalar used as the catch-up fetch floor (`iter_messages(min_id=…)`).
A new chat numbers its message ids independently, so inheriting the old chat's high-water mark makes catch-up
**silently skip** every new-chat message with a lower id — *before* `acquire_message` runs, so the dedup fix
never gets a chance. `onboard_provider` is the only non-test writer of `telegram_chat_id`; it now **fails closed**:
changing the chat id of a provider that carries a watermark is refused unless `--reset-watermark` is passed, which
resets the catch-up floor so the new chat is caught up from the start. This packet never invokes it — it is the
operator's explicit cutover control. (The Django admin can still edit `telegram_chat_id` directly; the cutover
runbook must use the command, not the admin.)

## Backfill determinism & migration safety (GATE B; AMBER-1 corrected)

Two migrations, both additive and behaviour-preserving (nothing deployed): `0010` (identity constraints +
`PendingSignalApproval.chat_id` + evidence-based backfill) and `0011` (`AcquiredMessage.non_genuine` + its backfill
+ the partitioned key).

- **Uniqueness can never regress.** Each OLD key already guaranteed uniqueness; the new keys are strict supersets
  (`0010` adds `chat_id`; `0011` adds `non_genuine`), so **no `AddConstraint` can fail on existing data** and no
  channel switch is needed.
- **`0011` non_genuine backfill (R2 HIGH-A).** Existing `AcquiredMessage` rows with `outcome=DROPPED_NOT_ARMED`
  (recorded while the provider was disabled) are set `non_genuine=True` — deterministic (keyed on the row's own
  IMMUTABLE outcome), completing the brand-new field, never a content change — so a historical disabled-source drop
  can never suppress a genuine acquisition once the provider is re-armed. Chat-refusal outcomes cannot exist in
  pre-PR data, so there is nothing else to classify.
- **Evidence-based approval backfill (AMBER-1).** `PendingSignalApproval.chat_id` is populated **only** from
  PERSISTED, IMMUTABLE acquisition evidence — the `chat_id` of the linked `AcquiredMessage` rows — and only when
  that evidence is unambiguous (exactly one distinct non-blank chat). It is **never** inferred from mutable
  `provider.telegram_chat_id` (not proof of a historical chat — it changes at cutover). No evidence / ambiguous /
  legacy file-intake → left `''`: **unresolved provenance is never inferred or fabricated.**
- **No ledger mutation.** `AcquiredMessage` / `ProviderCommand` / `SignalUpdate` already persist their own
  write-time `chat_id`; the backfill does **not** touch them — no in-place UPDATE of append-only evidence (data.md).
- **Certified against a populated historical migration** (`tests_migration_source_aware.py`, `MigrationExecutor`
  over 0009→0010→0011 with real legacy rows): evidence-backfill, no-evidence-stays-`''`, constraint enforcement,
  and forward/rollback/reapply.
- **Reversible** — reverse drops the new constraints/fields and restores the old constraints. Guaranteed only
  **before** a channel switch introduces a genuine cross-chat `message_id` collision (the condition the forward
  migration exists to tolerate); documented, not silent.

## Downstream identity completeness (R1 HIGH-3) — gated DARK/default-OFF

The source-aware identity now reaches the execution plan, gated so deploying it changes nothing:
- `execution/signal_planning.py` takes the plan's chat from the approval's chat-scoped identity **only when**
  `SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED` is on. **Default OFF** = the pre-T1 read
  (`approval.raw_payload.get("chat_id","")`, always blank), so `SignalExecutionPlan.chat_id` stays `''`, plan
  identity is byte-identical, and **no active trading behaviour, plan dedup, or order becomes possible merely
  because the correction is deployed**. ON (a separate cutover authorization) carries `approval.chat_id`, so two
  genuinely distinct approvals from different chats with the same source+message_id produce two distinct plans
  (unique `(source, chat_id, message_id, account)`), and a chat-qualified command cannot correlate across channels.
  Proven both OFF and ON in `execution/tests_t1_downstream_identity.py`.
- `execution/provider_commands_engine.resolve_target_plan` (R2 HIGH-B) now correlates **always chat-scoped** and
  **fails closed** on a blank/unresolved `command.chat_id` (`unresolved_chat_identity` → REJECTED, enqueues nothing)
  — `reply_to_message_id` alone can never bind a command to a plan in another channel. With the gate ON (plans
  carrying real chat) correlation is chat-exact and cross-channel isolated; with it OFF (plan chat `''`) a real-chat
  command matches none (already fail-closed). This only removes the pre-existing blank-command cross-channel path;
  no new execution authority is granted and the engine stays DARK (`PROVIDER_COMMANDS_ENABLED` off). Proven in
  `execution/tests_provider_commands_engine.py::R2CommandChatIsolationTests`.

**Unchanged (verified):** `execution/auto_router.py` (receives objects via `signal_acquired`, no chat re-lookup);
`execution_health` / `operations_summary` / `soak_report` (filter by source/provider/status/time);
`services.ingest_messages` (legacy file/batch classify — no provider/chat, operates in the `chat_id=''` partition).
`raw_payload` still carries no `chat_id` (the field is the sole source of truth).

## Cutover contract (prerequisites — NOT performed here; AMBER-2 resolved)

**Provider ownership model (the resolved contradiction).** There is **one continuous provider row** per logical
source. At cutover the **same** provider is **repurposed** — its `telegram_chat_id` is changed to the new chat and
its watermark reset — it is **not** replaced by a new provider row. The earlier contradictory instruction ("keep
the old provider row, do not repurpose it") conflated *preserving historical evidence* with *keeping a separate
provider*; it is withdrawn. Rationale: the provider's `slug` IS the downstream `source` (plan/source-config/command
scoping), so a continuous provider keeps the source identity stable, while `chat_id` — now part of row identity —
keeps old-chat and new-chat rows distinct under that one provider. Historical OLD_CHAT evidence is preserved in
place (same provider, its own `chat_id`), never deleted or repurposed.

- **Provider identity continuity:** one provider (stable `slug`/`source`) spans the switch.
- **Historical message ownership:** OLD_CHAT rows keep `(provider, OLD_CHAT, message_id)` — valid and immutable.
- **Watermark ownership:** the watermark belongs to the provider and tracks its *currently configured* chat; it is
  reset at the switch (it does not carry across chats).
- **Old-channel evidence preservation:** untouched; distinct from new-chat rows by `chat_id` (and, for refusals, by
  `non_genuine`).

A future, separately-authorised packet performs the switch. Before switching, it MUST:
1. **(BLOCKING) Reset the catch-up watermark for the new chat** — without it, catch-up's `min_id` floor silently
   drops the new chat's lower-numbered messages. Use `onboard_provider --chat-id <NEW> --reset-watermark` (the guard
   refuses the switch otherwise). A per-`(provider, chat_id)` watermark is a possible larger alternative, deferred.
2. Deploy migrations `0010`+`0011` (so every historical row carries its chat-scoped identity) and the runtime.
3. Set `SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED=1` so plans carry the real chat end-to-end (built here, DARK).
4. **Rollback:** switch the chat back (again via `--reset-watermark`) and clear the downstream flag; historical rows
   of both chats remain valid (distinct by `chat_id`); no evidence is deleted.

## Review history

### R0 — initial adversarial review (4 reviewers)
Identity/dedup/migration core sound; DARK claim holds byte-for-byte. Closed in R0: a channel-switch watermark
catch-up floor (`onboard_provider` fail-closed guard + `--reset-watermark`) and an amendment-path guard gap.
Dismissed as non-findings: int/str coercion false-mismatch, two-layer chat_id disagreement, replay double-row,
migration uniqueness regression, concurrent `intake_parsed` race (pre-existing), get_or_create MultipleObjectsReturned.

### R1 — Programme Director review of PR #495 (CI 10/10) → corrections applied
- **HIGH-1 fail-open trust boundary** — an armed provider with no configured chat accepted messages. **Fixed:**
  `_chat_identity_refusal` now refuses an armed+unconfigured provider (`provider_chat_unconfigured`); non-armed
  states still DROP. Tests 10 / 10b / 10c (incl. config drift).
- **HIGH-2 quarantined-id poisoning across cutover** — a refusal row suppressed a genuine post-cutover message.
  **Fixed:** `AcquiredMessage.chat_identity_refused` partitions the uniqueness key so refusal and genuine rows
  coexist; the refusal never suppresses or is promoted. Before/after regression tests 47 / 48 / 49.
- **HIGH-3 downstream plan collisions** — the chat stopped at the approval. **Fixed:** `signal_planning` carries
  `approval.chat_id` into the plan behind `SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED` (default OFF = byte-identical;
  ON = distinct channels distinct). Tests in `execution/tests_t1_downstream_identity.py`.
- **AMBER-1 historical migration** — **Fixed:** evidence-based backfill (persisted `AcquiredMessage.chat_id`, never
  mutable provider metadata; unresolved left `''`) + populated `MigrationExecutor` test over 0009→0010→0011.
- **AMBER-2 cutover contradiction** — **Resolved:** one continuous, repurposed provider (see the cutover contract).

A renewed adversarial review was run against the corrected subsystem; see the R1 completion report for its result.

### R2 — Programme Director review of PR #495 (R1 accepted; CI 10/10) → corrections applied
- **HIGH-A non-armed drop poisons re-armed genuine** — a `DROPPED_NOT_ARMED` row (stored `non_genuine=False`)
  suppressed a later genuine message after re-arm. **Fixed:** generalised the partition flag `chat_identity_refused`
  → **`non_genuine`** (chat-refusal OR arm-drop); `0011` backfills historical `DROPPED_NOT_ARMED` rows. Regression
  tests 53 (PAUSED→DROP→REARM→FRESH→REPLAY) / 54.
- **HIGH-B blank-chat command targets other channels** — `resolve_target_plan` chat-filtered only when
  `command.chat_id` was truthy. **Fixed:** correlation is always chat-scoped and fails closed on a blank/unresolved
  chat (`unresolved_chat_identity` → REJECTED, no job). `R2CommandChatIsolationTests` (7 cases).
- **MEDIUM historical rollback coverage** — **Fixed:** the `MigrationExecutor` test now uses populated relationships
  (association + content preserved across forward→rollback→reapply) and documents the post-cross-chat-collision
  rollback limit.
- **R2 adversarial-certification finding (MEDIUM, lifecycle sweep)** — the edit/amendment path bypassed the
  acquisition staleness window (a STALE signal could be resurrected into a fresh approval via an edit). **Fixed:**
  the amendment now re-applies `acquisition_window_seconds` in addition to parser-expiry (tests 55 / 56). Hypotheses
  A–D (disabled-source suppression, cross-channel command, duplicate downstream intentions, DARK behaviour) held
  with no confirmed findings.

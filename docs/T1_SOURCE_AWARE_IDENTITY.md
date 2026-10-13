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

### Fail-closed guard (§6-G)

`acquire_message` gains a chat-identity guard (defense-in-depth; the listener already routes by chat):
- **Mismatch** — provider `telegram_chat_id` and the message `chat_id` both non-blank and `str()`-unequal →
  `QUARANTINED`, reason `chat_identity_mismatch`, no intake, no order, no watermark advance from a trusted claim.
- **Missing** — an ARMED provider with a configured `telegram_chat_id` but a blank message `chat_id` →
  `QUARANTINED`, reason `chat_identity_missing` (provenance unconfirmable; never fabricated).
- **Edit/amendment path is gated too** — the dedup/amendment branch runs only for a trusted chat, so an
  untrusted-chat edit can never run the amendment side-effects (which could surface an approval). This keeps the
  guard's guarantee consistent on both the fresh and the edit paths (closes a GATE-E defence-in-depth finding).

Both are proven behaviour-preserving for the live/test paths: every current caller supplies a `chat_id` equal
(after `str()`) to the provider's `telegram_chat_id`; mis-routed chats never reach `acquire_message` (the listener
drops them at `provider_for_chat`). No fabrication: a blank chat is never back-filled from the provider at runtime.

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

## Backfill determinism & migration safety (GATE B)

- **Uniqueness can never regress.** The OLD key already guaranteed uniqueness; adding `chat_id` makes the key a
  strict superset, so every existing row stays unique — **no migration can fail on the new constraint**, and no
  channel switch is needed for the migration to be valid.
- **`PendingSignalApproval.chat_id` backfill** — deterministic from the linked `provider.telegram_chat_id` where
  `provider_id` is set and non-blank; otherwise `''` (legacy file-intake / pre-provider rows). The provider FK pins
  the exact provider, so the chat is unambiguous. Never fabricated.
- **`AcquiredMessage` / `ProviderCommand` / `SignalUpdate` gap-fill** — only rows whose `chat_id` is CURRENTLY `''`
  and whose provider has a single non-blank `telegram_chat_id` are filled (deterministic). Rows already carrying a
  `chat_id` are untouched (raw evidence is immutable).
- **Reversible** — reverse drops the new constraints, restores the old `(…, message_id)` constraints, and removes
  the `chat_id` field. Reverse is guaranteed only **before** any channel switch introduces a genuine cross-chat
  `message_id` collision (the exact condition the forward migration exists to tolerate); documented, not silent.

## Out of scope — downstream (documented, NOT changed)

- `execution/signal_planning.py` reads `approval.raw_payload.get("chat_id", "")`, which `intake_parsed` **never
  writes** → `SignalExecutionPlan.chat_id` is empty today. This packet does **not** write `chat_id` into
  `raw_payload` and does not touch `execution`, so plan identity and the provider-command engine's correlation are
  byte-for-byte unchanged. The new `PendingSignalApproval.chat_id` field is `signal_intake`-internal.
- `execution/auto_router.py` receives acquisition objects via the `signal_acquired` signal and never re-looks-up by
  `(provider, message_id)`/chat → unaffected.
- `execution/provider_commands_engine.resolve_target_plan` already filters by `command.chat_id` (already populated);
  adding `chat_id` to `ProviderCommand`'s uniqueness only hardens its key → unaffected.
- `execution_health` / `operations_summary` / `soak_report` filter by source/provider/status/time → unaffected.
- `services.ingest_messages` (legacy file/batch classify) has no provider/chat → operates in the `chat_id=''`
  partition; left unchanged.

## Cutover contract (prerequisites — NOT performed here)

A future, separately-authorised packet performs the Telegram source switch. Before switching, it MUST:
1. **(BLOCKING) Reset the catch-up watermark for the new chat.** This is the #1 prerequisite — without it,
   catch-up's `min_id` floor silently drops the new chat's lower-numbered messages (see the guard above). Use
   `onboard_provider --chat-id <NEW> --reset-watermark` (the guard refuses the switch otherwise). Alternatively
   adopt a per-`(provider, chat_id)` watermark — a larger change deferred to that packet.
2. Deploy this migration (so every historical row carries its chat-scoped identity) and the chat-aware runtime.
3. Repoint `execution/signal_planning.py` at `approval.chat_id` (so new plans carry the real chat) under its own
   execution-side gate — out of scope here. NB: until then, `SignalExecutionPlan.chat_id` stays `''` and the
   (DARK) provider-command engine's `resolve_target_plan` cannot correlate a command to a plan — a pre-existing
   latent state on `main`, unchanged by this packet, that this repoint resolves.
4. Keep the old provider row (do not repurpose it) so historical `(provider, chat_id, message_id)` rows stay valid.

## GATE E — adversarial review outcome

Four independent adversarial reviewers (identity-loss/suppression · fail-closed/DARK · migration/historical-safety
· edits/commands/downstream). Result: the identity/dedup/migration core is sound and the DARK claim holds
byte-for-byte. Findings addressed:
- **HIGH (identity-loss):** channel-switch watermark catch-up floor → closed with the `onboard_provider`
  fail-closed guard + `--reset-watermark`, elevated as the BLOCKING cutover prerequisite, and covered by tests
  45/46 (floor hazard + reset fix) and 42–44 (guard).
- **LOW (edits/downstream):** guard not enforced on the amendment path → closed by gating the amendment branch on
  a trusted chat (test 41).
- **LOW×2 (migration governance):** provenance pre-switch assumption + append-only data.md tension → documented in
  the migration docstring (structural key completion, not content correction); recorded as Amber.
Dismissed as non-findings: int/str coercion false-mismatch, two-layer chat_id disagreement, replay double-row,
migration uniqueness regression, concurrent `intake_parsed` race (pre-existing, unchanged), get_or_create
`MultipleObjectsReturned`.

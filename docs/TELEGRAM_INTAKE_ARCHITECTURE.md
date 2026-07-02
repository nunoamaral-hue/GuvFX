# Telegram Intake Architecture (GFX-PKT-E3-NODE-MERGE-AND-TELEGRAM-INTAKE-ARCHITECTURE)

| | |
|---|---|
| **Status** | PROPOSED design of record — design only; no listener/login/session/bot exists |
| **Date** | 2026-07-01 |
| **Scope** | Automatic ingestion of provider signals into `signal_intake`. **Ingestion ≠ execution** — nothing here changes the execution ladder, its gates, or E3 status (RED). |

## 1. Recommended architecture

A **read-only Telethon (MTProto) user-session listener** running as its own managed
container on the VPS (`guvfx-telegram-listener`, shadow-worker service pattern:
`extends`-style compose service, `restart: unless-stopped`, secrets via the
600-perm `.env`). It:

1. logs in as a **dedicated Telegram account** (Nuno's Philippines number — never
   Nuno's personal account),
2. subscribes to an **allowlist of provider chat IDs** (trust boundary),
3. on each new message, calls the existing `signal_intake.services` intake (which
   structurally cannot create an order — the one-way boundary is preserved:
   listener imports `signal_intake` only, never `execution`),
4. keeps a per-provider **watermark** (`last_seen_message_id`) for catch-up after
   downtime,
5. **never sends** a Telegram message (pure reader).

```
Wayond channel ──▶ Telethon listener (allowlisted chat_ids, read-only)
                        │  watermark catch-up · dedup · fail-closed
                        ▼
        signal_intake.services (existing: parse → PendingSignalApproval /
        quarantine; correlation id minted; SIGNAL_RECEIVED audit)
                        ▼
        existing human-gated ladder (approve → plan → promote → shadow)
```

## 2. Why Bot API is NOT sufficient

Bot API bots can only read chats **they are added to as member/admin**. Wayond is a
third-party channel (~13 subscribers, joined via shared link) — GuvFX cannot add a
bot to someone else's channel, and paid/private providers never will. A user
session that joins like any subscriber is the only mechanism that works for all
current and future providers. Bot API is therefore rejected (kept only as a
possible future *operator-notification* channel, unrelated to intake).

## 3. Data model changes (additive, backwards-compatible)

- **`signal_intake.SignalProvider`** (new): `name`, `source_key` (maps to the
  existing `source` string), `telegram_chat_id` (canonical numeric ID),
  `status` (ARMED/DISARMED — intake-side trust), `parser_profile` (which parser
  to apply), `watermark_last_message_id`, `subscription_note` (renewal date for
  paid providers — never credentials), timestamps.
- **`PendingSignalApproval.provider`** FK (nullable — old rows keep working);
  `source` string retained for compatibility.
- **`signal_intake.SignalUpdate`** (new): `provider`, `chat_id`, `message_id`,
  `reply_to_message_id`, `kind` (TP_HIT / MOVE_SL / EDIT / OTHER), nullable FK to
  the original approval, `raw_payload`, `processed=False`. Records updates —
  acting on them is a separate gated packet.
- Media: metadata inside `raw_payload["media"]` initially (no new model).

## 4. Provider → strategy mapping

One provider = one `SignalProvider` row = one `source_key` = one existing
`execution.SignalSourceConfig` row (execution-side arming + lot target stay where
they are — **two distinct gates**: intake trust in `signal_intake`, execution
arming in `execution`, honouring the one-way boundary). If providers later map to
Strategies, add a nullable `SignalSourceConfig.strategy` FK then — per-provider
**magic-number ranges** (Blueprint 06 mutual exclusion) already give attribution.

## 5. Message lifecycle

`received (listener, allowlisted chat only)` → `intake_parsed` (existing):
SIGNAL+tradeable → `PENDING_APPROVAL`; UPDATE → `SignalUpdate` (recorded,
unprocessed); UNKNOWN/media/edit → `QUARANTINED`. Then the unchanged human-gated
ladder. Observability stage 1–2 (`signal_received`, `parse_complete`) already
instrumented; the listener adds provider/chat labels.

## 6. Edit / update handling

- **Edits**: raw evidence is immutable (data rule) — an edit becomes a **new**
  record (`message_id#edit-<edit_ts>`) linked to the original, **quarantined** for
  human review; the original approval is never silently mutated. An edited
  tradeable signal is inherently suspicious.
- **Reply-chain updates** (TP hit / move SL): resolved via
  `(chat_id, reply_to_msg_id)` → original approval → stored as `SignalUpdate`.
  MVP records them (visible to the operator); automated position modification is
  **deferred and separately gated**.

## 7. Duplicate / replay handling

Existing `(source, message_id)` unique constraint + idempotent `intake_parsed`
already make replays safe; `message_id` becomes the composite
`"<chat_id>:<telegram_message_id>"` (Telegram message IDs are per-chat). Watermark
catch-up after downtime re-feeds history through the same idempotent intake;
old signals then die naturally at the existing staleness gates (120 s planning
gate + worker `stale_at_execution` re-check) — replay can never place a stale order.

## 8. Image / media handling (initial)

MVP records media **metadata only** (type, size, Telegram file id, caption) in
`raw_payload` and quarantines the message for human review. No OCR, no automatic
download in MVP (deferred; if later downloaded: hashed, stored outside git per
data rules). A screenshot-only signal therefore always requires a human.

## 9. Security model

- **Dedicated account** on the PH number; single-purpose, minimal profile, 2FA
  cloud password set. Nuno's personal account is never used.
- **Provisioning (RED, Nuno's action):** one-time interactive login (SMS code) via
  a provisioning script → Telethon **StringSession** stored in the deploy `.env`
  (600, rsync-excluded — same home as `MT5_SHADOW_WORKER_TOKEN`); `API_ID`/`API_HASH`
  from my.telegram.org likewise env-only. Never in git/logs (secret scanner active).
- **Trust boundary = chat-id allowlist** (closes gap Area 4): messages from any
  non-ARMED chat are dropped and counted, never parsed. Sender/channel identity
  recorded in `raw_payload` for audit.
- **Read-only client**: never sends messages, never joins channels autonomously
  (joining is a manual Nuno action). Low API rate; ban/flood-wait risk minimised.
- **Revocation runbook**: Telegram active-sessions revoke + StringSession rotation.

## 10. Operational runbook implications

New runbook sections: (a) account/session provisioning + rotation/revocation,
(b) provider onboarding checklist (join channel manually → create SignalProvider →
verify chat_id → ARM intake → separately arm execution), (c) listener health
(heartbeat + `signal_received` rate; container restart), (d) downtime catch-up
verification (watermark advance), (e) flood-wait/ban response (stop container,
assess, rotate if needed).

## 11. MVP scope (the safest listener)

Telethon read-only listener container + `SignalProvider` model + chat-id allowlist
+ composite-id dedup + watermark catch-up + `SignalUpdate` recording + media/edit
quarantine + observability labels. **Automatic ingestion only** — approval,
planning, promotion stay manual (existing 4-step operator flow). Queueing: the
single-threaded listener processes messages FIFO; burst arrival is arbitrated
downstream by the existing concurrent-position/daily risk gates.

## 12. Deferred scope (explicit)

OCR / image parsing; acting on TP-hit/move-SL updates (position modification);
auto-approval of signals (needs the RBAC packet + its own RED decision);
news-warning → risk-event automation (MVP just quarantines them); multi-session
redundancy; provider performance analytics; any Bot API use.

## 13. Human approval & manual-first list

**Needs a human before automatic execution:** signal approval (existing gate),
provider arming (both intake + execution), account/session provisioning, edits,
media-only signals, and — for E3 itself — Blueprint 06 ratification + Nuno's
recorded sign-off. **Manual in the first implementation:** approve/reject, plan,
promote, provider onboarding, session login.

## 14. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Telegram limits/bans user-bots on new accounts | MEDIUM | read-only, low-rate, official API creds, 2FA, no autonomous joins |
| Session string leak | MEDIUM | env-only (600, rsync-excluded), scanner, revocation runbook |
| Provider format drift breaks parser | MEDIUM | quarantine fail-closed + parser_profile per provider |
| Edited signals mislead execution | LOW (MVP) | edits quarantined, never mutate originals |
| Listener SPOF / downtime | LOW | watermark catch-up + staleness gates make missed windows safe |
| PH SMS dependency for re-login | LOW | persistent StringSession + 2FA password; re-login rare |

## 15. Recommended implementation packet

**`GFX-PKT-TELEGRAM-INTAKE-MVP`** — implement §11 (listener container, models,
allowlist, dedup, watermark, quarantine paths, tests, runbook), **preceded by the
RED prerequisite** `GFX-PKT-TELEGRAM-ACCOUNT-PROVISIONING` (Nuno's interactive
account creation + session generation — credential action, his hands). No
execution-side change in either packet; E3 gates unaffected.

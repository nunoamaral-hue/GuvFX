# Signal Acquisition Platform Architecture (GFX-PKT-SIGNAL-ACQUISITION-PLATFORM-ARCHITECTURE)

| | |
|---|---|
| **Status** | PROPOSED design of record — **design only**, no listener/session/login exists |
| **Date** | 2026-07-01 |
| **Relationship** | **Extends** `docs/TELEGRAM_INTAKE_ARCHITECTURE.md` (single-listener design) into a multi-provider platform. That doc's core decisions (read-only Telethon user-session, chat-id allowlist trust boundary, quarantine, replay-safety) carry forward unchanged; this document is the provider-platform layer on top. See §13. |
| **Boundary** | **Acquisition ≠ execution.** Nothing here places an order, changes the execution ladder/gates, or affects E3 (still RED). `signal_intake`/the platform never import `execution`. |

---

## 1. Recommended platform architecture

A **provider-driven acquisition platform**: one read-only Telethon user-session
(the dedicated PH-number account) subscribed to N provider chats; a small
**dispatcher** that, per inbound message, resolves the **provider by chat id**
(allowlist = trust), applies that provider's **parser profile**, dedups, enforces
the **5–10 min acquisition window**, and hands tradeable signals to the existing
`signal_intake.services` (which structurally cannot place an order). Everything
downstream (approval → plan → promote → shadow) is unchanged.

```
providers (manual onboarding, chat links from Nuno)
   │  join channel (manual) → SignalProvider row → ARM
   ▼
Telethon user-session (1 account, PH number) ── read-only ──▶ Dispatcher
   • resolve provider by chat_id (allowlist)      │ dedup · window · quarantine
   • parser_profile per provider                  ▼
   signal_intake.services.intake_parsed ─▶ PendingSignalApproval / SignalUpdate / quarantine
   ▼
   existing human-gated ladder (approve → plan → promote → shadow)   [unchanged]
```

Design principle: **providers are data, not code.** Onboarding a provider is
creating a `SignalProvider` row + arming it — no code change per provider (only a
new *format* needs a new parser profile, later).

---

## 2. Data model proposal (additive; not built in this packet)

- **`signal_intake.SignalProvider`** — the platform's core row:
  `name`, `slug` (stable key, replaces/loads the `source` string),
  `telegram_chat_id` (canonical numeric id — the identity), `chat_title`,
  `status` (see §3), `parser_profile` (FK/slug → which parser), `disabled_reason`,
  `last_signal_at`, `watermark_last_message_id`, `subscription_note` (renewal date,
  **never credentials**), `created_by`, timestamps.
- **`signal_intake.ParserProfile`** — `slug`, `description`, `version`, `active`;
  maps a provider to a parser strategy (§18). MVP has one profile (`wayond_v1`).
- **`PendingSignalApproval.provider`** — nullable FK (old rows keep working); the
  `source` string is retained for compatibility and derived from `provider.slug`.
- **`signal_intake.SignalUpdate`** — `provider`, `chat_id`, `message_id`,
  `reply_to_message_id`, `kind` (TP_HIT/MOVE_SL/EDIT/OTHER), nullable FK to the
  original approval, `raw_payload`, `processed=False`. Records updates; **acting on
  them is a separate gated packet.**
- **`signal_intake.QuarantinedMessage`** (or reuse `PendingSignalApproval`
  `QUARANTINED` status) — see §7.
- **Execution-side link:** the existing `execution.SignalSourceConfig`
  (`source` unique + `auto_demo_execution_enabled` + `total_lot_target`) stays as
  the **execution arming gate**, keyed by `provider.slug`. Two distinct gates
  (intake trust in `signal_intake`, execution arming in `execution`) preserve the
  one-way boundary.

---

## 3. Provider status lifecycle (§2, §3)

`ONBOARDING` (row created, not yet verified) → `ARMED` (verified chat id, actively
acquired) → `PAUSED` (operator-disabled — messages dropped + counted, not parsed)
→ `INACTIVE` (auto-flagged: no signal ≈ 1 month — alert only, still listening) →
`RETIRED` (removed). `status` is **operator-declared** except the `INACTIVE`
auto-flag (a derived health hint, never auto-parses/auto-retires). Only `ARMED`
providers feed intake.

**Disable/enable (§3):** setting `PAUSED` (or not `ARMED`) makes the dispatcher
drop that provider's messages immediately — **individually disableable**, no
listener restart. Re-arming resumes; the watermark bounds catch-up (§10).

---

## 4 & 5. Provider → strategy routing + multi-strategy-per-provider

**Preferred (Nuno): strategy-per-provider.** Model a nullable
**`SignalSourceConfig.strategy` FK** (execution side) so an armed provider's
planned signals attribute to a Strategy (existing `strategies.Strategy` +
`StrategyAssignment` → account/stage). Because it's a config row per provider,
**one provider can map to multiple strategies** via multiple config rows (or a
through-table `ProviderStrategyRoute(provider, strategy, account, enabled)`), and
the **same strategy** can be fed by multiple providers. Per-route **magic-number
ranges** (Blueprint 06 mutual exclusion) keep positions attributable. Routing lives
on the **execution** side (arming), not intake — the boundary holds. MVP can ship
with a single implicit strategy and add the FK when the second provider lands
(avoids overbuild).

---

## 5-policy. Staleness / replay policy (§9, §10) — Nuno's rule

- **Acquisition window = 5–10 min (configurable, default 10 min).** A message
  whose event time is older than the window at *acquisition* is recorded as
  **stale/dismissed** — never handed to planning. This is an **acquisition-layer**
  guard on top of the existing execution-layer staleness (120 s planning gate +
  worker `stale_at_execution` re-check), so an old signal can never place an order.
- **No indefinite replay.** Catch-up after downtime exists **only** to (a) advance
  the per-provider watermark so nothing is double-processed, and (b) **record**
  missed messages (as stale/quarantine) for audit. Fresh (<window) messages missed
  during a brief outage are processed normally; everything older is recorded stale.

---

## 6. Message lifecycle (§12, §13, §14)

`received (Telethon, ARMED provider only)` → resolve provider by chat id →
dedup (§12) → window check (§ staleness) → parser_profile →
- **SIGNAL + tradeable + fresh** → `intake_parsed` → `PENDING_APPROVAL`;
- **UPDATE** (TP hit / move SL, resolved via `reply_to_message_id`) → `SignalUpdate`
  (recorded, **not acted on** in MVP);
- **EDIT** (§13) → raw evidence is immutable → a **new** record keyed
  `message_id#edit-<ts>` linked to the original, **quarantined** (an edited
  tradeable signal is inherently suspicious; never silently mutate the original);
- **UNKNOWN / media / stale** → quarantine/stale record.

Observability stages 1–2 (`signal_received`, `parse_complete`) already exist; the
platform adds `provider`/`chat_id` labels.

**Duplicates (§12):** dedup key = composite `"<chat_id>:<telegram_message_id>"` on
the existing `(source, message_id)` unique constraint + idempotent `intake_parsed`.
**Opposite signals from different providers (§11):** each provider is an
independent stream; both are acquired and processed → **both trades taken** (Nuno's
rule). Risk arbitration (exposure/concurrency caps, per-account) happens downstream
in the merged risk controls — acquisition does not net or suppress them.

---

## 7. Quarantine policy (§17)

Anything not a clean, fresh, tradeable signal is **quarantined, not discarded**
(raw evidence immutable): unparseable text, media/screenshots, edits, non-trade
news/NFP warnings, messages from a non-`ARMED` chat, and stale messages. A
quarantined record carries provider + chat id + reason + raw payload for human
review. Quarantine is fail-closed (never guessed into a tradeable approval) and
has no auto-cleanup TTL initially (audit retention; a cleanup command is a later
operational choice). Non-trade warnings (NFP etc.) are **stored/quarantined** now;
turning them into risk events is deferred.

---

## 8. Monitoring / alerts (§15, §16) — Nuno's rules

- **Provider health:** providers assumed healthy by default; `last_signal_at`
  drives an **INACTIVE alert if no signal ≈ 1 month** (derived hint, not
  auto-retire). Emitted via the existing observability metrics + a health check.
- **Telegram rate-limit / flood-wait / ban → alert.** The listener catches
  Telethon `FloodWaitError`/limit responses and raises an operator alert (and backs
  off); persistent limits stop the container pending review.
- **Listener liveness:** heartbeat + `signal_received` rate (existing observability)
  so a silent listener is visible.

---

## 9. MVP scope

- `SignalProvider` + `ParserProfile` (one profile `wayond_v1`) + chat-id allowlist.
- One read-only Telethon user-session (PH account) + dispatcher (resolve provider,
  parser dispatch, dedup, 5–10 min window, quarantine).
- Per-provider **enable/disable** (`PAUSED`), watermark catch-up (record-only for
  stale), `SignalUpdate` recording, provider-health + flood-wait alerts.
- **Automatic acquisition only**; approval/plan/promote stay **manual** (existing
  operator flow). Focus on the **current Wayond format** (§ format decision).

---

## 10. Deferred scope

Acting on TP-hit/move-SL updates (position modification); OCR / image parsing /
format generalisation; auto-approval; news→risk-event automation; multi-session
redundancy; the `ProviderStrategyRoute` through-table (add at 2nd strategy);
provider performance analytics; any Bot API; quarantine auto-cleanup TTL.

---

## 11. Risks

| Risk | Sev | Mitigation |
|---|---|---|
| Telegram bans/limits a new user-bot | MED | read-only, low-rate, official API creds, 2FA, no autonomous joins; flood-wait alert + backoff |
| Session-string leak | MED | env-only (600, rsync-excluded), secret scanner, revocation runbook (see credential framework) |
| Provider format drift breaks parser | MED | quarantine fail-closed + versioned `parser_profile`; add a profile, don't hack the parser |
| Opposite/near-simultaneous signals over-expose the account | MED | downstream risk controls (exposure/concurrency) — acquisition doesn't gate; **dedicated demo account per Blueprint 06 R2** |
| Listener SPOF / outage | LOW | watermark + 5–10 min window make missed windows safe (recorded stale) |
| PH SMS dependency for re-login | LOW | persistent StringSession + 2FA; re-login rare |
| Multi-provider complexity creep | LOW | providers-as-data; strategy routing deferred until 2nd provider |

---

## 12. Recommended implementation sequence

1. **`GFX-PKT-TELEGRAM-ACCOUNT-PROVISIONING`** (RED, **Nuno's hands**) — create the
   PH-number account, generate the Telethon StringSession into the deploy `.env`
   (600). Credential action; no code executes his login.
2. **`GFX-PKT-SIGNAL-ACQUISITION-MVP`** (repo-first) — `SignalProvider`/`ParserProfile`
   models + migration, dispatcher, dedup/window/quarantine, `SignalUpdate` recording,
   health/flood-wait alerts, tests. Listener runs read-only against fixtures/session.
3. **`GFX-PKT-SIGNAL-ACQUISITION-DEPLOY`** (gated) — managed listener container
   (shadow-worker pattern), arm Wayond, verify acquisition.
4. Later, gated: strategy routing FK, update-acting, news→risk. **None touch
   execution/E3.**

---

## 13. Supersede vs extend

**Extends** `docs/TELEGRAM_INTAKE_ARCHITECTURE.md` — that document's single-listener
decisions (Telethon user-session, chat-id allowlist, dedup, quarantine,
replay-safety) remain valid and are the foundation. This document is the
**multi-provider platform layer** (providers-as-data, lifecycle, routing, parser
profiles, health, the tightened 5–10 min acquisition window). The intake doc should
be marked *"superseded for the multi-provider platform by
`docs/SIGNAL_ACQUISITION_PLATFORM.md`; retained for the core listener rationale."*
(Marking edit deferred to the MVP packet to avoid churn; noted here.)

---

*Design/read-only packet: no implementation, migration, Telegram login/session,
production change, service restart, E3 code, `order_send`, or real demo order.*

# Signal Acquisition MVP — Implementation Plan (GFX-PKT-SIGNAL-ACQUISITION-MVP-DESIGN-TO-BUILD)

| | |
|---|---|
| **Status** | PLAN (design-to-build). No code built in this packet. |
| **Design of record** | `docs/SIGNAL_ACQUISITION_PLATFORM.md` (PR #64, merged) + `docs/TELEGRAM_INTAKE_ARCHITECTURE.md` |
| **Boundary** | Acquisition ≠ execution. Nothing here places an order or affects E3 (RED). `signal_intake` never imports `execution`. |

## Phasing (the key decision)

The MVP splits into a **repo-only, testable core** (safe to build now, no Telegram)
and a **thin listener adapter** (Nuno-gated, needs the PH-number session):

- **Phase 1 — Acquisition core (repo-only, SAFE NOW):** models, parser registry,
  the pure **dispatcher**, `signal_intake` integration, admin, tests. Exercised
  entirely with **fixture message dicts** — no Telegram client, no session, no
  network, no deploy.
- **Phase 2 — Provisioning (RED, Nuno's hands):** create the PH-number Telegram
  account + Telethon StringSession into the deploy `.env` (600). No code executes
  his login.
- **Phase 3 — Listener adapter + deploy (gated):** a thin Telethon reader that
  turns real `new_message` events into the same message dict Phase 1 already
  consumes, in its own managed container.

Phase 1 is a complete, valuable, order-free deliverable on its own.

## 1. MVP implementation plan (Phase 1 detail)

**Dispatcher — the brain (`signal_intake/acquisition.py`).** A pure function
`acquire_message(provider, message: dict, *, now=None) -> AcquiredMessage` where
`message = {message_id, chat_id, text, date (epoch/aware), reply_to_message_id,
edit_date, media}`. Steps, all fail-closed:
1. **Dedup** — if an `AcquiredMessage(provider, message_id)` exists → outcome
   `DUPLICATE`, return (idempotent; safe for catch-up replay).
2. **Staleness window** — if `now − message.date > provider.acquisition_window_seconds`
   (default 600 s; per-provider 300–600) → outcome `STALE`, recorded, **not parsed**.
3. **Edit guard** — `edit_date` present → outcome `QUARANTINED` (edited signal;
   original never mutated).
4. **Parser dispatch** — `ParserProfile` slug → parser callable (registry);
   `wayond_v1` wraps the existing `intelligence.telegram_source.parse_message`.
5. **Route by kind** — SIGNAL+tradeable → `signal_intake.services.intake_parsed`
   → `PendingSignalApproval` (outcome `INTAKEN`, correlation id minted, existing
   ladder unchanged); UPDATE (TP-hit/move-SL via `reply_to_message_id`) →
   `SignalUpdate` (outcome `UPDATE`, recorded, **not acted on**); else → media/
   news/unknown → `QUARANTINED`.
6. **Bookkeeping** — advance `provider.last_signal_at` + `watermark_last_message_id`;
   write the `AcquiredMessage` ledger row.

Only `ARMED` providers are dispatched; a `PAUSED`/non-armed provider's messages are
dropped + counted (outcome `DROPPED_NOT_ARMED`). The dispatcher is the **only**
place message policy lives; the Phase-3 listener just supplies the dict.

## 2. Files expected to change / add

| File | Phase | Change |
|------|-------|--------|
| `backend/signal_intake/models.py` | 1 | + `SignalProvider`, `ParserProfile`, `AcquiredMessage`, `SignalUpdate`; nullable `PendingSignalApproval.provider` FK |
| `backend/signal_intake/acquisition.py` | 1 | **new** — `acquire_message` dispatcher (pure, fail-closed) |
| `backend/signal_intake/parsers/__init__.py` | 1 | **new** — parser registry (slug → callable); `wayond_v1` wraps `telegram_source.parse_message` |
| `backend/signal_intake/services.py` | 1 | small: accept a `provider` when creating an approval (backwards-compatible) |
| `backend/signal_intake/admin.py` | 1 | + `SignalProviderAdmin` (arm/pause actions, gated), read-only `AcquiredMessage`/`SignalUpdate` |
| `backend/signal_intake/management/commands/onboard_provider.py` | 1 | **new** — create/verify/arm a provider (chat id from Nuno's link) |
| `backend/signal_intake/tests_acquisition.py` | 1 | **new** — dispatcher + lifecycle + dedup + window + quarantine tests |
| `backend/signal_intake/migrations/0004_*.py` | 1 | **new** — the additive models |
| `backend/signal_intake/listener/telethon_listener.py` | 3 | **new** — thin Telethon adapter (Nuno-gated) |
| `deploy/signal-listener/*` | 3 | **new** — managed container (shadow-worker pattern) |
| `docs/STATUS.md`, `signal_intake/README.md` | 1 | update |

## 3. Migration plan

One additive migration `signal_intake.0004` (new tables + nullable
`PendingSignalApproval.provider` FK, `default=None`). Backwards-compatible: existing
approvals/audits unaffected; no data migration. A tiny **data migration or fixture**
seeds one `ParserProfile(slug="wayond_v1")` and (optionally) a `SignalProvider` for
Wayond in `ONBOARDING` (chat id filled at onboarding, never committed).

## 4. Test plan (Phase 1 — fixture dicts, no Telegram)

`tests_acquisition.py`: dedup (same message twice → 1 INTAKEN + 1 DUPLICATE);
staleness (message older than window → STALE, no approval, no parse); fresh signal
→ INTAKEN + `PendingSignalApproval` + correlation id; UPDATE (reply) → `SignalUpdate`,
no approval; edited message → QUARANTINED, original untouched; media/news →
QUARANTINED; non-`ARMED` provider → DROPPED_NOT_ARMED; watermark + `last_signal_at`
advance; parser-registry dispatch (`wayond_v1`); provider enable/disable; onboard
command creates/arms; **AST/import guard: `signal_intake` never imports `execution`
and the dispatcher never calls order_send.** Reuse the existing `SIGNAL_MSG` fixture.
Target: no order path, full branch coverage of the dispatcher.

## 5. Operator flow

1. Nuno provides the provider's Telegram link. Operator joins the channel with the
   GuvFX account (manual).
2. `manage.py onboard_provider --slug wayond --chat-id <id> --parser wayond_v1`
   → creates the `SignalProvider` (verifies the chat id), status `ONBOARDING`.
3. Operator reviews, then **arms** it (admin action / `--arm`).
4. Acquisition begins (Phase 3 listener); approvals appear in the existing review
   queue → the unchanged manual approve → plan → promote ladder (RBAC-gated).
5. Disable any provider anytime (`PAUSE`); re-arm to resume.
6. Onboard execution-side arming separately (`SignalSourceConfig` per provider) —
   two distinct gates.

## 6. Deployment plan

- **Phase 1** deploys with the backend on the next routine backend deploy (apply
  `0004`; additive, online-safe). No listener yet — nothing acquires until Phase 3.
- **Phase 3** — managed `guvfx-signal-listener` container (shadow-worker compose
  pattern: `extends`, `restart: unless-stopped`, session/API creds via the 600
  `.env`), a distinct process, read-only. Verify via a fixture-fed dry-run before
  arming a live provider. (Separate gated deploy packet.)

## 7. Nuno-held steps

- **Provisioning (Phase 2, RED):** create the dedicated PH-number Telegram account
  (2FA), generate the Telethon StringSession, place session + `API_ID`/`API_HASH`
  in the deploy `.env`. GuvFX **must not** use Nuno's personal account.
- Provide provider links; join channels; decide execution-side arming + `RISK_MAX_*`
  per provider. Approve the Phase-3 deploy.

## 8. Risks

| Risk | Sev | Mitigation |
|---|---|---|
| Scope creep (build listener before core is proven) | MED | strict phasing — Phase 1 fully testable without Telegram |
| New tables unused until Phase 3 | LOW | additive/nullable; harmless; documented |
| Parser coupling to Wayond format | MED | `ParserProfile` registry — new format = new profile, not a hack |
| Session/creds handling (Phase 3) | MED | env-only (600), scanner, revocation runbook (credential framework) |
| Provider chat-id spoofing | LOW | allowlist trust boundary; chat id verified at onboarding |

## 9. Can implementation begin repo-only immediately?

**Yes — Phase 1 only.** The models + parser registry + dispatcher + `signal_intake`
integration + admin + onboard command + tests are entirely repo-only, testable with
fixture message dicts, additive/backwards-compatible, and place no order. Phase 2
(Nuno provisioning) and Phase 3 (listener + deploy) must **not** begin here (no
Telegram login/session, no deploy per this packet's requirements).

## 10. Recommended next packet

**`GFX-PKT-SIGNAL-ACQUISITION-MVP-CORE`** — implement **Phase 1** (repo-only:
models + migration `0004` + parser registry + dispatcher + integration + admin +
onboard command + tests), reviewed and merged like any implementation packet. Then,
in parallel/after: **`GFX-PKT-TELEGRAM-ACCOUNT-PROVISIONING`** (RED, Nuno) →
**`GFX-PKT-SIGNAL-ACQUISITION-LISTENER-DEPLOY`** (Phase 3). None touch execution/E3.

---

*Planning packet: no implementation, migration, Telegram login/session, production
change, service restart, E3 code, `order_send`, or order was created.*

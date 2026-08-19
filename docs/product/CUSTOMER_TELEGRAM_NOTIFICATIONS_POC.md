# Customer Telegram notifications — beta POC

Status: **reconciled release candidate, DARK, activation human-gated** (verified 2026-08-19). This document is the
Programme Director review packet. No production bot, webhook, worker, migration,
or customer notification has been activated.

## 1. Forensic architecture map

The new `customer_notifications` Django app is an output-only customer plane.
It is separate from provider-channel Telegram ingestion and from existing global
stakeholder/operations alerts. It observes committed durable facts, writes its own
outbox, and uses its own dedicated worker and bot credentials.

```text
durable facts after commit                 customer plane
Trade / TradeOutcomeRecord  ─┐
AuditEvent                   ├─> allow-listed projection -> CustomerNotification
WorkspaceTransition          ┤                                  |
customer-visible OpsEvent   ─┘                    dedicated worker + customer bot
                                                               |
authenticated GuvFX user -> one-use /start token -> private Telegram chat.id
```

Notification code does not decide, authorize, route, claim, size, or execute a
trade. Observer and rendering failures are fail-open. A reconciler can recover
missed projections from the durable sources.

## 2. Selected identity model

`CustomerTelegramBinding` has one row per GuvFX user and stores Telegram's positive,
signed-64-bit-safe `chat.id` as the delivery authority. Input is range checked before
persistence. A partial unique constraint
prevents one active private chat from belonging to two GuvFX users. Telegram user
ID is corroborating metadata; username and first name are display-only. Settings
never returns the numeric chat ID. Disconnect atomically deactivates the binding,
invalidates unused tokens, and the worker rechecks the active binding before every
send.

## 3. Connection-token model

`TelegramConnectionToken` stores a unique SHA-256 digest, user, expiry, creation,
and consumption timestamp. `secrets.token_urlsafe(32)` produces a 43-character
opaque start parameter. The default lifetime is 600 seconds, capped at 30 minutes.
Only one unconsumed token remains valid per user. Redemption locks the token,
checks expiry/reuse, resolves the owner server-side, prevents cross-user chat
reuse, saves the binding, and consumes the token in one transaction.

## 4. Migration

`customer_notifications/migrations/0001_initial.py` creates:

- `CustomerTelegramBinding`
- `TelegramConnectionToken`
- `CustomerNotificationPreference`
- `CustomerNotificationProjectionCursor`
- `CustomerNotificationWorkerState`
- `CustomerNotification`
- immutable `CustomerNotificationAttempt`
- unique binding, token, dedupe, and attempt constraints plus queue/owner indexes

The attempt FK is protected and attempt rows reject instance update/delete and
bulk update/delete. Historical delivery attribution therefore survives a binding
disconnect and cannot be edited through the application or admin.
The outbox user FK is nullable `SET_NULL`: deleting a customer removes their binding,
tokens, and preferences without deleting durable outbox/attempt evidence or blocking
the deletion. A retained row with no user is suppressed before delivery. Migration
`0001` is additive only; reversing it removes all notification-plane tables and their
evidence, so rollback after use requires worker shutdown, an evidence export/backup,
and explicit retention approval before the reverse migration. No production migration
is authorized by this packet.

## 5. Webhook endpoint contract

`POST /api/customer-notifications/telegram/webhook/`

- unauthenticated only at the HTTP/session layer; protected by Telegram's
  `X-Telegram-Bot-Api-Secret-Token`, compared in constant time
- dedicated `customer_telegram_webhook` rate limit: 120/minute per source
- 404 while the feature/bot/webhook configuration is DARK or incomplete
- accepts `message` updates only; other update types are acknowledged and ignored
- accepts only a private chat where authoritative `chat.id == from.id`
- accepts exact `/start <opaque-token>` binding input only
- never logs the raw connection token or bot token
- returns a bounded machine code on invalid/expired/reused/conflicting tokens

## 6. Settings API contract

All customer settings routes require the authenticated GuvFX owner:

- `GET /api/customer-notifications/telegram/` — availability, connection state,
  display-only Telegram name, and preferences; never `chat.id`
- `POST /api/customer-notifications/telegram/connect/` — creates the one-use
  deep link; any submitted `chat_id` is ignored
- `POST /api/customer-notifications/telegram/disconnect/` — immediately
  deactivates the owner's binding
- `PATCH /api/customer-notifications/telegram/preferences/` — owner-only boolean
  preferences and `en`/`ja` language
- `GET /api/customer-notifications/health/` — staff-only queue aggregate

## 7. Preferences model and API

`CustomerNotificationPreference` has a master flag plus trade opened, trade
updated, trade closed, strategy changed, execution problem, and workspace ready flags. Every
flag defaults ON. Language defaults to English. Preferences can only suppress
messages; they have no dependency from, or effect on, execution state.

## 8. Notification outbox

`CustomerNotification` carries the server-resolved user and optional owned
account, event type, source reference, unique dedupe key, strictly allow-listed
presentation payload, language snapshot, queue state, bounded attempt count, next
attempt, delivery time, and a bounded error code. `CustomerNotificationAttempt`
is append-only proof with the recipient chat snapshot and Telegram message ID.

The outbox is isolated from source transactions with post-commit, robust,
exception-swallowing observers. A recipient is never supplied by an event caller.
The reconciler persists a per-source high-water mark and processes bounded batches in
their own transactions. The cursor advances only after each source batch projects
successfully; a transient projection error rolls back both batch and cursor for retry.
This prevents a fixed historical slice from starving newer durable events.

## 9. Event-source mapping

| Customer event | Authoritative durable seam | Dedupe |
|---|---|---|
| Trade opened | created `trading.Trade` successful fill record | trade PK |
| Trade update / TP progress | created `execution.TradeOutcomeRecord` while durable plan legs remain open | outcome PK |
| Trade closed | created `execution.TradeOutcomeRecord` when all durable plan legs are closed (or no leg plan exists) | outcome PK |
| Strategy enabled/disabled | `core.AuditEvent` for the saved assignment transition | audit UUID |
| Execution problem | customer-visible non-INFO `OperationalEvent` | account + class + hour |
| Workspace ready | state-changing `WorkspaceTransition` to `EXECUTION_READY` | transition PK |

Trade-open rendering uses the persisted trade volume. Plan/assignment data is
read only to obtain strategy/SL/TP presentation fields. A signal, plan, pending
job, or attempted order cannot generate a trade-opened message.
Trade progress is derived only from the account-scoped durable leg-evidence
contract already used by execution notifications. It reports closed/total legs,
the current reached TP when proven, and summed realised PnL. The observer does not
modify a plan, leg, outcome, job, or order; absent durable evidence means no
manufactured TP progress.

## 10. Delivery-worker design

`run_customer_notification_worker` is a dedicated non-execution process and has an
independent, default-OFF worker flag. When both flags are enabled it runs
the cursor-backed durable-fact reconciler, then claims eligible outbox rows with an atomic
compare-and-set. It revalidates binding, ownership, master preference, and event
preference immediately before delivery. Known definite failures use bounded
exponential retry (default maximum five attempts).
The production worker service uses only the backend image plus a dedicated protected environment file with
Django/database runtime settings and customer-notification keys. It does not receive or require MT5, bridge,
node, WorkerIdentity, execution, provider-Telegram, or WIMs credentials.

The design is deliberately **at most once**, not mathematically exactly once:
after a send is claimed, `PROCESSING` is never automatically reclaimed. A network
or response ambiguity fails terminally. If Telegram acknowledges and the database
then fails, the row remains `PROCESSING` for operator review rather than risking a
duplicate customer message. Queue health reports both enable flags, queue depth
(including processing), oldest queued age, processing/retrying/suppressed totals,
delivery success/failure totals, ambiguous delivery count, and retry exhaustion.
Health also reports total/active binding counts and a durable worker heartbeat/state. It never contains a chat
ID, token, secret, payload, or customer identifier. ACTIVE thresholds are heartbeat older than 120 seconds
(alert), oldest pending older than 900 seconds (warning), any definite failure/retry exhaustion (alert), and
any ambiguity (operator review, never replay). A heartbeat is not required while DARK.

Telegram is a **best-effort convenience notification channel**. Committed GuvFX
business records and the authenticated in-app experience remain the system of record.
An omitted, delayed, ambiguous, or failed Telegram message never changes that state.

## 11. EN/JA message catalogue

Messages are rendered from one bounded catalogue in `messages.py`; event inputs
are machine codes and allow-listed fields, with English fallback.

English trade-open example:

```text
GuvFX trade opened

Wayond WIM
XAUUSD · Sell
0.01 lots
Entry: 4343.44
Stop loss: 4349.69
Take profit: 4341.07

Demo account · 1302587
```

Japanese trade-open example:

```text
GuvFXの取引が開始されました

Wayond WIM
XAUUSD · 売り
取引量: 0.01ロット
エントリー: 4343.44
ストップロス: 4349.69
テイクプロフィット: 4341.07

デモ口座 · 1302587
```

The catalogue also includes connection confirmation, trade update, trade closed, strategy
enabled/disabled, three customer-safe execution-problem variants, and workspace
ready in both languages. Trade update/close messages include the server-derived
full MT5 account number in that owner's verified private chat and an ISO UTC
timestamp where available. It never reads raw diagnostic fields.

## 12. Frontend Settings UX

The Profile page now contains one Telegram notifications card. Disconnected users
see a connection button only when the server reports an approved bot available.
Connect opens the short-lived Telegram deep link and polls for confirmed binding.
Connected users see display-only metadata, disconnect, the master preference, and
six event toggles. The card exposes disconnected, connecting, and connected states.
Switching the GuvFX app language immediately persists the
notification language without requiring an unrelated preference change. No numeric
Telegram identifier or backend error is displayed.
The shared frontend API base now supports `NEXT_PUBLIC_API_BASE` for safe local
verification and retains `https://api.guvfx.com` as its production default.

## 13. Desktop and mobile EN/JA evidence

Visual artifacts:

- `docs/product/evidence/customer-telegram/settings-en-desktop.png`
- `docs/product/evidence/customer-telegram/settings-ja-desktop.png`
- `docs/product/evidence/customer-telegram/settings-en-mobile.png`
- `docs/product/evidence/customer-telegram/settings-ja-mobile.png`

The screenshots use a local, non-production API fixture. No Telegram request or
customer notification was made.

## 14. Privacy and isolation proof

Tests prove token-owner binding, active-chat uniqueness, owner-scoped API and
preferences, private-chat enforcement, no submitted-chat-ID trust, no chat ID
disclosure, cross-user/cross-account enqueue rejection, disconnect-before-send,
display-only username behavior, inactive/deleted-user suppression, and observation-only
Django admin surfaces that exclude chat IDs and token digests. Aggregate staff health
and worker output contain no recipient or credential values. Payloads
pass through per-event allow-lists.

## 15. Dedupe and at-most-once proof

A unique outbox dedupe key collapses repeated source projection. Atomic claim
prevents two workers claiming the same pending row. Delivered rows are never
eligible again. A known failure may retry only before acknowledgement; successful
acknowledgement is tested to result in one call. Ambiguous failures are terminal.

## 16. Telegram outage and execution isolation proof

Bot absence, Telegram 4xx/429/5xx responses, timeout/network ambiguity, malformed
responses, rendering failure, observer
failure, and acknowledgement-persistence failure are contained in the customer
plane. An observer exception cannot roll back creation of the durable trade.
Neither the outbox nor worker is imported by an execution claimant or bridge.

## 17. Focused tests

The focused backend suite covers 63 tests, including all 28 mandatory adversarial
cases plus webhook update filtering, settings data minimization, durable event
mappings, actual per-trade volume, hourly outage dedupe, preference ownership,
cross-user retrieval isolation, immutable attempt evidence, cursor advancement and
rollback, retry exhaustion, per-customer account-number isolation, unsupported-command
execution non-authority, missing-binding no-fallback, durable multi-leg progress,
and EN/JA catalogue behavior. The focused frontend suite has 6 tests covering
disconnected, connecting, and connected states, preferences, toggling, and EN/JA behavior.

## 18. Full verification gate

The final isolated-database `make check` completed with exit code 0:

- backend: 4,349 tests passed (1 skipped)
- frontend Vitest: 46 files / 286 tests passed
- frontend lint: 0 errors / 19 pre-existing warnings
- frontend parity: 46 routes, 56 components, no junk, 5 allow-listed env vars
- frontend production build: compiled, typechecked, and generated all 41 pages
- `customer_notifications` migration drift: none

A second production-mode build with the local fixture API base also passed solely
to capture the four visual artifacts in section 13; it did not contact production.

## 19. Adversarial review result

Review lenses: identity confusion, token replay, cross-tenant routing, webhook
spoofing, delivery races, preference/disconnect races, secret/diagnostic leakage,
source-authority drift, execution coupling, and deployment collision. Release bar:
**0 HIGH / 0 MEDIUM** after remediation. The review found and fixed three MEDIUM
issues before this verdict: admin mutation paths around identity/outbox invariants,
app-language changes not immediately persisting the notification language, and a
fixed-oldest-slice reconciler that could starve later durable events. The reconciler
now advances a transactional per-source high-water cursor and rolls cursor progress
back with a failed projection batch. The release hardening also made HTTP 429
retryable, made configuration/worker readiness explicit, expanded health metrics,
added fail-closed customer/global routing regression proof, and corrected aggregate
multi-leg outcomes to derive WIN/LOSS/BREAKEVEN from summed realised PnL rather than
the latest leg alone.
Any later finding above LOW blocks activation.

## 20. Collision report

The stream began at `b9763a2e4d9b6cefc2a56f97ab4e8b2089ee35f6`; draft PR #371 was based on
`c21bfb3` and was rebased cleanly onto current main
`de99004c8d42d2181f1492a8813e6d65ee649f8c` on 2026-08-19. PR #367 is merged.
Subsequent main work (#373–#375) added per-tenant execution transport,
provisioning integration, and launcher follow-up. Exact path overlap is limited to
`frontend/src/app/(app)/layout.tsx` and `frontend/src/lib/i18n.ts`; both are additive
and the newer main behavior is preserved. Open PRs #343 and #304 are documentation-only.
No execution-plane path has entered #371, and no other stream's work was dropped.

The only protected-area seam is two post-transition `core.audit.log_event` calls
after signal-copy enable/disable saves. They do not alter assignment, routing,
claiming, sizing, authorization, broker, bridge, node, MT5, or order behavior.

## 21. Exact production requirements

Programme Director approval must precede all of these changes:

1. approve/create a dedicated GuvFX customer notification bot;
2. provision the six customer-plane environment settings through secret/configuration
   management, leaving provider/stakeholder credentials untouched;
3. take a database backup, deploy reviewed code, and apply migration `0001`;
4. register the dedicated webhook URL with Telegram and its secret header;
5. add the prepared dedicated worker service to the production compose stack;
6. deploy the frontend with the approved production API base;
7. verify webhook rejection/acceptance with a non-customer test identity;
8. verify queue health, one EN and one JA test delivery, rate limit, disconnect,
   dedupe, and worker-failure isolation;
9. monitor queue age/failures and retain the feature flag as the immediate
   rollback control.
10. pilot only with `beta.guvfx01@gmail.com` (or another disposable acceptance
    customer): connect, confirm the private binding, use an existing durable safe
    event, disconnect, and prove suppression. Do not manufacture a trade.

## 22. Bot/token/webhook decisions still required

- approved customer bot display name and `@username`
- bot owner and rotation/revocation procedure
- secret-store locations and operators allowed to read/rotate them
- final public webhook hostname/path and Telegram secret-token value
- alert thresholds/on-call destination for failed and stale queue rows
- explicit pilot accounts and EN/JA acceptance identities
- retention policy for binding metadata, outbox, and immutable attempts

The complete configuration contract is:

| Setting | Secret | Default / rule |
|---|---:|---|
| `CUSTOMER_TELEGRAM_NOTIFICATIONS_ENABLED` | no | `false` |
| `CUSTOMER_TELEGRAM_BOT_USERNAME` | no | empty; approved public bot username |
| `CUSTOMER_TELEGRAM_BOT_TOKEN` | yes | empty |
| `CUSTOMER_TELEGRAM_WEBHOOK_SECRET` | yes | empty |
| `CUSTOMER_TELEGRAM_WEBHOOK_URL` | no | empty; valid public HTTPS URL required |
| `CUSTOMER_TELEGRAM_WORKER_ENABLED` | no | `false` |

The existing bounded token TTL and retry-attempt settings remain non-secret operational
controls. No real credential or production endpoint is present in Git, tests, docs,
screenshots, or image layers.

## 23. Deferred items

Provider/channel management, signal-ingestion changes, marketing broadcasts,
support chat, all Telegram trading/strategy commands, manual orders, IB routing,
team/household recipients, custom templates, and operator retry of ambiguous
`PROCESSING` rows are out of scope.

## 24. Final readiness verdict

**CUSTOMER_TELEGRAM_CODE_READY; PRODUCTION BLOCKED ON CONFIGURATION.**
The code and deployment preparation remain DARK. Production readiness requires
the decisions and controlled rollout in sections 21–22 plus exact-SHA green CI.

**STOP:** no production bot activation, webhook activation, customer notification
deployment, or trading/execution mutation is authorized by this packet.

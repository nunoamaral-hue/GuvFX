# Broker Intelligence — Multi-Account & Public-Metrics Platform (Design)

**Packet:** GUVFX_BROKER_INTELLIGENCE_MULTI_ACCOUNT_PUBLIC_METRICS (P0) · **Baseline main:** `4fcfaa5` ·
**Date:** 2026-10-18 · **Status:** Design of record for the DARK foundation. MT5/execution architecture frozen; this
revises the Broker Intelligence *product* model per explicit Sponsor requirements. Every change is additive, DARK,
reversible; **no migration is applied to production and no public surface is activated under this packet.**

This document is the architecture + migration + privacy methodology + PR decomposition that the programme report
(§K) references. Where a change alters an established pattern it is called out as needing a formal ADR/Notion
decision (this packet does not grant Notion access).

---

## 1. Current state (verified on `4fcfaa5`) and the blockers it creates

| Area | Today | Blocker for the new requirement |
|------|-------|----------------------------------|
| Account↔email | `BrokerEmailIdentity` is **unique `(email, broker_name)`** and carries a single `trading_account` + `connected_mailbox` FK | one `(email, broker)` cannot serve **many** accounts — the Sponsor requires exactly that |
| Mailboxes | `ConnectedMailbox` is `user`-FK with **no** per-user uniqueness; per-mailbox cursor/health/revoke already exist | a **product-level single-live-mailbox rule** (added in #509) now forbids a 2nd live mailbox |
| Attribution | `EvidenceBlob.sha256` over raw bytes; `Withdrawal` partial-unique `(trading_account, broker_reference_id)` | the RFC822 **Message-ID is never captured** and there is **no `ConnectedMailbox` FK** on `BrokerEvent`, so the same email via two inboxes → different sha256 → **double-count**; ref-less requests double-count unconditionally |
| Broker identity | fragmented across 4 unlinked models; only `BrokerProfile.slug` is canonical (but it is **internal** email/auth/parser config) | no single public canonical broker; `Withdrawal` has **no broker FK**; no editorial/stats separation |
| Public surface | DRF default `IsAuthenticated`; all metrics strictly owner-scoped, REAL-only; `(public)` Next.js route group + `AllowAny` precedent (`TelegramWebhookView`) exist | **no cross-member aggregate** and **no public endpoint** exist; both must be built leak-proof |
| Entitlement | owned cap = `max_trading_accounts` (overridable via `EntitlementOverride {"value": N}`, reversible/audited); enforced path is uncapped | legacy add path clamps **`min(10, …)`** (`trading/account_service.py:106`); reaching 20 otherwise requires the `concurrent_accounts_enforcement` grant, which **entangles** hosted-workspace/concurrency semantics |

## 2. Target model (additive, non-destructive)

Three identities stay distinct (as the existing code comments require): **ConnectedMailbox** (an inbox GuvFX reads,
owns the encrypted OAuth token ref), **BrokerEmailIdentity** (an address registered *at a broker*), **BrokerEmailAlias**
(the permanent GuvFX alias — lifecycle preserved, untouched).

**New association (A/C).** Introduce `TradingAccountBrokerContact` — the explicit many-to-many between a GuvFX
trading account and a broker-registered email identity, so one `(email, broker)` serves many accounts **without
duplicating mailbox credentials** (the credential stays on the one `ConnectedMailbox`):

```
TradingAccountBrokerContact
  trading_account        FK trading.TradingAccount      # the GuvFX account
  broker_email_identity  FK BrokerEmailIdentity         # the (email, broker) registry row (credential NOT here)
  connected_mailbox      FK ConnectedMailbox  null=True # the inbox that receives it (if verified); may be shared
  status                 PENDING|VERIFIED|UNRESOLVED|RETIRED   # per-binding verification (mailbox control alone != proof)
  verified_at, created_at, updated_at
  UNIQUE(trading_account, broker_email_identity)        # one binding per (account, email@broker)
  + append-only TradingAccountBrokerContactAudit (who/what/proofs/outcome)
```

- `BrokerEmailIdentity` is **relaxed in meaning** to "a `(email, broker)` that receives broker mail" — its existing
  `(email, broker_name)` unique stays (one registry row per email@broker), and its legacy `trading_account`/
  `connected_mailbox` FKs are **kept, not destroyed** (they are the primary/first binding; a DARK backfill command
  can mirror them into `TradingAccountBrokerContact` without deleting anything). Many accounts now attach via the new
  binding table. `EMAIL 1 → MANY ACCOUNTS` and `BROKER 1 → MANY ACCOUNTS` are expressed by multiple bindings.
- `USER 1 → MANY MAILBOXES` already holds; `ONE INBOX → MANY ACCOUNTS/BROKERS` is expressed by multiple bindings
  pointing at one `connected_mailbox`.
- **Disconnect retains history:** a disconnected account/mailbox sets a binding to `RETIRED` (never deletes evidence
  or the binding); withdrawals remain owner-scoped history.

**Attribution/dedup foundation (D).** Additive fields, never destructive:
- `BrokerEvent.message_id` (RFC822 Message-ID, captured in `parse_rfc822`) — the identifier that is **stable across
  inboxes** for the same logical email → the cross-inbox dedup key.
- `BrokerEvent.connected_mailbox` FK (null) — per-mailbox provenance (which inbox observed it).
- Dedup rule: the same `message_id` observed via two inboxes resolves to **one** logical observation; a withdrawal
  reference seen under **two different accounts/owners** → `UNRESOLVED` (never guessed). Reuse the existing
  `(trading_account, broker_reference_id)` primitive and the monotonic AMBIGUOUS/UNRESOLVED state machine.

**Broker canonical identity + public metrics (E/F).** A new **public** layer, separate from internal config:
```
PublicBroker          slug (unique, from BrokerProfile.slug), display_name, editorial fields, is_published  # public identity
BrokerMetricCategory  key (WITHDRAWAL_PROCESSING, …), version, methodology_version, status DRAFT|ACTIVE|RETIRED  # reuse the
                      # broker_catalogue.CatalogueVersion exactly-one-ACTIVE / immutable-once-not-DRAFT pattern
BrokerMetricAggregate public_broker FK, category FK, window_start/end, stats JSON, sample_size, contributor_count,
                      methodology_version, data_freshness_at, status, computed_at   # the PUBLISHED, privacy-safe aggregate
```
`Withdrawal`/`BrokerEvent` gain a nullable canonical-broker linkage (via `PublicBroker` slug derived from the
account's broker) so per-broker aggregation is possible without the 4-model string reconciliation.

## 3. Multi-mailbox policy (B) — replace the single-live rule, keep every safeguard

The #509 single-live-mailbox *product rule* is replaced by a **certified many-mailbox policy**. Removed: only the
"≤1 live mailbox" guard. **Preserved:** OAuth ownership validation; `gmail.readonly`-only; encrypted credential
storage; signed authorization state; explicit `ADD_NEW_MAILBOX` vs `RECONNECT_EXISTING_MAILBOX`; targeted
same-mailbox reconnect (expect-bound); independent per-mailbox cursors; per-mailbox health + revocation; cross-user
mailbox-ownership protection (global `(provider, provider_mailbox_id)` unique + clash 409); **per-owner concurrency
serialization** (the `pg_advisory_xact_lock` stays — it now guards "no duplicate of the *same* mailbox and a
consistent add" rather than "≤1 live"). `ADD_NEW_MAILBOX` becomes allowed when a live mailbox already exists; a
reconnect still targets exactly the named owned mailbox. **General customer onboarding is NOT auto-enabled** — the
policy ships DARK behind its flag.

## 4. Public aggregation — privacy & publication methodology (F/H)

- **Source:** exclusively **certified genuine** external-withdrawal evidence (`provenance=REAL` + the broker in
  `CERTIFIED_BROKERS`, today empty ⇒ everything is "insufficient data"). Synthetic/sanitised are excluded (the §507
  genuine-only rule extends to the public projection).
- **Never exposed:** customer identity, email, trading-account number, withdrawal reference, any individual
  transaction, raw notification, or evidence storage reference. The public payload is **aggregate-only**.
- **Thresholds (documented minimums):** an aggregate publishes only when `contributor_count ≥ MIN_CONTRIBUTORS`
  (distinct owners) **and** `sample_size ≥ MIN_SAMPLES`; below either → `INSUFFICIENT_DATA` (no stats). A single
  user or duplicated messages cannot dominate: per-contributor contributions are capped/winsorised and cross-inbox
  duplicates are collapsed (D) before aggregation.
- **Lifecycle states surfaced truthfully:** `REQUESTED`, `PROCESSING`, `BROKER_REPORTED_COMPLETED`,
  `RECEIPT_CONFIRMED_IF_EVER_AVAILABLE`, `UNRESOLVED`. **A broker-processing notification never claims funds were
  received**; pending transactions are never counted as completed processing-time observations.
- **Windows + freshness + methodology version** accompany every aggregate; aggregates refresh on eligible evidence
  change. **No individual withdrawal is ever published as a public event. No broker rating** without an approved
  scoring methodology (out of scope here).
- **Consent/participation:** private financial evidence is **not** automatically public. A documented, approved
  consent/participation policy is a **prerequisite** to using any customer observation in public metrics — a
  governance dependency, flagged, not satisfied here.

## 5. Entitlement (I) — 20 owned accounts via the existing engine

Use the existing per-user, reversible, audited `EntitlementOverride {"capability":"max_trading_accounts",
"value":20}`. The only blocker is the legacy `min(10, …)` ceiling at `trading/account_service.py:106` (and the
report-only clamp at `admin_ops/views.py:752`). Fix: make the ceiling a **configurable product maximum**
(`GUVFX_MAX_OWNED_ACCOUNTS_CEILING`, default 20) so a per-user 20-override is honoured **without** the
`concurrent_accounts_enforcement` entanglement. This changes **no customer whose entitlement is ≤10** (plans cap at
10; the clamp only ever bit an explicit >10 override), grants **no** trading/live-execution authority (owned cap is
already decoupled from `can_deploy_automation`; `account_mode` stays `standard`), and is reversible (lower the
setting). Amber (touches the trading add-account path) — authorised by this Sponsor packet; flagged in handoff.

## 6. Migration plan (additive, reversible, DARK — NOT applied to prod)

Prod `broker_intelligence` schema is **0010** (0011 health columns unapplied). All new tables/columns are additive
and reversible; **none is applied to production under this packet**. Planned DARK migrations:
1. `TradingAccountBrokerContact` + its audit (new tables).
2. `BrokerEvent.message_id`, `BrokerEvent.connected_mailbox` (nullable columns).
3. Public layer: `PublicBroker`, `BrokerMetricCategory`, `BrokerMetricAggregate` (new tables) + nullable canonical
   broker linkage on `Withdrawal`/`BrokerEvent`.
Backfills (legacy `BrokerEmailIdentity` binding → `TradingAccountBrokerContact`) are **DARK management commands**,
never auto-run. Each migration is independently reversible; the existing prod-0010/main-0011 gap is respected (new
migrations are numbered above the current head and stay unapplied).

## 7. PR decomposition & status

| PR | Scope | This session |
|----|-------|--------------|
| **P1** | This design doc (ADR) + programme report | **built** |
| **P2** | Entitlement: configurable owned-account ceiling → 20 (I) | **built + tested DARK** |
| **P3** | `TradingAccountBrokerContact` association + `BrokerEvent.message_id`/`connected_mailbox` + DARK migration + Message-ID capture (A/C/D foundation) | **built + tested DARK** |
| **P4** | Many-mailbox policy replacing single-live (B) | designed; next |
| **P5** | Public layer models + leak-proof aggregation + DARK `/api/public/brokers` (E/F) | designed; next |
| **P6** | Customer UI (account-level email mgmt) + public directory UI (C/G) | designed |
| **P7** | Governance/consent policy + scoring-methodology gate (H) | designed |

## 8. Dependencies & forecast

- **Genuine-data dependency (unchanged):** no public WITHDRAWAL_PROCESSING metric can leave `INSUFFICIENT_DATA`
  until certified genuine external-withdrawal evidence exists (Sponsor-initiated withdrawals; `CERTIFIED_BROKERS`
  empty today).
- **Governance dependency:** an approved consent/participation policy + an approved public scoring methodology are
  required before public metrics activate; formal ADR/Notion decisions for the model change and the public surface.
- **Production dependency:** all migrations stay unapplied; activation is a separate gated deployment (and the prod
  0010→0011 gap must be resolved in that deployment, not here).
- **Forecast:** the multi-account foundation (P2+P3) is the critical path and lands this session DARK; the public
  platform (P4–P7) is a bounded sequence of further DARK PRs, gated at the end on genuine data + governance.

*No production deployment, no migration applied, no public metrics activated, no execution change, no entitlement
granted to any user (the override is an admin action), no financial action.*

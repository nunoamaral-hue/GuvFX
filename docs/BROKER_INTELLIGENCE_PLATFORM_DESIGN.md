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

**Association safeguards — ownership, consistency, deletion, audit integrity (A/C) [added per PD review of #513].**
Mailbox control is *evidence*, not *proof* of ownership; the binding must never let one customer's inbox claim
another customer's account or broker identity. The following are invariants of the association layer, enforced in a
fail-closed verified-state service and documented where the schema alone cannot carry them:

- **Authoritative ownership is the trading account's owner — never the email address.** A binding's owner is
  `trading_account.user`. The broker email address is *attribute data on an identity*, shared by design across many
  accounts and even many customers (the same `jane@example.com` may be the registered contact at a broker for two
  unrelated GuvFX users), so possession of mail at an address NEVER establishes who owns a GuvFX account. A verified
  binding therefore proves "this account's owner controls this inbox and this inbox receives this (email, broker)
  identity's mail" — it never infers account ownership from the address.
- **Owner/mailbox consistency invariant (fail-closed before VERIFIED).** A binding may transition to `VERIFIED` only
  when `connected_mailbox` is non-null AND `connected_mailbox.user_id == trading_account.user_id`. A mailbox owned by
  a different user, a null mailbox, or a mailbox whose ownership later diverges forces the binding back out of
  `VERIFIED` (to `UNRESOLVED`) rather than silently trusting it. This is the structural block on cross-user email
  claims: user B connecting an inbox that happens to receive user A's broker mail can never verify a binding on user
  A's account, because the owners do not match.
- **Identity/broker consistency invariant.** The binding's `broker_email_identity` carries the canonical broker; a
  binding whose identity disagrees with the trading account's broker context is `UNRESOLVED`, not `VERIFIED`. Broker
  identity is taken from the authoritative catalogue slug (as in §4 aggregation), never from display text or a
  free-form address domain.
- **Verification is revocable and non-latching.** `verified_at` records when consistency last held; it is not a
  permanent grant. Re-evaluation on mailbox disconnect, re-consent revocation, or ownership change can demote a
  binding. A stale/revoked mailbox credential demotes every binding that depended on it. (No binding state is ever a
  trading or execution authority.)
- **Deletion policy parity — no silent history loss.** The binding's `trading_account` FK is **history-safe**: a
  trading account is not hard-deleted out from under withdrawal/event history, so the FK is `PROTECT` (or an
  equivalent soft-retire that preserves the binding and its evidence), matching `broker_email_identity` (`PROTECT`).
  `connected_mailbox` is `SET_NULL` (an inbox may be disconnected while the binding is retained as `RETIRED` with its
  history). Destroying a customer's withdrawal history as a side-effect of an account row deletion is forbidden; the
  raw-evidence-is-immutable and quarantine-not-destroy data rules bind here.
- **Audit integrity is DB-enforced, not just application-asserted.** `TradingAccountBrokerContactAudit` is append-only
  and immutable at the database layer (an `UPDATE`/`DELETE` trigger refusing mutation, the same mechanism already used
  for `EvidenceBlob`/`BrokerEvent`), so a documentation claim of "append-only audit" is backed by an enforced
  guarantee rather than an application-layer convention a bulk operation could bypass. Any application-layer-only
  guard that remains (e.g. a bulk/raw-SQL path that the ORM cannot intercept) is documented explicitly as a known
  bypass limit — never described as if the trigger covered it.
- **Bulk / raw bypass is stated, never implied away.** `bulk_create`/`update()`/raw SQL do not run model `save()` or
  signal hooks; any invariant enforced only there is explicitly labelled as not covering those paths, and the
  DB-trigger immutability (above) is what actually holds for the audit trail. (Evidence rule: silence is not
  coverage.)

**Attribution/dedup — corrected three-layer model (D) [revised per PD review of #511].** The RFC822 Message-ID is
**NOT** a trusted cross-inbox transaction or universal delivery key: it is sender-controlled, may be absent, reused
or forged; one real withdrawal can carry MANY distinct Message-IDs, and one reused Message-ID must never suppress
separate genuine observations. **Raw eligible broker evidence is NEVER dropped on a Message-ID.** Three distinct
layers, de-duplicated independently:

1. **Delivery** (an email received in an inbox). De-duplicated by the TRUSTED mailbox-native identity
   `(provider, connected_mailbox, provider_message_id)` — the provider's own stable id for a message in that inbox,
   not the sender-controlled RFC822 header. Re-fetching the same message from the same inbox is one delivery; the
   SAME logical email arriving in TWO inboxes is TWO distinct authenticated deliveries, each preserved with its own
   per-mailbox provenance. (Message-ID may be stored as non-authoritative metadata only, versioned-canonicalised, but
   is never a suppression key.)
2. **Broker event** (a parsed observation from a delivery's immutable evidence). One per delivery; evidence is
   content-addressed and immutable. **Cross-owner safety:** the existing global `EvidenceBlob.sha256` +
   `BrokerEvent(evidence)` uniqueness + the early existing-event return means byte-identical bytes reaching two
   users/mailboxes would hand the SECOND user the FIRST user's event — a cross-owner provenance leak / silent drop.
   The idempotency return is therefore made **owner-aware** (an existing event is reused only for the SAME owner);
   delivery provenance is modelled per mailbox/user so no second customer ever inherits another's event, while the
   immutable evidence blob is still shared/de-duplicated. Account for `BrokerEvent` immutability (app + DB trigger).
3. **Financial withdrawal** (the correlated transaction). De-duplicated at the TRANSACTION layer by
   `owner + canonical broker + trading account + broker reference/lifecycle` — the existing
   `(trading_account, broker_reference_id)` partial-unique + the monotonic AMBIGUOUS/UNRESOLVED correlation state
   machine. Duplicate deliveries of the same withdrawal collapse here (same owner+account+reference → one
   withdrawal); anything ambiguous → `UNRESOLVED`, never guessed. This preserves the distinction delivered-email ≠
   broker-event ≠ withdrawal: duplicate deliveries do not inflate statistics, and unrelated authentic evidence is
   never discarded.

Required tests (negative): same Message-ID with different content/account/broker/owner (must NOT collapse); distinct
Message-IDs for the same withdrawal (must collapse at the transaction layer); missing Message-ID (falls back to
delivery id + reference correlation); shared raw bytes across two owners (each keeps its own event). **The
Message-ID-based event suppression merged in #514 is superseded by this model and is corrected in a follow-up PR; it
is DARK (flag OFF) and was never active.**

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
- **k-anonymity-like suppression + dominance + differencing [added per PD review of #511 §4].** Beyond the
  MIN_CONTRIBUTORS / MIN_SAMPLES floors: a published cell is suppressed unless it reflects at least *k* independent
  contributors (k-anonymity), each contributor's weight is bounded against the FINAL sample (water-filling
  winsorisation — no one member or duplicate dominates), and **differencing across rolling windows is prevented**
  (two near-identical windows must not let an observer subtract them to isolate one contributor — windows change
  only when the contributor set changes enough that a difference cannot single anyone out, and small deltas are
  suppressed). **Revocation:** a contributor who withdraws consent is removed and the affected aggregates recomputed.
  Empty `CERTIFIED_BROKERS` ⇒ **never publish an observed speed** (production default).
- **Three-way completion distinction [per PD].** Public metrics must distinguish (a) **total GuvFX-observed
  completion** (what GuvFX saw across inboxes), (b) **broker-reported completion** (the broker said "completed" — the
  only thing a processing notification evidences), and (c) **actual funds receipt** (confirmed money arrival — no
  signal exists today, so always 0 / "if ever available"). The public surface never conflates (b) with (c).
- **Consent/participation [emphasised per PD].** Private financial evidence is **not** automatically public. A
  documented, approved **participation/consent policy** (opt-in, revocable) is a hard **prerequisite** to using ANY
  customer observation in public metrics — a governance gate, not satisfied here. Pages ship DARK; no public
  financial samples are shown before the consent + methodology + threshold gates are all met.

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
and reversible; **none is applied to production under this packet**. Migrations landed by the foundation programme
(DARK, unapplied to prod):
1. `broker_intelligence` **0012** — `TradingAccountBrokerContact` + `TradingAccountBrokerContactAudit` (new tables).
2. `broker_intelligence` **0013** — `BrokerMessageObservation` (new **separate** table, so `BrokerEvent` writes stay
   byte-identical and unmigrated-safe) + captured RFC822 `Message-ID`. *(Note: Message-ID is retained as
   non-authoritative metadata only — see §Attribution; it is NOT a suppression key.)*
3. `broker_public` **0001** — `PublicBroker`, `BrokerMetricCategory`, `BrokerMetricAggregate` (new app, own sequence).

Correction-packet migrations (additive, DARK, numbered above the current head, **not applied to prod**):
4. **C3** — `TradingAccountBrokerContact.trading_account` FK `CASCADE→PROTECT` (history-safe) + DB-trigger append-only
   immutability on `TradingAccountBrokerContactAudit` (same trigger mechanism as `EvidenceBlob`/`BrokerEvent`).
5. **C2** — delivery-identity fields supporting dedup keyed on `(provider, connected_mailbox, provider_message_id)`
   (on the observation/delivery layer, never altering `BrokerEvent`'s immutable append-only shape), as required by the
   corrected three-layer model.

Backfills (legacy `BrokerEmailIdentity` binding → `TradingAccountBrokerContact`) are **DARK management commands**,
never auto-run. Each migration is independently reversible; the existing prod-0010/main-0011 gap is respected (new
migrations are numbered above the current head and stay unapplied; the gap is resolved migration-first in a later
gated deployment, not here).

## 7. PR decomposition & status

**Foundation programme (merged DARK, baseline `4fcfaa5` → `54647ca`):**

| PR | Scope | Status |
|----|-------|--------|
| #511 | This design doc (ADR) + programme report | merged `838cc87` |
| #512 | Entitlement: configurable owned-account ceiling → 20 (I) | merged `80b6170` (tested DARK) |
| #513 | `TradingAccountBrokerContact` association + audit (A/C) | merged `32d2787` (tested DARK) |
| #514 | Cross-inbox dedup: `BrokerMessageObservation` + Message-ID capture (D) | merged `025557a` (tested DARK) |
| #515 | `broker_public` app: directory + versioned metrics + leak-proof aggregation (E/F) | merged `54647ca` (tested DARK) |

**PD foundation review — correction packet (this session).** The PD review of #511/#513 revised the attribution
architecture and tightened the association safeguards. The correction sequence is ordered so the **design is accepted
before the corrected implementation lands** — the Message-ID-based *suppression* is not implemented until this
corrected design (the three-layer model in §Attribution and the §A/C safeguards above) is accepted:

| PR | Scope | Order / gate |
|----|-------|--------------|
| **C1** | This design correction (three-layer delivery/event/withdrawal model; §A/C ownership-consistency-deletion-audit safeguards; public-aggregation differencing/dominance/consent additions) | **first — design must be accepted before C2** |
| **C2** | #514 re-architecture: delivery-dedup keyed on `(provider, connected_mailbox, provider_message_id)`; **owner-aware** existing-event idempotency (no cross-owner inheritance); Message-ID demoted to non-authoritative metadata (never a suppression key); never drop raw eligible evidence; reference-id transaction correlation; negative tests | after C1 accepted |
| **C3** | #513 safeguards: `trading_account` FK CASCADE→PROTECT/history-safe; DB-trigger audit immutability; fail-closed owner/broker/mailbox consistency service before `VERIFIED`; corrected docs; tests | after/with C2 (new additive DARK migration) |
| **C4** | Frontend DARK scaffolding: public directory UI + customer account-level broker-email management (C/G), en/ja, parity-registered, where dependencies permit | independent of C2/C3; built where deps allow |
| **P-next** | Many-mailbox policy (B); governance/consent policy + scoring-methodology gate (H) | designed; gated on genuine data + governance |

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

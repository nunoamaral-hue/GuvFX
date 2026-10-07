# Broker Email Identity — Design (read-only, NOT implemented)

**Status:** DESIGN ONLY (Sponsor-directed, 2026-10-07). No code, no migration, no mailbox ingestion.
**Refines:** `docs/STREAM_D_LIVE_MT5_AND_BROKER_INTELLIGENCE_DESIGN.md` §7 (broker-email alias) — this document is a
focused, decision-ready refinement of that Sponsor-reviewed section; it does **not** supersede §8–§12 (BrokerEvent
/ Withdrawal / ingestion / metrics), which remain the authoritative downstream roadmap and stay out of scope here.
**Scope boundary:** this is the IDENTITY/alias foundation only. Everything downstream — BrokerEvent, Withdrawal,
mailbox ingestion, Withdrawal Intelligence, public WAYOND scoring — is explicitly **out of scope** (see §7 below).

Grounded in a read-only exploration of the current tree (file:line references throughout). The goal is to let the
Sponsor approve a small, additive, DARK first increment and to pin the genuine decisions before any code.

---

## 1. Purpose and the two onboarding journeys

Provision **one opaque, never-reused email alias per Model-A BrokerAccount lifecycle instance**, of the form
`<opaque>@accounts.guvfx.com`, that can later receive a broker's transactional mail (withdrawals, etc.) without
exposing the customer's real email or the platform's account identifiers. This stream builds the alias identity
only; a **separate, isolated** ingestion stream (parent §9) will later read mail.

Two journeys the design must admit (Sponsor directive):

- **(A) Connect an existing broker account.** The customer already holds an MT5 account (registered with their own
  email). GuvFX mints an alias and binds it to that lifecycle instance. The alias is a passive correlation tag;
  journey A has **no inbound-mail dependency**.
- **(B) Open a new broker account.** GuvFX mints the alias **before** the customer registers at the brokerage; the
  customer registers at the broker **using the alias** as their email; the resulting MT5 account is later bound
  back to the same lifecycle instance. Journey B **requires** that `*@accounts.guvfx.com` can actually receive mail
  (MX + mailbox), so it is gated separately and stays dark until that infrastructure exists.

---

## 2. The lifecycle anchor (why "per instance, never reused" is free)

There is **no separate `BrokerAccount` model** — the lifecycle instance *is* `trading.TradingAccount`
([backend/trading/models.py:45](backend/trading/models.py:45)). Model-A (shipped) guarantees:

- A re-add of the same broker identity creates a **brand-new `TradingAccount` row (new pk)** — revive-in-place was
  removed ([backend/trading/account_service.py:31](backend/trading/account_service.py:31);
  [backend/trading/views.py:213](backend/trading/views.py:213) filters `disconnected_at__isnull=True`).
- A tombstone is **retained, never deleted** (`disconnected_at`,
  [backend/trading/models.py:142](backend/trading/models.py:142)); partial-unique-on-active constraints
  ([backend/trading/models.py:179](backend/trading/models.py:179)) let a tombstone and its re-add coexist.

Therefore, keying the alias **1:1 to the `TradingAccount` instance row** makes "one alias per instance, never
reused across instances" a **database invariant for free**: a new instance has no alias and mints a fresh one; a
tombstoned instance keeps its (retired) alias forever; the re-added instance (new pk) can never inherit it.

> **Do NOT** key the alias to anything that survives re-add (`user`+`login`+`server`, broker name, etc.) — that
> would let a re-added instance inherit a prior instance's alias and cross-link its broker correspondence (a
> data-isolation breach).

---

## 3. Data model — `BrokerEmailAlias`

A new model sibling to the account instance, mirroring the shape of the existing per-instance siblings
`AccountProvisioning` / `AccountRuntime` ([backend/terminal_provisioning/models.py:31,173](backend/terminal_provisioning/models.py:31))
and `HostedMt5Workspace` ([backend/hosted_workspace/models.py:63](backend/hosted_workspace/models.py:63)), and the
immutability ethos of `LiveExecutionAuthorization` (execution migration 0034).

| Field | Type | Notes |
|-------|------|-------|
| `alias_local` | `CharField(unique=True, editable=False)` | The **opaque local-part only**. UNCONDITIONAL (all-rows) unique — see §4. |
| `trading_account` | `OneToOneField(trading.TradingAccount, on_delete=PROTECT, null=True)` | **Nullable** to admit journey B's pre-account window. `PROTECT` preserves never-reuse against a (forbidden) hard delete. |
| `user` | `ForeignKey(AUTH_USER_MODEL, on_delete=CASCADE)` | Owner — set at mint so a journey-B alias has an owner **before** any account exists. |
| `origin_journey` | `CharField(choices=CONNECT_EXISTING \| OPEN_NEW)` | Captured at mint; drives the read-model (§5). Legacy/default = `CONNECT_EXISTING`. |
| `status` | `CharField(choices=PENDING \| ACTIVE \| RETIRED, default=PENDING)` | `PENDING` = journey-B minted-unbound; `ACTIVE` = bound to a live instance; `RETIRED` = tombstoned/abandoned (terminal). |
| `domain_at_creation` | `CharField(editable=False)` | Immutable snapshot of the alias domain at mint (see §4) so a later domain change never orphans a registration. |
| `created_at` / `bound_at` / `retired_at` | `DateTimeField` | `bound_at`/`retired_at` nullable until the transition. |

**Constraints**
- `UniqueConstraint(fields=['alias_local'])` — **unconditional / all-rows** (deliberately NOT the partial-on-active
  shape used for accounts): a RETIRED token permanently reserves its value, so it can never be recycled.
- The `OneToOneField` gives "at most one alias per bound instance" for free. (Postgres treats multiple NULL
  OneToOne values as distinct, so many `PENDING` unbound aliases per user coexist during journey B; the
  `alias_local` uniqueness — not the account FK — carries non-reuse in that window.)

**Immutability (model-layer, binds the DRF full-save path)** — copy the `AccountRuntime._IMMUTABLE_BINDING`
guard ([backend/terminal_provisioning/models.py:202](backend/terminal_provisioning/models.py:202)) and
`TradingAccount`'s write-once hosted-identity guard ([backend/trading/models.py:215](backend/trading/models.py:215)):
`alias_local` and `domain_at_creation` are immutable always; `trading_account` is **write-once** (the single
`NULL → account` journey-B bind is allowed exactly once; a bound alias can never be re-pointed). `status` is a
monotonic lifecycle (`PENDING → ACTIVE → RETIRED`); `RETIRED` is terminal and never flips back.

---

## 4. Opacity, uniqueness, domain (security)

- **Opaque local-part:** a CSPRNG token of ≥128 bits from an email-safe lowercase charset (e.g.
  `secrets.token_hex(16)` → 32 hex chars, or base32), reusing the house opaque-token idiom
  ([backend/terminal_provisioning/services.py:45](backend/terminal_provisioning/services.py:45);
  [backend/onboarding/models.py:94](backend/onboarding/models.py:94)). It **must embed no PII and nothing
  derivable**: not the sequential `TradingAccount` pk, not the user id/email/name, not the broker. (Counter-example
  to avoid: `guvfx_u_<id>` at [backend/terminal_provisioning/services.py:32](backend/terminal_provisioning/services.py:32)
  — fine for an ACL-fenced host identity, wrong for a public email alias because it is enumerable.) An optional
  fixed non-identifying prefix (`ba-`, per parent §7) is allowed; a per-account/sequence prefix is not.
- **Uniqueness spans retired rows** (§3). On the astronomically rare collision, regenerate with a bounded retry
  budget; if exhausted, **fail closed** — never fall back to a derived/sequential value.
- **Domain is configuration, not a constant** (data rule): add `BROKER_EMAIL_ALIAS_DOMAIN` via `env()` alongside
  the existing email config ([backend/guvfx_backend/settings.py:333](backend/guvfx_backend/settings.py:333)),
  default empty ⇒ fail closed. Snapshot it into `domain_at_creation` at mint; assemble the full address with a pure
  helper `alias_address(alias)` → `f"{alias.alias_local}@{alias.domain_at_creation}"`. No I/O, no send, no DNS in
  this stream.
- **Classification: PII-adjacent identifier, NOT a secret.** Do **not** Fernet-encrypt it (that boundary is for
  credentials, [backend/trading/crypto.py:84](backend/trading/crypto.py:84)). But because it de-anonymizes a GuvFX
  instance ↔ a brokerage account, it must be: owner-scoped on every read path; **masked** in logs/audit/evidence
  (the `mask_account` / `_mask_login` convention,
  [backend/analytics/portfolio.py:61](backend/analytics/portfolio.py:61)); never placed in a URL; excluded from
  all public/customer-facing projections (as `account_number` already is,
  [backend/trading/models.py:82](backend/trading/models.py:82)); and fed into the shared-artefact identity scanner
  ([backend/broker_catalogue/sanitiser.py:82](backend/broker_catalogue/sanitiser.py:82)) so it can never ride along
  in a golden image. Mint/retire emit append-only audit ([backend/core/audit.py:131](backend/core/audit.py:131))
  carrying a **masked** alias only.

---

## 5. The two journeys in the existing flow

The only real difference between A and B is the **ordering of alias-mint vs broker-identity-pin**; both reuse the
existing certified seams, so no parallel identity path is introduced.

- **Mint seam (both journeys):** the one canonical create contract
  `create_customer_account` ([backend/trading/account_service.py:38](backend/trading/account_service.py:38)) — the
  row exists at intent (`is_active=False`, `mt5_instance=None`) before `account_number` is final. Add an idempotent,
  savepoint-isolated, flag-gated `ensure_broker_email_alias(account)` modelled **exactly** on
  `_maybe_enqueue_beta_provisioning` ([backend/trading/views.py:167](backend/trading/views.py:167)): own nested
  `transaction.atomic()` savepoint, best-effort try/except that never raises into the create path. For hosted
  onboarding the equivalent seam is `request_hosted_workspace`
  ([backend/hosted_workspace/provisioning.py:195](backend/hosted_workspace/provisioning.py:195)).
- **(A) Connect existing:** identity is known at/just after Add-Account, so mint and identity coexist from creation;
  `status=ACTIVE`, `origin_journey=CONNECT_EXISTING`. No UX change required beyond optionally surfacing the alias.
- **(B) Open new:** mint first with `trading_account=NULL`, `user` set, `status=PENDING`,
  `origin_journey=OPEN_NEW`, reusing the deferred-identity model
  ([backend/hosted_workspace/flags.py](backend/hosted_workspace/flags.py) `hosted_deferred_identity_bind_enabled` +
  the `WAITING_FOR_LOGIN` state). The customer registers at the broker with the alias; the resulting MT5 login is
  bound back through the **existing write-once** `bind_broker_identity`
  ([backend/hosted_workspace/provisioning.py:627](backend/hosted_workspace/provisioning.py:627)), and that same
  transaction binds the alias (`trading_account` set write-once, `PENDING → ACTIVE`, `bound_at` stamped). The alias
  is **never** written to `account_number`/`broker_server` and never consulted by the order-time identity pin.
- **Read-model:** add a projection-only `AWAITING_BROKER_REGISTRATION` phase + `register_at_broker` next-action in
  `onboarding_read_model.py` ([backend/hosted_workspace/onboarding_read_model.py:39,125](backend/hosted_workspace/onboarding_read_model.py:125)),
  derived as `origin==OPEN_NEW AND identity not declared AND not connected`, branched **before** the existing
  `AWAITING_BROKER_LOGIN` case (otherwise a journey-B instance is mis-told "open MT5 and log in" before any broker
  account exists). It is a **projection only**, never a canonical state, so the write-once bind stays legal
  (bind is allowed only while `PROVISIONING`/`WAITING_FOR_LOGIN`,
  [backend/hosted_workspace/provisioning.py:673](backend/hosted_workspace/provisioning.py:673)).
- **D2 account_type** (demo/live) is captured at **request** for journey B (which broker product to open),
  independent of the later login bind — reusing the existing demo/live cross-check at bind
  ([backend/hosted_workspace/provisioning.py:693](backend/hosted_workspace/provisioning.py:693)); D2 is unchanged.

**Retire seam (both journeys):** add `retire_broker_email_alias(account)` modelled on
`destroy_runtime_provisioning_credential` ([backend/trading/account_removal.py:81](backend/trading/account_removal.py:81)),
invoked **inside** `remove_account`'s tombstone transaction next to the existing credential-retire
([backend/trading/account_removal.py:139](backend/trading/account_removal.py:139)): `status=RETIRED`,
`retired_at=now`, row + token retained forever, idempotent, best-effort, never un-tombstones.

---

## 6. Module placement, flags, dependency direction

- **Placement (decision item — ADR-worthy):** a **new dedicated `broker_identity` app** is recommended over
  extending `trading`, so the later deliberately-isolated BrokerEvent/Withdrawal/ingestion domain (parent §8–§9)
  has a narrow module to resolve `To:-alias → account` against **without** importing `trading` execution internals.
  Dependency direction is strictly one-way: `broker_identity → trading` (a nullable FK + read-only reads);
  `trading → broker_identity` **only** through the lazy-imported, best-effort, flag-gated mint/retire hooks (the
  `account_removal._release_beta_runtime_slot` idiom) so a module-load import cycle is impossible and an alias-side
  failure can never block a tombstone or poison an account create. (Establishing a new module boundary is an
  "approved decision" item per the architecture rule — hence this is flagged for the Sponsor, not assumed.)
- **DARK flags** (copy the `hosted_workspace._flag` helper, two-level darkness like D4e):
  - `BROKER_EMAIL_IDENTITY_ENABLED` (master) — OFF ⇒ no alias ever minted; every hook a dormant no-op; existing
    journeys byte-identical; DEMO unaffected.
  - `BROKER_EMAIL_IDENTITY_OPEN_NEW_ENABLED` (independent sub-gate) — gates **journey B specifically**, because
    journey B hands a real brokerage a receivable address. It must stay OFF until MX + a catch-all mailbox for
    `*@accounts.guvfx.com` exist. Journey A (no inbound dependency) can be certified first under the master flag.
- **Migration:** a pure additive `CreateModel` (no backfill in the schema migration); any backfill of pre-existing
  instances is a **separate, idempotent management command** run after the flag is armed — so the schema migration
  stays reversible and the Sponsor decides the backfill policy.

---

## 7. Scope fence — what this stream MUST NOT build

IN SCOPE (first increment): the `BrokerEmailAlias` model + additive migration; opaque minting; the two mint paths +
the write-once journey-B bind; the retire-on-tombstone hook; the config-driven domain + pure address formatter; the
two DARK flags; an owner-scoped read/admin surface; unit tests + a DEMO/flag-off equivalence regression.

OUT OF SCOPE (later governed streams — do **not** add tables, columns, or stubs for them now): `BrokerEvent`,
`Withdrawal`, correlation, metrics, any parser/classifier, the raw-message evidence store, the isolated ingestion
worker + its mailbox credential, MX/DNS provisioning, **any** inbound or outbound mail path for the alias, the
Withdrawal/Broker-Intelligence UX, and the WAYOND public scoring formula. `BrokerEmailAlias` carries **no forward
FK** to event/withdrawal tables (those will FK *to* the alias later), so the next streams add cleanly with no schema
conflict. (Parent §9 keeps ingestion a standalone least-privilege service that owns the mailbox credential — never
Django request code, never the trading workers/MT5 bridge.)

---

## 8. Risks (carried from the exploration)

1. **Journey-B dead-letter:** giving a broker an alias before MX + mailbox exist means the broker's verification
   email bounces and the customer is stuck. ⇒ journey B stays behind its own OFF sub-gate until infra lands;
   journey A ships first.
2. **Enumeration / PII leak:** a derivable or short local-part leaks the account↔user map and the account count.
   ⇒ ≥128-bit CSPRNG, no PII, masked everywhere.
3. **Wrong uniqueness shape:** reusing the account's partial-unique-on-active for the alias would let a retired
   token be re-minted. ⇒ unconditional all-rows unique on `alias_local`; `RETIRED` terminal.
4. **`on_delete` choice:** `PROTECT` (retention-safe) vs `CASCADE`. The never-reuse invariant argues `PROTECT` since
   accounts are tombstoned-not-deleted — a deliberate decision, not inherited by copy-paste.
5. **Immutability must bind the serializer path:** the write-once/opacity guard must live in model `save()` (like
   `AccountRuntime`/`TradingAccount`), not only a service seam, or a full DRF save / admin edit could rebind/rotate.
6. **Transaction coupling:** mint must use the savepoint envelope (never poison the account INSERT); retire must sit
   inside the tombstone atomic block (no ACTIVE alias on a tombstoned instance, or vice-versa).
7. **Name collision:** `delivery.py` already has `remoteapp_alias` / `guvfx_mt5_<id>`
   ([backend/hosted_workspace/delivery.py:44](backend/hosted_workspace/delivery.py:44)). Use a distinct name
   (`BrokerEmailAlias` / `broker_email_identity`) to avoid conflating the two "alias" senses.
8. **Orphan PENDING aliases (journey B abandoned):** must be **retired (token retained), never hard-deleted**, or a
   naive cleanup reopens reuse. Needs a retention/TTL policy (design decision below).

---

## 9. Decisions the Sponsor must confirm before any code

(Several echo parent §new-decisions; re-stated with the Journey-B refinements.)

1. **Journey-B nullable-FK refinement:** approve refining parent §7 from a non-null FK "minted at account creation"
   to a **nullable FK + `PENDING` unbound state + `origin_journey`**, which is what makes journey B expressible.
   (ADR-worthy: it is a deliberate divergence from the approved §7 wording.)
2. **Mint scope:** one alias per **every** Model-A instance (DEMO + LIVE), or **LIVE-only**? (Withdrawal
   Intelligence targets real brokers; demo brokers send no real financial mail — LIVE-only avoids noise/PII, but
   "one per instance" implies universal. Also: should pre-existing instances, incl. Account 43 and tombstones, be
   backfilled, or new-instances-only going forward?)
3. **Domain + token format:** confirm `accounts.guvfx.com` (GuvFX-controlled, distinct from guvfx.com /
   api.guvfx.com / guac.guvfx.com), set via `BROKER_EMAIL_ALIAS_DOMAIN` env; confirm the `ba-<opaque>` prefix
   (parent §7) vs bare `<opaque>`, and the token scheme (128-bit base32 vs `token_hex`). Note any brokerage
   local-part length/charset limits that bound entropy.
4. **Module placement:** new `broker_identity` app (recommended) vs extend `trading` — an ADR-level boundary call.
5. **Journey-B gating + orphan policy:** confirm journey B stays behind `BROKER_EMAIL_IDENTITY_OPEN_NEW_ENABLED`
   until MX + catch-all mailbox exist, and set the retire/TTL policy for an unbound alias whose brokerage
   registration never completes (token still never recycled).
6. **Infra (outside this repo):** MX record + catch-all mailbox for `*@accounts.guvfx.com` — ownership + timeline.
   This is the hard prerequisite gating journey B **and** all later ingestion; it is explicitly **not** built here
   but must be tracked.
7. **Erasure vs never-reuse:** a never-reused, PII-linkable alias collides with right-to-erasure, and per-customer
   crypto-shred is not built ([backend/trading/credential_lifecycle.py:1](backend/trading/credential_lifecycle.py:1)).
   Need a policy for honoring an erasure request without recycling a local-part.
8. **Staff visibility / lifecycle ownership:** may support staff view a tenant's alias (audited, masked)? Per the
   Notion rule, PM owns lifecycle status — confirm who may transition an alias to `RETIRED` outside the automatic
   tombstone hook.

---

## 10. Suggested first governed increment (once decisions land)

A single small DARK PR: the `BrokerEmailAlias` model + additive migration + opaque minting + journey-A mint/retire
hooks + the two flags + owner-scoped read surface + unit tests + DEMO/flag-off equivalence regression. **Journey B,
the read-model phase, and anything inbound stay behind the OPEN_NEW sub-gate (OFF) until the MX/mailbox decision.**
No ingestion, no BrokerEvent/Withdrawal, no real mail — exactly the identity foundation, nothing downstream.

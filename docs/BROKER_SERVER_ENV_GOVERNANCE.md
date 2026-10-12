# Broker Server Environment Governance Correction

**Packet:** Broker Server Environment Governance Correction (Sponsor authorisation 2026-10-12)
**Branch:** `fix/broker-server-environment-governance` (base `main` @ `3419cc7`)
**Status:** corrective PR — **DARK / not deployed**. No production record modified. No account onboarded.
**Decision class:** Amber (touches a shared gate + an established onboarding pattern) — strict posture is
flag-gated DARK; arming is a Sponsor decision.

---

## 1. Root cause (confirmed)

`BrokerServer` carries a single, globally-shared `environment` (`demo`/`live`). The previous hosted-provisioning
path established that value from the **first registering account's** `account_type` via
`get_or_create(server_name=…, defaults={"environment": <from is_demo>})`, defaulting to `demo`.

Consequence — **first-registration environment poisoning**: a genuinely LIVE broker server first touched by a
demo account is pinned `environment=demo` for the whole estate. The existing demo/live mismatch guard
(`REQ_ENV_MISMATCH`) then refuses every later LIVE registration on that server with the opaque message *"That
broker server is already registered with a different (demo/live) environment."*

**Production proof** (read-only query, 2026-10-12 — §4): `IS6Technologies-Live` is recorded `environment=demo`
with **3 demo accounts** bound to it, which is exactly the block that prevents onboarding the real IS6FX LIVE
account `51763830`.

This is **systemic**, not a one-off: four production servers carry an environment that diverges from their own
published name (`IS6Technologies-Live`, `DominionMarkets-Live`, `FortressFX-Trade` all recorded `demo`;
`WIMS-Demo` recorded with non-canonical casing `DEMO`), and a case-insensitive **identity collision** exists
(`TradersWay-Live` vs `Tradersway-Live`).

---

## 2. Architectural summary (Scope A — what the correction does)

The correction makes a **verified catalogue** the authoritative source of a server's environment, removes the
account-derived default, and never trusts a name suffix. It is additive and preserves the existing fail-closed
mismatch guard.

| Component | Change |
|---|---|
| `trading/server_environment.py` (new) | Pure resolver. `resolve_verified_environment(name) -> (env, CATALOGUE)` on a **verified** catalogue match (case-folded `name_key`), else `(None, UNKNOWN)` — the fail-closed signal. `normalise_environment` (canonical `demo`/`live` or `None`), `server_name_key`/`normalise_server_name`, `server_name_collisions()` (read-only case-insensitive collision groups), and `server_name_suffix_hint()` (**advisory only — never consulted by provisioning**). |
| `trading/models.py` | New `BrokerServerCatalogue` (authoritative verified `demo`/`live` per server; `name_key` unique case-insensitively; `save()` normalises name+env, rejects a non-`demo`/`live` env). New `BrokerServer.environment_source` (`catalogue`/`operator`/`inferred`, default `inferred`). |
| `trading/migrations/0020_…` | Schema (CreateModel + AddField) **+ data migration**: normalises existing `BrokerServer.environment` casing (`DEMO`→`demo`) and **idempotently seeds** the verified pilot catalogue. Reverse is a no-op (the `CreateModel` reverse drops the table; lowercasing is lossless). Proven up/down/up (§5). |
| `hosted_workspace/provisioning.py` | Rewrote the `expected_server` block (below). |
| `hosted_workspace/flags.py` | New `broker_server_catalogue_strict_enabled()` — **DEFAULT OFF**. |
| `hosted_workspace/onboarding_views.py` | 409 responses for `REQ_SERVER_UNVERIFIED` and `REQ_SERVER_METADATA_INCONSISTENT` with clear, actionable copy. |
| `trading/admin.py` | `environment_source` surfaced on `BrokerServerAdmin`; `BrokerServerCatalogueAdmin` registered (`name_key` read-only). |

### Provisioning decision table (for a non-empty `expected_server`)

Let `desired = demo if is_demo else live` (the customer's **explicit, required** `account_type`), and
`verified = resolve_verified_environment(server)`.

| Catalogue | Existing `BrokerServer` row | Outcome |
|---|---|---|
| verified | — | `verified != desired` → **`REQ_ENV_MISMATCH`** (fail closed). Else materialise a row **from the verified value** (`source=catalogue`, race-safe `get_or_create`). |
| verified | present, env = verified | reuse, **no mutation**. |
| verified | present, env ≠ verified | **`REQ_SERVER_METADATA_INCONSISTENT`** — row left **untouched**, fail closed (the IS6 poisoning case; surfaced for governed correction, never silently rewritten). |
| unknown | present | legacy mismatch guard: `row.env != desired` → `REQ_ENV_MISMATCH`; else proceed, **no promotion/poisoning**. |
| unknown | absent, **STRICT ON** | **`REQ_SERVER_UNVERIFIED`** (decision #4 strict posture). |
| unknown | absent, **STRICT OFF** (default) | create from `desired`, `source=inferred` (legacy auto-create **minus** the hardcoded-DEMO default; never from suffix). |

### How this satisfies each authorised decision

1. **Env not established solely from the first account** — for a known server the env is the verified catalogue
   value; the account only has to *agree*. ✔
2. **Suffixes are hints, not authoritative** — `server_name_suffix_hint` exists for operator review only and is
   never read by provisioning (test: a demo account on an uncatalogued `…-Live` server is created *demo*). ✔
3. **Verified catalogue is authoritative** — `BrokerServerCatalogue` + resolver. ✔
4. **Unknown fails closed, never default DEMO** — the hardcoded DEMO default is removed; strict mode fails
   closed on genuinely-new servers. *Posture note:* strict is **flag-gated DARK** so the estate's existing
   (un-catalogued) servers keep onboarding until the catalogue is populated via §6; arming strict is the final
   step of the governed correction. ✔ (see Outstanding risk R1)
5. **Existing mismatch protection remains effective** — `REQ_ENV_MISMATCH` preserved on every path. ✔
6. **A registration never mutates shared/verified metadata** — a disagreeing row fails closed; the catalogue is
   never written by onboarding (test asserts `updated_at` unchanged). ✔
7. **Account isolation / uniqueness preserved** — `(login, server)` identity, per-user `select_for_update`,
   `server_name` uniqueness, and cross-user independence all unchanged. ✔

### Deliberate design decision (flagged per handoff rule)

Decision #4 ("unknown fails closed") taken literally against **all** onboarding breaks a third of the existing
onboarding contract (34/99 onboarding tests, measured) and conflicts with the packet's own `preserve compat`
safety boundary — existing demo onboarding uses un-catalogued server names. Resolution: implement the strict
fail-closed posture behind a **DARK, default-OFF** flag (`BROKER_SERVER_CATALOGUE_STRICT_ENABLED`), the standard
GuvFX pattern for a not-yet-safe behaviour change. The **always-on** part of the correction (catalogue
authority, no hardcoded-DEMO default, metadata-inconsistent fail-closed, mismatch guard) already fixes the IS6
defect; strict mode is armed only **after** the production catalogue is populated (§6). This is an Amber
decision recorded here and in the PR; final arming is Sponsor-gated.

---

## 3. Scope B — Production reconciliation report (READ-ONLY, 2026-10-12)

Source: read-only ORM query against prod `guvfx-backend` (no row touched). Account numbers are **redacted**
(shown only as existence + digit-length + owning user id). `environment_source` does not yet exist in prod
(migration unapplied) and was not referenced.

### 3.1 All broker servers (12) and bound accounts (15)

| Server name | Recorded env | Accounts | is_demo split | Catalogue verdict | Classification |
|---|---|---|---|---|---|
| `IS6Technologies-Live` | `demo` | 3 | demo:3 live:0 | **live** (verified) | **INCONSISTENT — poisoned** |
| `IS6Technologies-Demo` | `demo` | 1 | demo:1 | demo (verified) | CONSISTENT |
| `TradersWay-Live` | `live` | 1 | demo:1* | live (verified) | env OK; see collision |
| `Tradersway-Live` | `live` | 2 | demo:1 live:1 | live (verified, same key) | **CASE COLLISION** with above |
| `TradersWay-Demo` | `demo` | 0 | — | demo (verified) | CONSISTENT |
| `WIMS-Demo` | `DEMO` | 1 | demo:1 | not in catalogue | **casing non-canonical → normalise**; env **UNRESOLVED** |
| `DominionMarkets-Live` | `demo` | 1 | demo:1 | not in catalogue | suffix-divergent → **UNRESOLVED** |
| `DominionMarkets` | `demo` | 1 | live:1 | not in catalogue | **UNRESOLVED** (a live account on a demo-recorded server) |
| `FortressFX-Trade` | `demo` | 1 | demo:1 | not in catalogue | suffix-divergent → **UNRESOLVED** |
| `FortressFX` | `demo` | 1 | demo:1 | not in catalogue | UNRESOLVED |
| `PepperstoneUK-Demo` | `demo` | 2 | demo:2 | not in catalogue | UNRESOLVED (name suggests demo; not verified) |
| `Taurex-Demo` | `demo` | 1 | demo:1 | not in catalogue | UNRESOLVED (name suggests demo; not verified) |

\* `TradersWay-Live` shows an `is_demo=True` account despite `environment=live` — a pre-existing env/account
disagreement independent of this packet; recorded here, **not** corrected.

Per the packet, **only** the servers an operator has verified against real broker topology are classified
(IS6 + TradersWay pilots, seeded into the catalogue). Every other server is left **UNRESOLVED** — not
auto-classified from its suffix, not re-pointed. Resolving them is operator work (§6, future).

### 3.2 IS6 focus (the registration block for `51763830`)

- `IS6Technologies-Live` — recorded `demo`, verified **live**. Bound accounts (all **`is_active=False`**):

  | Acct id | is_demo | is_active | acct# (redacted) | user |
  |---|---|---|---|---|
  | 30 | True | False | 5-digit (redacted) | 34 |
  | 31 | True | False | 8-digit (redacted) | 35 |
  | 34 | True | False | 8-digit (redacted) | 38 |

  These three demo accounts are the agents of the poisoning. The target account `51763830` is **not** present
  (it has never been onboarded, consistent with the block).
- `IS6Technologies-Demo` — recorded `demo`, verified demo. 1 account (id 25, user 29). CONSISTENT.

### 3.3 Case-insensitive collisions

- `tradersway-live` ← {`TradersWay-Live`, `Tradersway-Live`} — two distinct `BrokerServer` rows that are the
  same broker identity. 3 accounts split across the two rows. Needs a governed **merge** decision (not done
  here). All other keys are unique.

---

## 4. Scope C — Governed production correction plan (DO NOT EXECUTE)

This plan is **not executed** in this packet. It modifies production `BrokerServer` records and therefore
requires explicit Sponsor authorisation and a separate, audited execution packet. Stated as preconditions →
action → postconditions → rollback, per the evidence/handoff rules.

### C.0 Preconditions (all must hold before any write)
1. Migration `0020` applied in production (adds the catalogue + `environment_source`; normalises casing;
   seeds the verified pilots). Verified by: catalogue has the 4 pilot rows; `WIMS-Demo` reads `demo`.
2. A fresh read-only snapshot matches §3 (no new accounts on the affected servers since). Re-run the §3 query
   and diff.
3. Sponsor sign-off on the IS6-Live account disposition (C.2) and the TradersWay collision merge (C.3).
4. A full DB backup taken immediately before (operations estate has no automated backup — treat as a RED
   precondition; capture a manual dump first).

### C.1 Correct `IS6Technologies-Live` → `live` (the defect)
- **Source of truth:** `BrokerServerCatalogue['is6technologies-live'] = live` (operator-verified).
- **Action (single transaction):** set `BrokerServer(server_name='IS6Technologies-Live').environment='live'`,
  `environment_source='operator'`. Audit the before/after.
- **Dependency (MUST resolve first — C.2):** three `is_demo=True` accounts (ids 30/31/34) are bound to this
  server. After the re-point they become demo-accounts-on-a-live-server, which
  `trading.classification.classification_error` treats as inconsistent.
- **Postcondition:** a LIVE registration for `IS6Technologies-Live` passes; a DEMO one is refused
  `REQ_ENV_MISMATCH`. (Proven in tests — §5 `PoisonedRowReproductionTests`.)
- **Rollback:** restore `environment='demo'`, `environment_source='inferred'` (or restore from backup).

### C.2 Disposition of the three poisoning accounts (Sponsor decision — options, recommendation)
All three are **`is_active=False`** (already disconnected/inactive), so the live impact is limited to
classification/analytics of historical rows.
- **Option A (recommended):** leave them as inactive historical records and record the known, inert
  inconsistency (they were mis-registered against a LIVE server as demo). Lowest risk; no history rewrite
  (data.md — raw records are immutable). Document in `KNOWN_ISSUES.md`.
- **Option B:** re-point each to `IS6Technologies-Demo` if broker evidence shows they were genuinely demo
  logins. Requires per-account broker verification; changes account→server linkage (governed).
- **Option C:** full audited tombstone if they are abandoned. Irreversible-ish; only if confirmed orphaned.
- **Do not** silently flip their `is_demo`. Account demo/live is authoritative to the account (account_policy)
  and tied to real broker identity; flipping it would falsify the record.

### C.3 Resolve the `TradersWay-Live` / `Tradersway-Live` collision (Sponsor decision)
Two rows, same broker identity, 3 accounts split. Governed **merge**: pick the canonical surface name, re-point
the minority row's accounts to the canonical row inside one transaction, then retire the duplicate row. Must
preserve each account's identity and history; audit every move. **Not** done here.

### C.4 Remaining UNRESOLVED servers (future operator work)
`WIMS-Demo`, `DominionMarkets(-Live)`, `FortressFX(-Trade)`, `PepperstoneUK-Demo`, `Taurex-Demo` — each needs an
operator to verify its real environment against the broker and add a **verified** catalogue row (or mark it
`operator`-corrected on the `BrokerServer`). Only after the catalogue covers the live estate should
`BROKER_SERVER_CATALOGUE_STRICT_ENABLED` be armed (R1). Until then they keep working via the legacy inferred
path.

### C.5 Failure handling
Any precondition or postcondition failure → **STOP**, do not proceed, restore from backup, report with redacted
detail. Never leave a half-applied correction.

---

## 5. Scope D — Certification evidence

### 5.1 Tests (exact commands + results)
```
cd backend && .venv/bin/python manage.py test hosted_workspace.tests_broker_server_governance
  -> Ran 38 tests … OK   (31 original + 7 added for the two adversarial findings)
cd backend && .venv/bin/python manage.py test hosted_workspace trading
  -> Ran 1615 tests … OK   (exit 0; full regression pre-fix; re-confirmed green post-fix)
cd backend && .venv/bin/python manage.py makemigrations --check --dry-run trading
  -> No changes detected in app 'trading'   (schema + data migration cover all model changes)
```
New suite `hosted_workspace/tests_broker_server_governance.py` (38 tests) covers every Scope-D scenario:
LIVE-on-verified-live, DEMO-on-verified-demo, both mismatch directions, unknown fail-closed (strict),
legacy auto-create (strict off, marked inferred), metadata-inconsistent (the IS6 reproduction) + post-correction
onboarding, existing-account compat (legacy inferred, consistent + mismatch), cross-user isolation,
case-insensitive collision detection, duplicate prevention (name_key unique), concurrent same-server race (one
row, both succeed, no IntegrityError), suffix-not-authoritative, the resolver/normalisation/model units, the
view 409 messages, and the migration seed outcome.

### 5.2 Migration idempotency + reversibility (scratch DB, actual output)
```
migrate trading 0019            -> baseline
migrate trading 0020            -> Applying … OK          (forward)
migrate trading 0019            -> Unapplying … OK        (reverse, no error)
migrate trading 0020            -> Applying … OK          (re-forward)
catalogue rows after re-forward -> 4
  [('is6technologies-demo','demo'),('is6technologies-live','live'),
   ('tradersway-demo','demo'),('tradersway-live','live')]
```
Idempotent (`update_or_create` on `name_key`), reversible without error, order-safe (RunPython after
CreateModel+AddField), and it rewrites **no** server identity beyond lowercasing the env value.

### 5.3 Adversarial review
Ran a 4-lens review (cross-user isolation, environment-validation, data-integrity/migration, backward-compat)
→ refute-by-default verification of every candidate. **2 candidates surfaced, both CONFIRMED (reproduced with
tests), both now FIXED and regression-covered.** The isolation and compat lenses found nothing real.

- **[HIGH — FIXED] Incomplete fix: `bind_broker_identity` (the deferred-identity sibling write path) was not
  corrected.** Its demo branch did a bare `get_or_create(server_name=…)`, so a DEMO account binding a
  catalogue-verified LIVE server (e.g. `IS6Technologies-Live`) created a demo-default row — re-introducing the
  exact poisoning defect on a second path and bypassing the mismatch guard; a later legitimate LIVE onboarding
  of that server would then be self-blocked `REQ_SERVER_METADATA_INCONSISTENT`. **Fix:** applied the identical
  catalogue-authoritative logic to `bind_broker_identity`, keyed on `acct.is_demo` (verified→must match; new
  row from verified value `source=catalogue`; disagreeing existing row → `BIND_SERVER_METADATA_INCONSISTENT`,
  untouched; genuinely-new → `BIND_SERVER_UNVERIFIED` under strict else legacy `inferred`); preserved
  `BIND_LIVE_FORBIDDEN` for demo/live disagreement. Regression: `BindPathGovernanceTests` (6 tests).
- **[LOW — FIXED] Case-variant 500 risk.** Making server resolution case-insensitive (`server_name__iexact`)
  while the Phase-C idempotency guard stayed exact meant a case-variant re-request of the same `(login, server)`
  could fall through to a create and raise an uncaught `IntegrityError`/500. **Fix:** aligned the idempotency
  guard to `server_name__iexact`, so a case variant resolves to the existing workspace (`REQ_EXISTS`).
  Regression: `CaseVariantIdempotencyTests`.

Post-fix, the full governance suite is **38 tests OK** and the full `hosted_workspace`+`trading` regression is
green (§5.1 figure re-confirmed after the fixes).

### 5.4 Stated limitations / not covered
- The strict posture is implemented and unit-tested but **not armed** anywhere (DARK). Its production safety
  depends on the catalogue first covering the live estate (R1).
- No end-to-end onboard of `51763830` was performed (out of scope, forbidden by the boundaries).
- Scope C is a plan only; **no production record was read-written** — §3 is read-only.
- Broker-truth for the UNRESOLVED servers was not independently obtained; they are deliberately left unresolved.

---

## 6. Outstanding risks & deployment readiness

- **R1 (Amber):** Arming `BROKER_SERVER_CATALOGUE_STRICT_ENABLED` before the production catalogue covers the
  live estate would refuse legitimate onboarding to not-yet-catalogued servers. Arming is gated on §C.4 and a
  Sponsor decision. Default OFF is safe.
- **R2:** §3 reveals pre-existing data-quality issues beyond the IS6 defect (the TradersWay case-collision; a
  live account on demo-recorded `DominionMarkets`; the `TradersWay-Live` demo account). These are **recorded,
  not fixed** here; they belong to the governed correction packet (§C).
- **R3:** Applying `0020` in production normalises `WIMS-Demo` `DEMO`→`demo` (lossless) and seeds the catalogue
  — a real (if benign) data migration; it is part of §C.0 and must run under the backup precondition.

**Deployment-readiness recommendation:** the code is **mergeable to `main` as DARK** (strict default OFF;
always-on catalogue authority is additive and fully regression-covered). **Do not deploy or arm strict** until
the governed production correction packet (§C) runs with Sponsor sign-off. This PR is a prerequisite for that
packet, which is itself a prerequisite for onboarding IS6FX LIVE `51763830`.

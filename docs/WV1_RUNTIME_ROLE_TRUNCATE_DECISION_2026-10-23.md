# WV1 / ADR-0054 follow-up — Migration-identity TRUNCATE-over-evidence decision packet (2026-10-23)

**Status:** **PD-ACCEPTED INTERIM & CONDITIONAL (2026-10-23)** — disposition `ACCEPT_TRUSTED_MIGRATION_IDENTITY`,
not yet implemented in production. Option D′ recorded as the **required stronger target architecture before
general customer availability** (not implemented this increment).
**Base:** canonical main `3586b540` (F-8 runtime-role separation `75e5469a` is in main's history).
**Scope:** governance of the trusted migration identity's effective ability to destroy immutable evidence
(`broker_intelligence_evidenceblob`, `broker_intelligence_brokerevent`). **No production mutation. No MT5 /
dashboard / Copy-Trader. No OAuth.**

All privilege claims below are **empirically proven** on disposable PostgreSQL 15 (15.19 Debian) reproducing the
F-8 topology faithfully (local-trust → faithful `session_user` per role → `SET ROLE` membership tested truly; no
credentials). Production is PG **15.15** — identical major, identical privilege semantics. Positive + negative
controls were used throughout (RULE 11). Test drivers: `scratchpad/trunc/matrix2.sh`, `matrix3.sh`.

---

## 1. The question

In the merged F-8 model, `guvfx_schema_owner` owns **all** application objects **and** schema `public`;
`guvfx_migrate` is a **NOINHERIT member** of `guvfx_schema_owner` and exists to run governed migrations by
`SET ROLE guvfx_schema_owner`. Because migrate can *become* the object owner, can any table-level control
(`REVOKE TRUNCATE`, a `BEFORE TRUNCATE` trigger) actually stop it from destroying evidence — or only stop the
runtime and erasure roles?

---

## 2. Proven PostgreSQL-15 facts (the measurement, not assumptions)

| # | Observation | Result |
|---|---|---|
| C0 | `guvfx_app` session is non-super; **cannot** `SET ROLE guvfx_schema_owner` (faithful-session control) | confirmed |
| — | `guvfx_migrate` **is** a member of `guvfx_schema_owner` (`pg_has_role … MEMBER` = t) | confirmed |
| B | `REVOKE TRUNCATE … FROM guvfx_schema_owner` → `has_table_privilege(...,'TRUNCATE')` = **f** (owner privilege **is** revocable) | confirmed |
| B1 | migrate-as-owner TRUNCATE immediately after the REVOKE → **DENIED** | confirmed |
| **B2** | migrate-as-owner **self-`GRANT TRUNCATE` then TRUNCATE** → **OK (table emptied)** → **REVOKE bypassed** | **confirmed** |
| A1 | `BEFORE TRUNCATE` trigger present, owner TRUNCATE → **DENIED** ("evidence immutable") — trigger fires | confirmed |
| **A2** | owner **`ALTER TABLE … DISABLE TRIGGER` then TRUNCATE** → **OK (emptied)** → **trigger bypassed** | **confirmed** |
| A3 | migrate `SET session_replication_role = replica` → **DENIED** (SUSET / superuser-only) — that bypass is closed to migrate | confirmed |
| **D6** | evidence owned by a *separate* role but still in `public`: migrate-as-`schema_owner` **`DROP TABLE`** → **OK (table gone)** — the **schema owner may drop any table in its schema** | **confirmed** |
| R1/R3 | `guvfx_app` TRUNCATE, `guvfx_erasure_exec` TRUNCATE → **DENIED** | confirmed |
| R4 | `guvfx_erasure_exec` row `DELETE` → **OK** (governed row-scoped erasure path intact) | confirmed |

**Interpretation.** Any role that can *assume the object owner* can destroy the object: it self-grants TRUNCATE
(B2), disables/drops the trigger (A2), or — because `schema_owner` owns `public` — simply `DROP`s the table
(D6). `guvfx_migrate` can assume `guvfx_schema_owner` by design. Therefore **Options A and B provide no durable
protection against the migration identity**; they only stop non-owner roles and accidents (which the F-8 grants
already stop). The `session_replication_role` superuser bypass is **not** available to migrate (A3).

> Per the directive's explicit caution: **this packet does not claim `REVOKE TRUNCATE` protects against a role
> that can assume the table owner — the test proves the opposite (B2).**

---

## 3. Corrected Option D′ — dedicated evidence schema (proven effective)

Because the `public` schema owner can DROP any table in `public` (D6), a separate *table* owner in `public` is
insufficient. The only configuration that holds is evidence tables in a **dedicated schema** (`evidence`) owned
by a role (`guvfx_evidence_owner`) that `guvfx_migrate` is **not** a member of. Proven (`matrix3.sh`):

| # | As `guvfx_migrate` | Result |
|---|---|---|
| Dp1 | `SET ROLE guvfx_evidence_owner` | **DENIED** (not a member) |
| Dp2 | `TRUNCATE evidence.blob` (direct) | **DENIED** |
| Dp3 | `SET ROLE guvfx_schema_owner; TRUNCATE evidence.blob` | **DENIED** |
| Dp4 | `SET ROLE guvfx_schema_owner; GRANT TRUNCATE … TO schema_owner` | **DENIED** (not owner) |
| Dp5 | `ALTER TABLE evidence.blob OWNER TO guvfx_migrate` | **DENIED** |
| Dp6 | `SET ROLE guvfx_schema_owner; DROP TABLE evidence.blob` | **DENIED** (other schema) |
| Dp4′ | `SET ROLE guvfx_schema_owner; DROP SCHEMA evidence CASCADE` | **DENIED** (must own schema) |
| Dp5′ | `SET ROLE guvfx_schema_owner; ALTER TABLE … DISABLE TRIGGER ALL` | **DENIED** |
| Dp10/11 | `guvfx_erasure_exec` row `DELETE` → **OK**; TRUNCATE → **DENIED** | governed erasure intact |
| Dp12 | `guvfx_evidence_owner` (bootstrap-gated) TRUNCATE | **OK** (retention path intact for the true owner) |
| **Dp13/14** | `SET ROLE guvfx_schema_owner; ALTER/CREATE in evidence schema` | **DENIED** — **the operational cost** |

D′ holds. Its cost: `guvfx_migrate` **cannot** migrate the evidence schema, so evidence-table DDL needs a
separate, explicitly-authorized identity/path; the two existing evidence tables must be **relocated** from
`public` to `evidence`; and the app/erasure code must reference them by qualified name or an extended
`search_path`. Bootstrap `guvfx` can always destroy (unavoidable; two-person custody).

---

## 4. Option comparison (per the directive's required dimensions)

| Dimension | A — BEFORE TRUNCATE trigger | B — REVOKE TRUNCATE | C — Accept trusted migration identity | D′ — Dedicated evidence schema |
|---|---|---|---|---|
| Owner can bypass/disable/drop? | **YES** — DISABLE/DROP trigger (A2); schema-owner DROP (D6) | **YES** — self-GRANT back (B2); schema-owner DROP (D6) | N/A (acceptance, not a control) | **NO for migrate** (Dp1–Dp6); only true owner + bootstrap |
| `migrate` regains TRUNCATE via `SET ROLE schema_owner`? | **YES** (disable trigger) | **YES** (self-grant) | **YES — by design** | **NO** (Dp3/Dp4) |
| Breaks Django migrate / test teardown / `flush`? | **YES** — trigger raises on any evidence TRUNCATE (TransactionTestCase teardown & `flush` use TRUNCATE) unless disabled | Moderate — owner self-grants; a non-owner test role is blocked (prod never flushes) | **NO** — nothing changes | **YES (evidence schema)** — migrate cannot ALTER/CREATE evidence; needs a separate DDL path; prod never flushes, CI runs as a privileged role |
| Runtime (`app`) & erasure cannot TRUNCATE? | YES (already via grants) | YES (already via grants) | **YES** (proven R1/R3) | **YES** (Dp9/Dp11) |
| Required credentials / access | none | none | `guvfx_migrate` SCRAM under two-person custody; pgAudit | new `guvfx_evidence_owner` (NOLOGIN) + a bootstrap-gated evidence-DDL path + custody |
| Rollback / recovery | drop trigger | re-GRANT TRUNCATE | none | relocate evidence back to `public`; drop `evidence` schema + role |
| Implementation complexity | Low — but breaks teardown | Trivial | **Zero** + compensating controls | **High** — new schema/role, evidence relocation, bifurcated migration path, app `search_path`/qualified names, erasure-proc targeting |
| Residual risk | **HIGH** — no protection vs migration identity (theatre) | **HIGH** — no protection vs migration identity (theatre) | **MEDIUM** — trusted, governed, non-runtime identity can destroy evidence; detective (audit) not preventive; runtime/erasure surface already closed | **LOW (vs migration tier)** — proven preventive; residual = bootstrap superuser (unavoidable) + a new governed evidence-DDL surface |

---

## 5. Recommendation → PD disposition

### `ACCEPT_TRUSTED_MIGRATION_IDENTITY` — **ACCEPTED by the PD on 2026-10-23 as INTERIM & CONDITIONAL**

The PD accepted the trusted migration identity as an **interim governance boundary** under **all** of the
following binding conditions (this acceptance **expires before general customer availability**):

1. `guvfx_migrate` is **never** used by a persistent application service.
2. Its credential exists **only** for an approved maintenance window.
3. It remains **NOINHERIT** and assumes ownership authority **only** through explicit `SET ROLE`.
4. Credential access and **each use** are recorded.
5. Every migration window has a **reviewed migration plan** and an **immutable command transcript**.
6. Destructive evidence operations are **prohibited operationally**.
7. **Database audit logging for TRUNCATE, DROP and DELETE on protected evidence is verified before production
   cutover.** If pgAudit is unavailable, the disposition returns **CHANGES REQUIRED** with a *tested* alternative
   — an unavailable control must not be claimed.
8. The credential is **revoked, rotated, or removed** from the operator environment after the window.
9. This acceptance **expires before general customer availability**.
10. **Option D′ (dedicated evidence schema owned by a role the migration identity cannot assume) is recorded as
    the required stronger target architecture before general release** — **not** implemented this increment.

**Rationale (evidence-backed):**
1. **Options A and B are ineffective** against the migration identity (B2, A2 proven) — deploying either *as
   protection* would be security theatre and is explicitly disallowed by the directive. Option D (separate owner,
   same schema) is also ineffective (D6).
2. **Only D′ works**, and it is a **substantial structural change** — relocating the very evidence tables the
   control protects, bifurcating the migration path, and touching app `search_path` and the erasure SECURITY
   DEFINER proc. That belongs in its own Sponsor-gated packet, not bolted onto the merged F-8 cutover.
3. The threat ADR-0054 actually defends — a **compromised runtime** or an **unauthorized erasure** — is **fully
   closed** by F-8 (proven: `guvfx_app` and `guvfx_erasure_exec` cannot TRUNCATE/DROP evidence; erasure is
   row-scoped and governed). The migration identity is a **distinct, higher-trust tier**; its SCRAM is not
   deployed to any running service (minted at cutover under custody).

**Mandatory compensating controls for C (make the acceptance real, not implicit):**
- `guvfx_migrate` SCRAM under **two-person custody**; never stored in a running service's environment; used only
  for an authorized, logged migration window.
- **pgAudit** (or equivalent) logging of `TRUNCATE`/`DROP`/`DELETE` on the two evidence tables, shipped off-host.
- `guvfx_migrate` **never** used for routine/interactive work; migrations run through the governed path only.
- **Recorded acceptance** in the risk register (Notion, PM-owned) naming this residual and these controls.

**Lever for the PD:** if the governance decision is that evidence-immutability must bind even the migration tier
(preventive, not detective), elect **`IMPLEMENT_STRONGER_TRUNCATE_CONTROL`** → Option D′, which is **proven** and
specified in §3. It is cheapest to adopt **now**, while the evidence tables are near-empty. A and B must not be
adopted under any disposition.

**Not chosen:** `RUNTIME_ROLE_DESIGN_CHANGES_REQUIRED` — the F-8 runtime-role design is sound; this is a scope
boundary (a known, recorded residual), not a defect.

---

## 6. Limitations / not covered
- Tested on PG 15.19 (disposable); production is 15.15 — same major, semantics identical; not re-tested on 15.15
  bytes (privilege rules are version-stable across 15.x).
- D′'s interaction with the ADR-0054 erasure SECURITY DEFINER proc (which must own/target the relocated evidence
  schema) is **not** implemented or rehearsed here — it is part of the D′ packet if elected.
- This packet does not change any production role, privilege, or object. Nothing here was run against production
  except **read-only** catalog inspection.

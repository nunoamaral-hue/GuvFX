# WV1 / ADR-0054 F-8 — Production-prep governed return packet (2026-10-23)

**Base:** canonical main `3586b540` (F-8 `75e5469a` in history). **Authorized work performed:** READ-ONLY
verification, design, documentation, disposable-container rehearsal only. **Production: NO-GO (unchanged).
Erasure: OFF / UNPROVISIONED.**
> Reconciliation note (2026-10-23): this document is the record of the *first* production-prep return
> (`WV1_RUNTIME_ROLE_PRODUCTION_PREP_READY`). The PD subsequently accepted the interim TRUNCATE disposition and
> authorized recovery + read-only inventory + disposable rehearsal; that execution is tracked in
> `WV1_RUNTIME_ROLE_STAGE8_RETURN_2026-10-23.md`. No dashboard/MT5/Copy-Trader content is part of this workstream.
Companion docs: `WV1_RUNTIME_ROLE_TRUNCATE_DECISION_2026-10-23.md`,
`WV1_RUNTIME_ROLE_PRODUCTION_CUTOVER_PLAN_2026-10-23.md`.

## 1. Post-merge CI on `75e5469a` — 5/5 SUCCESS
| Job | Conclusion | Run ID |
|---|---|---|
| backend | success | 120327782957 |
| frontend | success | 120327782868 |
| governance | success | 120327782912 |
| market-data-foundation | success | 120327782698 |
| research-foundation | success | 120327782893 |

No failing checks; no correction PR required. (Canonical `main` has since advanced to `3586b540` via #555 —
unrelated analytics; `75e5469a` remains in history with 5/5 CI.)

## 2. TRUNCATE option comparison (empirically proven on disposable PG15; prod is 15.15, same semantics)
- **A (BEFORE TRUNCATE trigger):** owner can **DISABLE/DROP** the trigger → TRUNCATE succeeds (proven A2). No
  protection vs the migration identity.
- **B (REVOKE TRUNCATE from owner):** owner privilege *is* revocable (has_priv=f), but the owner **self-GRANTs it
  back** → TRUNCATE succeeds (proven B2). No protection vs the migration identity.
- **Schema-owner DROP:** `guvfx_schema_owner` owns `public` and may **DROP any table in it**, even one owned by
  another role (proven D6) → a separate *table* owner in `public` does not help.
- **D′ (dedicated `evidence` schema owned by a role `guvfx_migrate` is NOT a member of):** migrate cannot
  SET ROLE to it, cannot TRUNCATE/DROP/DROP-SCHEMA/disable-trigger/steal-ownership (proven Dp1–Dp6); runtime &
  erasure stay correct; true owner + bootstrap retain a retention path. **Cost:** migrate cannot ALTER/CREATE
  evidence objects → evidence DDL needs a separate authorized path + evidence-table relocation (proven Dp13/14).
- **C (accept trusted migration identity):** zero change; runtime/erasure surface already closed.

Full matrix + the four required per-option dimensions: see the decision packet §2–§4.

## 3. Evidence-backed recommendation → `ACCEPT_TRUSTED_MIGRATION_IDENTITY`
A and B are proven ineffective against a role that can assume the owner (so must not be deployed as protection);
D (same schema) is ineffective; **only D′ works** but is a substantial structural change (evidence relocation +
bifurcated migration path + app `search_path`/erasure-proc changes) that belongs in its own packet. The threat
ADR-0054 actually defends — compromised **runtime** / unauthorized **erasure** — is **fully closed** by F-8.
Recommend **ACCEPT (C)** near-term **with mandatory compensating controls** (two-person SCRAM custody; pgAudit on
evidence TRUNCATE/DROP/DELETE; migrate used only in governed windows; recorded risk-register acceptance).
**Lever for the PD:** elect `IMPLEMENT_STRONGER_TRUNCATE_CONTROL` (Option D′, proven, cheapest now) if
evidence-immutability must bind the migration tier. **Do not ratify/implement without a subsequent PD disposition.**

## 4. Complete proposed production sequence
20-stage, per-stage-gated plan in the cutover-plan doc (T0 = Stage 9 forward separation). Uses fresh read-only
prod observations: PG 15.15; `guvfx` sole superuser owning 137 tables/130 seq/5 trigger-funcs; `public` owned by
`pg_database_owner` (cutover must re-own it); broker_intelligence at **0010** → repo leaf **0022** (12 pending);
≈9 worker connections to drain; **0 SECURITY DEFINER** functions.

## 5. Per-stage rollback matrix
| Mutation stage | Boundary | Rollback |
|---|---|---|
| 9 forward separation | single BEGIN/COMMIT (fail-closed) | error → auto-ROLLBACK (nothing changed) |
| 11 migrations | per-migration | recovery-point restore (Stage 1) |
| 12 erasure provisioning | explicit | `production_rollback_erasure.sql` (F-8-aware; preserves ownership boundary) |
| 10 config (pgAudit, if adopted) | reload | reload revert |
| 15 app `DB_USER` cutover | per-container | re-point `DB_USER`→`guvfx` + redeploy (fast path) |
| teardown | reverse order | erasure-rollback → config re-point + drain → `cutover.sh --rollback` (refuses w/ active app sessions) |
Invariant: **always ≥1 controlled superuser (`guvfx`)**; `guvfx_app` never becomes an owner/superuser on any path.

## 6. Credential-custody model
SCRAM-SHA-256 **verifiers** (not cleartext) for `guvfx_app` and `guvfx_migrate` minted in the secret store,
written to a `0600` env file, exported into the cutover shell (`set -a; . file; set +a`), then the file deleted.
Verifiers reach psql **only** via environment `\getenv`/`docker exec -e NAME` — **never** on argv, in shell
history, in the combined SQL, in Git, or in the server statement log (the cutover txn runs under
`SET LOCAL log_statement='none'` + `log_min_duration_statement=-1`; bash format-validates before psql).
Two-person custody; `guvfx_migrate` never persisted in a running service's environment.
**Rehearsal proof:** 0 verifier occurrences on process argv during `--execute` (psql process positively observed);
combined temp SQL removed on exit; creds file `0600`.

## 7. Expected downtime & operational prerequisites
Downtime limited to broker_intelligence writers for the Stage-9 transaction + Stage-11 migrations (~minutes; no
heavy table rewrites expected in 0011→0022 — confirm with `migrate --plan` in a final dry-run). **Trading/MT5
unaffected.** Prerequisites: fresh off-host encrypted recovery point on the NAS (`100.73.130.98`) + disposable
restore-verify; reviewed+SHA-pinned manifest (2 reviewers); maintenance window; writer drain; secret-store
credentials under two-person custody; named operator + two-person GOs at Stages 4/8/9/12. Standing watch items:
disk `/` ~90%; single logical recovery point.

## 8. Disposable-rehearsal evidence (Phase 4 — ALL PASSED; refusal rc ledger `3 2 2 3 1 3`)
Fresh disposable PG15, synthetic prod-like pre-state, the **actual** merged `ops/erasure` scripts:
- manifest **mismatch refuses before mutation** — downgrade-protected (rc3), SHA-pin mismatch (rc2), bare-`\.`
  injection (rc2), manifest-object-missing-live (rc3); schema owner unchanged, evidence table intact.
- **active sessions refuse** rollback (rc3); after drain, rollback proceeds (rc0) and **returns to start**
  (`public`→`pg_database_owner`, guvfx owns 12, app owns 0).
- **credentials off argv/logs/repo** — 0 verifier occurrences on argv (psql observed); temp SQL removed; `0600`.
- **app owns nothing**; app SELECT/INSERT/UPDATE/DELETE on app tables OK; SELECT/INSERT on protected OK;
  **DELETE/TRUNCATE on protected DENIED**; cannot assume schema_owner; cannot CREATE in schema.
- **migration identity performs migrations** (create+alter; new objects owned by schema_owner).
- **future functions (incl. SECURITY DEFINER) non-executable by app**; trigger functions not directly callable.
- **verification catches intentionally weakened privileges** (granted app DELETE on evidence → verifier rc1).
- **every refusal exited nonzero.**
Drivers: `scratchpad/trunc/matrix2.sh`, `matrix3.sh`, `scratchpad/reh/phase4.sh`.

## 9. Remaining risks (P0–P3)
- **P0:** none.
- **P1:** none (no blocking defect; F-8 design sound; CI 5/5; rehearsal all-pass).
- **P2:**
  - **Migration-identity evidence destruction** is governance-accepted (C), **not technically prevented** —
    detective (audit) only; pending PD disposition (Phase-2 finding).
  - **Config-drain completeness:** a missed worker would keep connecting as superuser `guvfx`; Stage 15/16
    zero-`guvfx`-session verification is mandatory (≈9 workers).
  - **Prod behind on migrations (0010→0022):** in-window manifest must be re-captured; `migrate --plan` must
    confirm no heavy rewrites / no unexpected pending app.
- **P3:**
  - Pre-existing PLAIN invoker functions would retain PUBLIC EXECUTE post-cutover — **moot in prod** (0 SECDEF;
    all 5 funcs are trigger funcs, not directly invocable); recorded.
  - `broker_public MetricParticipationConsent.owner CASCADE` → `User.delete()` fails on prod (table unmigrated);
    dormant (no user-deletion workflow); Stage-11 applies broker_public 0002.
  - PG minor differs (prod 15.15 vs rehearsal 15.19) — semantics version-stable; not re-tested on 15.15 bytes.
  - If a pgAudit auditor is configured, its GUCs are not governed by the cutover's log-suppression — handle for
    the window.

## 10. Updated Withdrawal Intelligence percentages (this control)
- **F-8 runtime-role separation:** design + 3-round review + rehearsal + CI, **merged DARK → `75e5469a`** —
  engineering **~95%** complete; **production operability 0% / NO-GO**.
- **TRUNCATE governance:** decision packet **READY (proposal)**; **0% ratified** (PD disposition pending).
- **Production cutover:** plan **READY (proposal)**; **0% executed**.
- Broader WV1 (ingestion, customer Gmail UI, board demo) tracked separately; unchanged by this work.

## 11. Exact Sponsor decisions still required
1. **Ratify the TRUNCATE disposition:** `ACCEPT_TRUSTED_MIGRATION_IDENTITY` (recommended, + compensating
   controls) **vs** `IMPLEMENT_STRONGER_TRUNCATE_CONTROL` (Option D′) **vs** defer.
2. **Authorize the production cutover packet** — per stage. First mutating action is Stage 1 (recovery point,
   read-only on DB) and then Stage 9 (forward separation), each its own GO.
3. **Confirm** the maintenance window, named operator, and two-person approvers (Stages 4/8/9/12).

## 12. Confirmation production was unchanged
Production received **only READ-ONLY** catalog inspection — two SELECT-only transactions wrapped in
`SET TRANSACTION READ ONLY` against `guvfx-postgres`. **No** production role/ownership/privilege/credential/config
change; **no** migration; **no** deploy/restart; **no** erasure provisioning; **no** erasure flag change; **no**
erasure executed; **no** OAuth change. **MT5, dashboard, and Copy-Trader untouched.** All rehearsals ran on
disposable containers (`guvfx_trunc`, `guvfx_reh`), now removed; the shared local `postgres`/`guac-db`/
`guvfx-postgres` containers were not modified.

---
**RETURN STATUS: `WV1_RUNTIME_ROLE_PRODUCTION_PREP_READY`**

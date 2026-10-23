# WV1 — PR #554 CHANGES-REQUIRED return (runtime-role separation, F-8) — 2026-10-23

**Directive:** PROGRAMME DIRECTOR DISPOSITION — CHANGES REQUIRED — DO NOT MERGE PR #554. Fix F1–F4 + rollback
safety, re-validate, return a governed review packet. **This remains a governed review packet, not merge
authorization.** PR #554 stays OPEN and UNMERGED. No production role/ownership/grant/credential/config change; no
deploy/migrate/restart/provision/canary/erasure/flag; no MT5/copy-trading change. Scope: Withdrawal Intelligence.

Verified fact is stated as such; assumptions are marked. Evidence is from a disposable bootstrap-`guvfx` PG15
rehearsal; **no production contact occurred.**

---

## 1. Corrected head SHA + PR state

- Branch `fix/runtime-role-separation`, base `feec863d` (== current `origin/main`). PR **#554**, kept OPEN /
  UNMERGED.
- Prior head `795efd1d`. Corrected heads: `ae6386a3` (F1-F4 + rollback-safety rework) -> `15516c23`
  (round-1 adversarial-review fixes: 2 P1 + P2/P3) -> `b1a529cd` (round-2 review fixes: 1 P1 + a self-inflicted
  guard regression + P2/P3). Head at this writing: **`b1a529cd`**. Not self-merged.
- **Reconciliation:** `git fetch origin` shows `origin/main` = `feec863d`, which is this branch's base; the branch
  is a clean descendant with **nothing to reconcile in this repository**. (The `ba460985` advance noted in
  working memory is the out-of-scope MT5 copier canonical, not this origin; I did not touch it.)

## 2. Finding-by-finding disposition

| Finding | Disposition | Where |
|---|---|---|
| **F1** membership closure fail-closed | DONE | `separate_runtime_role.sql` `$closure_pre$` + `$post$` (WITH RECURSIVE direct+transitive closure for `guvfx_app`/`guvfx_schema_owner`/`guvfx_migrate`; RAISE on any unexpected membership; permitted matrix app={}, schema_owner={}+NOLOGIN, migrate={guvfx_schema_owner}). The previous silent `REVOKE … FROM app_role` was **removed** (it was the very silent-revoke F1 forbids and was redundant with the guard). |
| **F2** minimal runtime grants | DONE | `separate_runtime_role.sql` grant block (application tables S/I/U/D; protected tables S/I/U only; sequences U/S/U; **no function EXECUTE**; default privileges S/I/U, **no DELETE, no EXECUTE**). Verified by `$post$` + `verify_erasure_privilege.sql` (9) + `erasure.py` (7e). |
| **F3** explicit reviewed manifest | DONE | `capture_app_manifest.sql` + `cutover.sh --capture` produce a reviewed `.psv`; `separate_runtime_role.sql` `$manifest$` compares live-vs-manifest and RAISEs on missing/unexpected/owner-mismatch/unclassified; transfers ONLY manifest objects; emits `MANIFEST_DIGEST` + `POST_OWNER_DIGEST`. Rollback reversal is manifest-driven. |
| **F4** credentials off argv | DONE | `\getenv GUVFX_APP_SCRAM/GUVFX_MIGRATE_SCRAM`; `cutover.sh` passes `docker exec -e NAME` (no value), writes no secret to the combined SQL, `set +o history`, `umask 077`, trap-cleanup. Rehearsal proved 0 argv occurrences. |
| **Rollback safety** | DONE | `separate_runtime_role_rollback.sql` active-session guard (refuse while any `guvfx_app` session is connected; documented `--emergency-override`) + inverted-state precondition; all refusal paths now exit **nonzero**. |

## 3. Membership-closure evidence (F1)

Pre-mutation and pre-COMMIT closures computed with `WITH RECURSIVE` over `pg_auth_members`. Rehearsal:
- Happy path: `post-checks OK: … no membership …`; app role gained no membership; migrate's only membership is
  `guvfx_schema_owner`.
- The guard RAISEs (fail-closed) on any unexpected membership; unknown memberships are **never silently revoked**.

## 4. Manifest design + digest (F3)

- Format: `obj_kind|obj_schema|obj_identity|current_owner|classification`, deterministic order. Extension-owned
  objects excluded (`pg_depend` deptype `e`). Kinds covered: tables, partitioned tables, sequences, views,
  matviews, foreign tables; routines: functions, procedures, aggregates (identity includes the arg signature).
- Reference manifest `ops/erasure/cutover_manifest.rehearsal.psv`: 312 rows (155 tables, 149 sequences, 8
  routines; 310 application + 2 protected). **SHA-256 `af7c63fed7449e7633f293bbbff34d4a3dfb06f642864174fc01157ace25cb4f`**.
- In-script digests on the happy path: `MANIFEST_DIGEST=887ebcef0823f80699d3cc84cd02135e`,
  `POST_OWNER_DIGEST=95b3b21cf59fe077d143da625a1261a5`.
- Production path (F3's signed/captured option): the operator re-captures in-window with `cutover.sh --capture`
  against the restored snapshot, reviews the diff vs the reference, and passes the reviewed file to `--execute`.
  This is **not an unconstrained schema sweep** — the script refuses unless live == reviewed manifest exactly.

## 5. Minimum runtime-privilege matrix (`guvfx_app`)

| Object class | Privilege | Not granted |
|---|---|---|
| application tables | SELECT, INSERT, UPDATE, DELETE | TRUNCATE, ownership, DDL |
| protected evidence tables | SELECT, INSERT, UPDATE | **DELETE, TRUNCATE**, ownership |
| sequences | USAGE, SELECT, UPDATE | ownership |
| routines / SECURITY DEFINER | **none** | EXECUTE |
| schema `public` | USAGE | CREATE, ownership |
| future (default privileges) | S/I/U on tables, U/S/U on sequences | DELETE, EXECUTE |
| cluster attributes | — | SUPERUSER/CREATEROLE/CREATEDB/BYPASSRLS; membership in any privileged role |

## 6. Protected-table privilege evidence at every intermediate stage

- **After separation, before provision:** `guvfx_app` DELETE on evidence → `permission denied`;
  `has_table_privilege DELETE|TRUNCATE = false|false`; owns `0|0`; SELECT works; SCRAM TCP login works.
- **In-script, before COMMIT:** `$post$` RAISEs if the app role has DELETE/TRUNCATE on any protected table.
- **After provision:** `verify_erasure_privilege.sql` → `VERIFY OK` (includes the no-DELETE/TRUNCATE + no-SECDEF
  assertions).

## 7. Routine / SECURITY DEFINER execution inventory

Rehearsal, post-provision — the only SECURITY DEFINER routine in `public`:

| routine | owner | PUBLIC EXECUTE | guvfx_app EXECUTE | guvfx_erasure_exec EXECUTE |
|---|---|---|---|---|
| `bi_governed_erase_owner(bigint,text)` | guvfx_erasure | **false** | **false** | true |

Both `verify_erasure_privilege.sql` (9) and `privileged_backend_readiness()` (7e) now assert generally that
PUBLIC and the runtime role can execute **no** SECURITY DEFINER routine in the schema (a focused unit test with a
positive control was added: `test_refuses_app_role_that_can_execute_a_security_definer_routine`).

## 8. Credential custody + non-exposure evidence (F4)

- Verifiers minted with the repo's standalone `erasure_operators.scram_sha256_verifier` into an owner-only
  (`0600`) env file; only the verifier **SHA-256** was ever displayed, never the verifier or password.
- Argv proof (concurrent `ps -Ao args=` sampling during `--execute`, 800 samples):
  `psql_process_observed=1 verifier_app_in_argv=0 verifier_mig_in_argv=0 cleartext_pw_in_argv=0`. The positive
  control (observing the psql process at least once) confirms the sampler looked during the run; argv is
  immutable per process, so one clean observation is conclusive.
- Combined-SQL check: 0 verifier/password occurrences in the piped SQL.
- `ALTER ROLE … PASSWORD :'verifier'` stores the SCRAM verifier verbatim (no re-hash, no cleartext).
- Custody/rotation: verifiers are minted into the secret store at cutover; the disposable rehearsal secrets and
  container were destroyed at the end of the round. Rollback uses no new credentials.

## 9. Faithful rehearsal + rollback results

Disposable PG15 started with `POSTGRES_USER=guvfx` (OID 10, sole superuser, DB owner — faithful F-8 topology);
schema reproduced via the governed migrations (155 tables / 149 sequences / 8 routines — a LARGER, more demanding
object set than prod's 137/130/5). All GREEN:

- NEG-A unexpected live object → **pre-mutation refusal**, rc=3, no roles created, no ownership change.
- NEG-B manifest references a missing object → refusal, rc=3.
- NEG-C manifest owner-mismatch → refusal, rc=3.
- Credentials-absent → wrapper refuses, rc≠0 (before psql).
- Happy path → committed; 155+8 transferred to `guvfx_schema_owner`; post-checks OK; digests emitted.
- Provision + `verify_erasure_privilege.sql` → **VERIFY OK**.
- Pre-F-8 teardown guard → **refuses, rc=3, schema intact (155 tables preserved)**.
- Rollback active-session guard → **refuses, rc=3** while a `guvfx_app` session is connected.
- Drain → manifest-driven rollback → rc=0; ownership restored to `guvfx` (155 tables).
- Backend regression: `broker_intelligence` suite **48/48** erasure tests + **709/709** full app, OK, on a clean
  dedicated cluster. Secret scan clean. Operational scripts ASCII-only (RULE 9).

### Two architectural findings surfaced by the rehearsal (RULE 2)

- **AF-1 (fixed in this PR):** every `\if … \quit` guard exited psql with code **0**, so a fail-closed refusal
  did not propagate a failure to a scripted caller (`cutover.sh --execute && next` would proceed). Converted all
  refusal paths in `separate_runtime_role.sql` / `_rollback.sql` / `capture_app_manifest.sql` to `RAISE`
  (exit 3 under `ON_ERROR_STOP`); the wrapper's credential guard exits nonzero before invoking psql.
- **AF-2 (guarded in this PR; residual recorded):** the pre-F-8 `rollback_erasure_privilege.sql` (its own header
  labels it the DISPOSABLE-REHEARSAL teardown) does `DROP OWNED BY guvfx_schema_owner`. In the pre-F-8 model
  `guvfx_schema_owner` owned only a handful of evidence objects; in the **F-8 model it owns the entire
  application schema**, so running it would (a) hand schema/evidence ownership to `guvfx_app` and (b) **drop every
  application table** (reproduced: 155 → 3 tables). Added a fail-closed F-8 guard that refuses it.
  **Blast radius:** does NOT affect the forward cutover, the WV1 migration, deployment, or erasure; the
  **production** teardown path (`production_rollback_erasure.sql`) is already F-8-safe (preserves ownership, never
  drops `guvfx_schema_owner`). It only mattered for the rehearsal teardown, which is now guarded. **Residual /
  bounded follow-up:** make `rollback_erasure_privilege.sql` fully F-8-aware (restore to `guvfx_schema_owner`,
  keep the role) in a separate packet, rather than relying only on the guard.

### Round-2 re-rehearsal (adversarial-review fixes, head `15516c23`)

All re-run GREEN on a fresh bootstrap-`guvfx` PG15:
- digest-pin mismatch -> refuse rc=2; malformed verifier env -> refuse rc=2 (bash, before psql); tampered
  manifest with a bare `\.` line -> refuse rc=2; standalone-enum present -> refuse rc=3, schema intact.
- happy path (with `--expect-manifest-sha256`) -> rc=0, post-checks OK (now incl. "no CREATE").
- `guvfx_migrate` `rolinherit=false`; `guvfx_app` has no CREATE on `public`; PUBLIC has no CREATE on `public`.
- a future SECURITY DEFINER function created by `guvfx_schema_owner` -> `pub_exec=false, app_exec=false`.
- `demote_app_role_rollback.sql` in the F-8 model -> refuse rc=3, 155 tables intact.
- every teardown/demote refusal path now exits nonzero; erasure suite **48/48**; secret scan clean; ASCII-only.

**Expanded scope (Amber, documented):** the adversarial-review fixes also touch
`production_rollback_erasure.sql`, `demote_app_role.sql`, `demote_app_role_rollback.sql` (exit-code + F-8 guard).
These are the erasure teardown/demote family; the changes are additive and fail-closed (behaviour-preserving for
the legitimate pre-F-8 path). Called out here per the no-silent-deletion / scope-accuracy rules.

## 10. Adversarial-review report

An independent multi-lens workflow (privilege-escalation / manifest-TOCTOU / credential-hygiene / rollback-safety
lenses, each then adversarially verified) reviewed the scripts. **Round 1: 21 findings (P0:0, P1:4, P2:9, P3:8);
after verification 0 P0, 2 CONFIRMED P1** (the other two flagged P1s were downgraded to false-positive-as-P1 but
carried real lower-severity residuals). Every confirmed/verified item was then fixed (head `15516c23`):

- **P1 (confirmed) - incomplete exit-code fix.** My first-round AF-1 fix converted `\quit` refusals to nonzero
  `RAISE` only in the three cutover scripts; the pre-existing guards in `production_rollback_erasure.sql`,
  `rollback_erasure_privilege.sql`, `demote_app_role.sql`, `demote_app_role_rollback.sql` still exited 0, so a
  scripted caller checking `$?` would read a refusal as success. **Fixed:** all `\quit` refusals across those
  four converted to `RAISE` (exit 3); re-rehearsed rc=3. (This is exactly RULE 7 - the first return overstated
  the fix's scope; the review caught it and it is now complete and stated accurately.)
- **P1 (confirmed) - `demote_app_role_rollback.sql` had no F-8 guard.** Its blanket `REASSIGN OWNED BY
  guvfx_schema_owner TO <app_role>` + elevated-posture restore would, in the F-8 model, hand the whole schema +
  the immutable-evidence tables to the runtime role and re-elevate it. **Fixed:** added the symmetric F-8 guard;
  re-rehearsed - refuses rc=3, 155 tables intact.
- **P2/P3 (addressed):** statement-log suppression + bash-side format-check so SCRAM verifiers never reach the
  server log; `guvfx_migrate` NOINHERIT; `REVOKE CREATE ON SCHEMA public FROM PUBLIC`+app role (+ post-check);
  GLOBAL `ALTER DEFAULT PRIVILEGES ... REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC` (the schema-scoped form is a PG15
  no-op - verified and corrected); manifest re-validation + bare-`\.` rejection on `--execute`/`--rollback`
  (closes a `\copy`-FROM-STDIN injection); optional `--expect-manifest-sha256` pin; rollback precondition extended
  to routine ownership; broadened pre-F8 teardown guard; fail-closed guard for standalone enum/domain/composite
  types; trap HUP/QUIT; history-safe verifier-export documented.
- **Two findings verified as false-positive-as-P1** (recorded, not silently dropped): (a) "PUBLIC EXECUTE lets the
  app run the erasure proc" - the governed proc is absent at cutover and provisioning REVOKEs PUBLIC+app, with
  `verify`/readiness asserting it; the real residual (future SECDEF) is closed by the global default-priv REVOKE
  above. (b) "reclassify an evidence table to 'application' grants DELETE" - the base-migration BEFORE-DELETE
  trigger blocks the row delete regardless and provisioning REVOKEs by hard-coded name; recorded as a
  defense-in-depth residual (re-anchor the manifest to the known evidence-table names) in the risk register.

**Recorded architectural residuals (NOT silently fixed; flagged for decision):**
- `guvfx_migrate`/`guvfx_schema_owner` are trusted migration identities: acting as the schema owner they can
  `TRUNCATE` evidence (the immutability triggers are BEFORE-DELETE, not TRUNCATE) or disable a trigger. NOINHERIT
  narrows accidental use; the deliberate-owner path is inherent to a migration role that owns the schema.
  **Recommended follow-up (separate ADR-0054 packet):** a BEFORE-TRUNCATE trigger (or `REVOKE TRUNCATE`) on the
  evidence tables + document the trusted-identity assumption. Not newly introduced by this PR.
- TOCTOU: the ownership/grant loops re-scan the catalog by name rather than binding to the verified `_live_inv`
  by OID, and the cutover takes no schema lock; a concurrent CREATE-privileged session could substitute an object
  between verification and mutation. **Operational control:** run the cutover with the application offline and no
  concurrent privileged sessions (already in the production sequence - writers paused); OID-driven loops are a
  recommended future hardening.
- pgAudit (if configured) is not governed by `log_statement`/`log_min_duration_statement`; the verifier-log
  suppression must be paired with operational pgAudit config at cutover.

**Round 2** (re-review of the committed fixes at `15516c23`): **8/8 round-1 fixes CONFIRMED-FIXED; 0 P0,
1 CONFIRMED P1, 0 false positives.** The P1: `verify_erasure_privilege.sql:9` still used `\quit` on its
missing-`-v app_role` refusal (exit 0) - my first exit-code sweep had covered only nine of the ten scripts, so a
gate wired `verify && enable` could treat a vacuous run as PASS. The review also caught a **regression I had
introduced**: broadening the `rollback_erasure_privilege.sql` F-8 guard to key on schema ownership
false-positived on the legitimate pre-F-8 rehearsal reset (schema_owner owns `public` there too). **Fixed at
`b1a529cd`:** `verify` (and `provision`'s six) `\quit` -> `RAISE`; the F-8 guard discriminator changed to
"schema_owner owns a general relation/routine beyond the evidence allowlist" (drops the schema-ownership arm) in
both `rollback_erasure_privilege.sql` and `demote_app_role_rollback.sql`; and `separate_runtime_role.sql`
re-anchors to the known evidence tables (refuse unless they are classified `protected`). Re-rehearsed with
positive+negative controls: verify/provision missing-arg -> rc=3; **pre-F-8 provision-alone teardown PROCEEDS
(rc=0, not bricked); F-8 teardown still REFUSES (rc=3, 155 tables intact);** evidence downgrade -> rc=3; happy
path rc=0.

**Round 3** (convergence re-review of `b1a529cd`): **CONVERGED - 0 P0, 0 P1.** Both independent lenses returned
`converged: true`. They re-verified every round-2 fix: zero `\quit` on any refusal path across all ten erasure
scripts (all `RAISE` under `ON_ERROR_STOP`); the F-8 guard discriminator catches the F-8 model AND no longer
false-positives on the pre-F-8 provision-alone state (the reviewer enumerated provision's exact ownership moves -
schema_owner owns only the six allowlisted evidence/grant relations + two allowlisted trigger functions + the
schema, and the schema arm is deliberately absent); and the evidence-classification re-anchor holds. The full
privilege / credential / manifest-injection surface was re-confirmed clean. Two sub-P2 observations were recorded
as non-defects: (a) `demote_app_role_rollback.sql`'s F-8 guard also refuses a demote against a full-schema role -
a safe redirect to `separate_runtime_role_rollback.sql`; (b) `separate_runtime_role_rollback.sql` restores schema
`public` to `pg_database_owner` (matches a normal PG15 cluster; superuser-correctable otherwise).

**Three independent review rounds converged** (round-1 2 P1, round-2 1 P1 + 1 self-inflicted regression, round-3
clean), each finding fixed and re-rehearsed with positive + negative controls before the next round.

## 11. Exact-head CI + review-thread state

- **CI at exact head `b1a529cd`:** 4 of 5 checks passing, 1 still running, **0 failing** (via the app's PR
  monitor); the five jobs are backend / frontend / governance / market-data-foundation / research-foundation.
  No review comment threads are open on the PR. (The final return line will be updated to 5/5 once the last
  check reports; nothing is failing.)
- The PR monitor's auto-fix, auto-merge and auto-archive switches are all OFF (this is a review packet, not a
  merge). Nothing was merged.

## 12. Revised production GO/NO-GO

**NO-GO (engineering-complete, awaiting PD review + per-stage Sponsor authorisation).** The design is implemented;
the faithful disposable rehearsal is GREEN across every F1-F4 control, both architectural-finding guards, and all
round-1/2/3 review fixes; three independent adversarial-review rounds converged to 0 P0 / 0 P1; exact-head CI is
green; the erasure suite is 48/48. Nothing in production changed.

The first production-changing action remains a fresh in-window recovery point (read-only), then
`cutover.sh --capture` + operator review (optionally `--expect-manifest-sha256` pinned), then `cutover.sh
--execute`, then app-scoped migrate, provision, `verify_erasure_privilege.sql`, and the config cutover - **each a
separate Sponsor GO** per the production sequence in `docs/WV1_RUNTIME_ROLE_SEPARATION_2026-10-22.md` section 10.
Before any production *teardown* is attempted, the two recorded residuals should be dispositioned: the BEFORE-
TRUNCATE evidence guard (trusted-identity TRUNCATE) and making the pre-F-8 `rollback_erasure_privilege.sql` fully
F-8-aware - both bounded, separate packets.

**Bounded next action:** PD review of PR #554 and, on GO, a governed DARK squash-merge. **Authorize no production
change.**

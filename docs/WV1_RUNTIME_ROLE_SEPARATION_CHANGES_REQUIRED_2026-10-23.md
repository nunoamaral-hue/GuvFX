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
- Prior head `795efd1d`; the corrected head is the commit recorded in the Git Status footer of this packet
  (added after the adversarial review + CI items below are filled). Not self-merged.
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

## 10. Adversarial-review report

_(Filled from the independent multi-lens review — see below.)_

## 11. Exact-head CI + review-thread state

_(Filled after push: CI 5/5 at the exact head; review threads.)_

## 12. Revised production GO/NO-GO

**NO-GO.** The design is implemented and the faithful disposable rehearsal is GREEN across every F1–F4 control and
both architectural-finding guards; CI and the adversarial review are recorded below. Production awaits PD review
of the corrected PR #554 and separate, per-stage, in-window Sponsor authorisation. The first production-changing
action would be a fresh in-window recovery point (read-only), then `cutover.sh --capture` + review, then
`cutover.sh --execute` — each a separate Sponsor GO. The AF-2 residual must be closed before any production
teardown is attempted.

**Bounded next action:** PD review of PR #554; authorize no production change.

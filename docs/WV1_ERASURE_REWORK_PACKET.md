# WV1 Controlled-Erasure — Governed Review Packet (adversarial-review corrections, round 3)

- **Date:** 2026-10-21 · **PR #543** (branch `feat/wv1-controlled-erasure`). **A governed review packet, NOT a
  merge request.** No ADR ratification, no merge, no production migration/erasure/deploy, no Console change.
  MT5 Copy Trading untouched.
- **Verified this round (local, PostgreSQL):** `broker_intelligence` full suite pass; erasure + governance
  **32 pass**; no migration drift in `broker_intelligence`/`core`; the erasure CLI parses.
- **Standing boundary unchanged:** nothing provisioned, migrated, merged, flag-armed, or executed in production.

## Independent adversarial review (round 3) + fixes
A fresh independent reviewer tried to REFUTE the claims. It confirmed the headline claim — **no destructive
deletion can occur in this build** (double-guarded: readiness gate + an unconditional refuse at the end of
`execute_owner_erasure`; no row-deletion code exists in the module) — and found defects, now fixed:
- **HIGH — two-person SoD vacuous when `requested_by` is NULL.** Both `approve_request` and
  `assert_erasure_allowed` guarded the approver≠requester check with `requested_by_id is not None`, so a NULL
  requester skipped it; `--operator` was optional. **Fixed:** both gates refuse when no requester is recorded;
  `--create` now requires `--operator`. Tests: `test_approve_blocked_when_no_requester`,
  `test_execute_blocked_when_no_requester`.
- **MED — outbox completion race.** The drained-check read was not serialized by the request-row lock, so two
  workers draining the last tasks could each skip completion, wedging the request in EXECUTED. **Fixed:** the
  worker locks the `ErasureRequest` row BEFORE reading the drained state, so the last committer sees every task.
- **MED — no dead-letter.** A poison task retried forever and blocked completion. **Fixed:** a retry cap
  (`_MAX_DELETION_ATTEMPTS`) dead-letters the task (new `DeletionTask.DEAD`); a request with a DEAD task moves to
  FAILED (operator intervention), never COMPLETED. Test: `test_dead_letter_after_max_attempts`.
- **LOW — overclaims.** `executed_by` is not recorded in this (fail-closed) build and `verify_identity` had no
  status guard. **Fixed:** docstrings corrected to what is enforced; `verify_identity` refuses once APPROVED/
  EXECUTED (`test_verify_blocked_after_approval`).
The reviewer also independently confirmed SOLID: readiness-probe transaction safety (no query raises in an
unprovisioned DB; execute runs no further query after not-ready), token/TOCTOU completeness (only two PROTECT
referencers to `EvidenceBlob`, both counted; credential-ref drift covered), the provisioning-SQL role model, and
that the 32 tests are discriminating (not vacuous).

## What changed since round 2 (the PD "NO-GO FOR PROVISIONING" disposition)
The round-2 build fixed the GUC bypass (role-enforced triggers, fail-closed) but left eight blocking items. This
round fixes the ones that can be built + verified WITHOUT the superuser provisioning, and corrects the design
docs for the ones that genuinely require it. Specifically, the durable outbox + idempotent worker + two-person
lifecycle are now **built and tested** (the PD required these before provisioning); the destructive DB procedure
+ the role/ownership boundary remain **specified + scripted** for the separately-authorized rehearsal.

## Finding-by-finding disposition
| # | Finding | Status this round |
|---|---------|-------------------|
| 1 | Provisioning script references nonexistent `bi_*_block_truncate()` | **FIXED** — `ops/erasure/provision_erasure_privilege.sql` no longer references those functions (they never existed after the BEFORE-TRUNCATE design was dropped). The script now moves only the two real `block_delete` functions + the tables + sequences to the schema owner. A clean-PG15 run of the complete script is part of the DEFERRED rehearsal set (needs superuser). |
| 2 | App role still owns tables / could be granted EXECUTE | **REDESIGNED** — the script establishes a least-privilege multi-role boundary: `guvfx_schema_owner` owns tables + trigger functions; the runtime `guvfx_app` has DML only (no DELETE/TRUNCATE, no ownership, no membership, **no EXECUTE on the procedure**); a SEPARATE `guvfx_erasure_exec` login (credential NOT in the web env) is the only EXECUTE grantee; `guvfx_erasure` (NOLOGIN) owns the SECURITY DEFINER procedure + is the trigger identity. Documented in the script header + ADR-0054 §1 + the matrix below. |
| 3 | Irreversible file deletion inside the txn | **IMPLEMENTED (control plane + worker) + tested** — a durable `DeletionTask` outbox model (unique per `(request,kind,target_ref)`) + an idempotent `process_deletion_tasks()` worker that removes files **after commit**, treats a missing file as success, marks failures retryable, and advances the request to COMPLETED only when the outbox is drained. The governed procedure will enqueue tasks in the same txn as the row deletes (gated). Tests: `test_worker_removes_files_and_completes_request`, `test_worker_missing_file_is_success`, `test_worker_rerun_is_noop`. |
| 4 | Post-commit crash window / weak readiness | **IMPLEMENTED + tested** — (a) crash safety: a committed PENDING task is durable; the worker completes it on the next run (not an orphan). (b) `privileged_backend_readiness()` now verifies the whole boundary: roles exist; procedure present with expected signature, SECURITY DEFINER, owner, pinned `search_path`; EXECUTE = executor only (not PUBLIC/app); tables owned by schema owner; app role not a member of any privileged role; app role lacks DELETE/TRUNCATE — any mismatch ⇒ NOT READY (fail-closed), returned with structured reasons. Tests: `test_readiness_reports_reasons_when_unprovisioned`, `test_execute_fails_closed_when_backend_unprovisioned`. |
| 5 | Plan digest omitted observation evidence + credential drift | **IMPLEMENTED + tested** — (5a) candidate blob set is the UNION of owner BrokerEvent + BrokerMessageObservation evidence refs (`test_observation_only_blob_is_a_delete_candidate`); (5b) the TOCTOU token now includes stable sha256 digests of the owner's credential refs, so a re-auth invalidates the token even with unchanged id sets (`test_credential_ref_drift_invalidates_token`). |
| 6 | Concurrent execution | **IMPLEMENTED (control plane)** — execute takes `select_for_update` on the request + a per-subject `pg_advisory_xact_lock` + re-validates every gate under the lock. The two-competing-process end-to-end test runs in the provisioned rehearsal (DEFERRED). |
| 7 | One-boolean identity verification | **IMPLEMENTED + tested** — replaced by an auditable two-person lifecycle: distinct `requested_by` / `verified_by` / `approved_by` / `executed_by` with timestamps + a method category; `approve_request` enforces approver ≠ requester + a produced plan + verified identity + an approval expiry + revocation; execute re-checks approval validity under the lock. Tests: `test_self_approval_blocked`, `test_approve_requires_verified_identity`, `test_approve_requires_a_plan`, `test_revoked_approval_blocks`, `test_expired_approval_blocks`, `test_approver_equals_requester_blocks_exec`, `test_verify_then_plan_then_approve_happy_path`. |
| 8 | Migration/ownership role model | **DESIGNED + scripted** — dedicated NOLOGIN `guvfx_schema_owner` owns the schema; `guvfx_migrate` (its member) runs migrations; runtime/executor/read-only are separate. Deploy + rollback outline in the script. Applying the ownership move needs superuser (DEFERRED rehearsal). |
| 7-ADR | Stale "GUC" terminology in ADR | **FIXED** — removed "GUC-gated path", "GUC unset", "sets the GUC"; the remaining references only explain why a GUC was *rejected* or name the `test_old_guc_cannot_bypass` control. Added the five capability-status categories (IMPLEMENTED NOW / PROPOSED / NOT YET BUILT / REQUIRES PROVISIONING / PROHIBITED). |

## Role / privilege / ownership matrix (Findings 2 + 8)
| Role | Login | Owns | DELETE/TRUNCATE on evidence tables | EXECUTE `bi_governed_erase_owner` | Member of | Credential location |
|------|-------|------|-----------------------------------|-----------------------------------|-----------|---------------------|
| `guvfx_schema_owner` | no | evidence tables + `block_delete` fns + sequences | (owner) | no | — | n/a (NOLOGIN) |
| `guvfx_migrate` | yes | — | via membership (migrations only) | no | `guvfx_schema_owner` | deploy secret store |
| `guvfx_app` (runtime) | yes | nothing | **NO** (revoked) | **NO** | **none** | web/worker env |
| `guvfx_erasure` | no | the SECURITY DEFINER procedure | DELETE (as trigger identity) | (owner) | — | n/a (NOLOGIN) |
| `guvfx_erasure_exec` | yes | — | no | **yes (only grantee)** | none | governed-job secret store, **NOT** web env |
| `guvfx_readonly` | yes | — | no | no | — | ops secret store |

## Readiness invariants re-checked before any execute (fail-closed)
roles exist · procedure present · expected signature `(bigint, bytea)` · SECURITY DEFINER · owned by
`guvfx_erasure` · pinned `search_path` · EXECUTE = `guvfx_erasure_exec` only (not PUBLIC, not app) · evidence
tables owned by `guvfx_schema_owner` · app role not a member of any privileged role · app role lacks
DELETE+TRUNCATE. Any failure ⇒ `privileged_backend_ready()` is False and execute refuses with sanitised reasons.

## Build vs gated (honest split)
- **IN THE BRANCH, TESTED (local + CI):** role-enforced immutability triggers; the fail-closed control plane;
  the enhanced plan + token (observation union + credential-ref drift); the two-person SoD lifecycle; the durable
  `DeletionTask` outbox + idempotent worker; the comprehensive readiness probe; 28 discriminating tests.
- **NOT YET BUILT (design only; nothing deletes):** the physical-deletion step driven from inside the governed
  procedure; audit-row anonymisation applied to live rows (classified in `WV1_AUDIT_PII_INVENTORY.md`).
- **REQUIRES PRIVILEGED PROVISIONING (superuser, Sponsor-authorized, rehearsal first):** the role/ownership
  boundary + the `bi_governed_erase_owner` procedure body, and the DB role-integration test set below.

## DEFERRED PostgreSQL-15 role-integration tests (need superuser; run in the rehearsal)
provisioning runs clean + is idempotent · governed procedure is the ONLY delete path · app role + PUBLIC cannot
EXECUTE it · only `guvfx_erasure_exec` can · spoofed caller gains no authority · TRUNCATE blocked by revocation ·
trigger DISABLE/DROP denied to the app role · two competing executes → one terminal outcome · crash → durable
PENDING task → worker completes · shared evidence retained · observation-only included · credential drift
invalidates · audit PII anonymised in-procedure · future migrations still operational.

## Updated production-readiness (honest)
- Erasure **capability ~55%**: control plane + security boundary design + outbox/worker + two-person lifecycle +
  readiness are built and tested; the destructive DB procedure + ownership provisioning + audit anonymisation are
  specified/scripted but unbuilt/unprovisioned, so **erasure is not yet operable**.
- WV1 engineering overall unchanged (~95% feature; erasure is additive and DARK).
- Production-readiness **~55%** (the erasure gap + prior recovery / log-retention / DPA / DPIA gaps). Live-user
  availability **0%**.

## Approval requests (unchanged)
- Review the corrected design (this packet + ADR-0054 + the provisioning script).
- Authorize (separately) the **superuser provisioning in a REHEARSAL database** so the destructive procedure +
  the role-integration test set can be built and run.
- Keep PR #543 **DRAFT/DARK**, unratified, unmerged; do not arm the flag or run `--execute` against production.

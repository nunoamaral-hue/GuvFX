# WV1 Controlled-Erasure — Governed Review Packet

- **Date:** 2026-10-22 · **PR #543** (branch `feat/wv1-controlled-erasure`). **A governed review packet, NOT a
  merge request.** No ADR ratification, no merge, no production provisioning/migration/erasure/deploy, no Console
  change. MT5 Copy Trading untouched.
- **Standing boundary unchanged:** nothing provisioned, migrated, merged, flag-armed, or executed **in
  production**. The only place destructive deletion ran is a **disposable PostgreSQL-15 rehearsal with synthetic
  data**, destroyed on exit.

## Round 5 (2026-10-22) — P0 closure (CHANGES REQUIRED from the round-4 disposition)
The PD accepted the round-4 rehearsal evidence but returned CHANGES REQUIRED on two P0 correctness items. Both
are fixed and re-rehearsed green (`ALL EVIDENCE PASS`; see `ops/erasure/REHEARSAL_EVIDENCE.md`):
- **P0 — executor identity was forgeable.** The procedure no longer accepts an executor-id argument
  (`bi_governed_erase_owner(bigint, text)`, v2). The executor is resolved from the DB-authenticated `session_user`
  via a single-use `ErasureExecutionGrant` (mig 0021), must equal the request's `assigned_executor`, and must
  differ from the approver. `guvfx_erasure_exec` is now a NOLOGIN group; per-operator short-lived member logins +
  grants are minted by an independent process; the grant table is owned off the app role. Rehearsal proves
  no-grant / expired / revoked / wrong-operator / single-use-consumed / executor≠approver refusals + that
  `executed_by` is the DB-authenticated operator (steps 12, 30–33, 06-cannot-touch-grants).
- **P0 — post-erasure ingestion race.** A shared per-owner advisory lock (`erasure_lock.acquire_owner_erasure_lock`,
  ns 26485) is taken by EVERY owner-scoped WI writer (ingestion, correlation, mailbox connect, identity/contact);
  ingestion re-reads the mailbox under the lock and refuses a stale write (removed / REVOKED / owner-mismatch /
  `generation` bumped, mig 0022). The procedure takes the lock first + deletes the mailbox in-transaction.
  Rehearsal steps 40–42.
- Also: separate PRODUCTION rollback runbook (`ops/erasure/production_rollback_erasure.sql`) that disables the
  capability while PRESERVING the secure ownership boundary (distinct from the disposable-rehearsal teardown).
- Full backend regression re-run on the reconciled head; branch reconciled with canonical main.

## Round 4 (2026-10-22) — Phase 1–3 under the PD conditional authorisation
The PD authorised: (1) correct PR metadata + ADR/packet contradictions; (2) complete the implementation WITHOUT
production provisioning; (3) a **disposable PG15 rehearsal, synthetic data only**. All three are done.

- **Phase 1:** PR #543 title + description corrected to ADR-0054 and the role-based design (states destructive
  deletion remains unimplemented/unprovisioned in production); ADR category contradiction fixed.
- **Phase 2 (implemented, locally tested):** the dedicated **executor DATABASES alias** (Finding C,
  fail-closed when absent); the **comprehensive readiness probe** (Finding D — schema/overload/signature/owner/
  SECURITY DEFINER/exact search_path/version marker/ACL-grantees/ownership/triggers/migrations); the full
  **two-person SoD** with a verification-method allowlist + `ErasureRequest` CHECK constraints (Findings A/B); a
  complete, guarded, idempotent, transactional **provisioning script + the SECURITY DEFINER procedure body** +
  **verification** + **rollback** scripts (Finding F); **audit anonymisation** in the procedure + the
  migration-0020 carve-out (Finding G). Ordinary full regression stayed green before any role creation.
- **Phase 3 (disposable PG15 rehearsal — `ops/erasure/REHEARSAL_EVIDENCE.md`, `ALL EVIDENCE PASS`):** 22 steps,
  incl. unprovisioned negative control, provision + verify, app/PUBLIC cannot EXECUTE/TRUNCATE/DDL, real
  governed execution, shared-evidence retention, observation-only handling, outbox durability + worker drain,
  audit anonymisation, dead-letter → FAILED, concurrency one-winner, plan-token drift + hold + executor=approver
  refusals, and a clean rollback restoring the original arrangement. Container destroyed; no secrets logged.

- **Independent adversarial review (round 4):** a fresh reviewer attacked the backend and confirmed the boundary,
  SECURITY-DEFINER search_path safety, proc shared-blob/token/SoD/carve-out logic, and fail-closed behaviour are
  SOLID. It found one HIGH (`DROP SCHEMA public CASCADE` owner-level vector), two MED (the contact-binding
  PROTECT FK; a narrow plan→delete ingestion race), and minor LOW hardening. **All confirmed items are fixed and
  the rehearsal re-ran green** — see `ops/erasure/REHEARSAL_EVIDENCE.md` (steps `06-app-cannot-drop-schema`,
  `03-contact-binding-seeded`/`11 contacts:1`, `20-future-migrations-work`) and ADR-0054 round-4 status.

### Finding A–G disposition (this round)
| # | Finding | Status |
|---|---------|--------|
| A | Lifecycle authority forgeable in app data | **FIXED** — complete verification record required + re-checked at execute; DB CHECK constraints block an unverified/method-less advanced state; the procedure re-derives + re-checks from DB rows. |
| B | SoD overstated | **FIXED** — exact enforced policy (requester/verifier/approver pairwise-distinct; executor ≠ approver; executor authority from the executor connection) stated in ADR-0054 and encoded in code + tests + procedure. |
| C | No executor connection | **IMPLEMENTED** — `erasure_executor` alias; credential absent in web/worker/CI/local → fail-closed; rehearsal proves app connection cannot call the proc, executor can. |
| D | Readiness not comprehensive | **IMPLEMENTED** — pinned schema + single overload (ambiguous ⇒ NOT READY) + signature via `proargtypes` + owner + SECURITY DEFINER + exact search_path + version marker + ACL enumeration + ownership + triggers + migrations. |
| E | Doc contradictions | **FIXED** — ADR reconciled into five status categories; worker-vs-procedure distinction corrected. |
| F | Provisioning was a template | **IMPLEMENTED** — runnable, guarded (target-db + ack + synthetic), idempotent, transactional; full procedure body; `verify_*` + `rollback_*` scripts; all ran in the rehearsal. |
| G | Audit anonymisation unimplemented | **IMPLEMENTED** — procedure anonymises `BrokerEmailIdentityAudit`/`TradingAccountBrokerContactAudit`/`core.AuditEvent` (email/proofs/ip/user_agent/metadata cleared, subject/links nulled) while keeping accountability facts; migration 0020 carves out the append-only UPDATE trigger for the governed path only. |

### Round-3 record (retained below)
- **Verified round 3 (local, PostgreSQL):** `broker_intelligence` full suite pass; erasure + governance
  **32 pass**; no migration drift; the erasure CLI parses.

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
roles exist · procedure present · expected signature `(bigint, text)` · SECURITY DEFINER · owned by
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
- Erasure **engineering ~90%**: the full capability — control plane, two-person SoD, outbox/worker/dead-letter,
  readiness probe, the SECURITY DEFINER procedure, the six-role boundary, provisioning/verify/rollback, and audit
  anonymisation — is **built, locally tested, and certified in a disposable PG15 rehearsal**. Remaining
  engineering: the AuditEvent operational-vs-security retention split and record-level holds (documented future
  refinements, not required for owner erasure).
- Erasure **production-operability 0%**: it is **not operable in production** — production provisioning, the ADR
  ratification, arming the flag, and deploying the executor credential are all Sponsor-gated and NOT done.
- WV1 engineering overall ~95% feature (erasure is additive and DARK). Overall WV1 production-readiness still
  gated by the prior recovery / log-retention / DPA / DPIA items. Live-user availability **0%**.

## Approval requests
- Review the implemented design + the rehearsal evidence (this packet + ADR-0054 + `ops/erasure/`).
- Decide on ratifying ADR-0054 and (separately) authorising production provisioning + flag-arming — both remain
  your gate; nothing here presumes them.
- Keep PR #543 **DRAFT/DARK**, unratified, unmerged; do not arm the flag or run `--execute` against production.

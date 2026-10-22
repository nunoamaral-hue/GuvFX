# Controlled-Erasure — Disposable PostgreSQL-15 Rehearsal Evidence (ADR-0054)

- **Date:** 2026-10-22 · Driver: `ops/erasure/rehearse_erasure.sh` · **Synthetic data only; disposable
  container (`postgres:15`), newly-generated disposable credentials, no prod connectivity, no NAS, no OAuth, no
  prod env file; the container is destroyed on exit.** No production system was touched.
- Bootstrap: app role created LOGIN SUPERUSER only to run `migrate`, then `ALTER ROLE guvfx_app NOSUPERUSER`
  before provisioning, so the boundary is exercised against a NON-superuser application role.
- Exit: `REHEARSAL: ALL EVIDENCE PASS`. Evidence log scanned — no secret material (0 matches for
  password/secret). Passwords live only in shell vars, never echoed or logged.

## Sequence + results (each line emitted by the harness as `EVIDENCE|step|PASS|detail`)
| Step | Result | Detail |
|------|--------|--------|
| 01 unprovisioned negative control | PASS | `privileged_backend_readiness`=NOT READY (16 reasons) before provisioning |
| 02 ordinary delete blocked | PASS | role-enforced trigger: "EvidenceBlob is immutable raw evidence; DELETE is not allowed" |
| 03 contact binding seeded | PASS | a subject `TradingAccountBrokerContact` (its PROTECT FK blocks naive identity deletion) |
| 03 governed lifecycle | PASS | create→verify→plan→approve produced APPROVED requests (main/hold/drift/poison) |
| PROVISION | PASS | `provision_erasure_privilege.sql` ran clean in one transaction (incl. schema-ownership move) |
| VERIFY | PASS | `verify_erasure_privilege.sql`: "VERIFY OK: …satisfies every invariant" |
| 20 future migrations work | PASS | the `guvfx_migrate` role can CREATE/DROP post-provision (schema owned by `guvfx_schema_owner`) |
| 04 executor alias | PASS | `erasure_executor` alias present (dedicated `guvfx_erasure_exec` connection) |
| 05 readiness READY | PASS | full probe over the executor connection: READY |
| 06 app cannot call proc | PASS | `permission denied for function bi_governed_erase_owner` (ordinary app connection) |
| 06 app cannot TRUNCATE | PASS | `permission denied for table broker_intelligence_evidenceblob` |
| 06 app cannot DROP TRIGGER | PASS | `must be owner of relation broker_intelligence_evidenceblob` |
| 06 app cannot DROP SCHEMA | PASS | `must be owner of schema public` (closes the `DROP SCHEMA public CASCADE` owner-level vector) |
| 08 active hold refused | PASS | "an active legal/security hold blocks erasure" |
| 09 plan-token drift refused | PASS | state changed after plan → "owner state changed since the plan was produced (TOCTOU)" |
| 10 executor == approver refused | PASS | "two-person rule: the executor must differ from the approver" |
| 11 governed execution | PASS | status→EXECUTED; outcome: withdrawals 1, observations 1, broker_events 2, evidence_blobs_deleted 2, connected_mailboxes 1, identities 1, **contacts 1**, deletion_tasks 3 |
| 12 shared blob retained | PASS | the blob referenced by another owner survives |
| 13 subject-only blob deleted | PASS | |
| 14 observation-only blob deleted | PASS | (union: a blob referenced only by the owner's observation) |
| 15 other owner untouched | PASS | the co-owner's events + audit rows intact |
| 16 outbox durable before worker | PASS | files still present, tasks PENDING, request EXECUTED (crash-safe: no orphan deletion inside the txn) |
| 17 worker drains + completes | PASS | files removed post-commit; request → COMPLETED (worker: completed 3, failed 0, dead 0) |
| 18 audit PII anonymised | PASS | subject email row gone, subject AuditEvent user nulled, contact-audit row kept but anonymised (proofs `{}`, links nulled) |
| 19 re-execute refused | PASS | an already-COMPLETED request is not executable (idempotency) |
| 20 dead-letter + FAILED | PASS | a poison credential-file task retried to the cap → DEAD; request → FAILED (needs intervention, never COMPLETED) |
| 21 concurrency one winner | PASS | two threads over the executor connection → exactly one EXECUTED, the other refused ("request not APPROVED") |
| 22 rollback proc gone | PASS | `rollback_erasure_privilege.sql`: procedure dropped |
| 22 rollback ownership restored | PASS | evidence table + schema `public` owned by `guvfx_app` again (original arrangement) |

## Round-4 independent adversarial review — findings fixed, then re-rehearsed green
An independent reviewer attacked the round-4 backend and found one HIGH + two MED + minor LOW items; all confirmed items are fixed and the above rehearsal re-ran green with the fixes:
- **HIGH** — app role could `DROP SCHEMA public CASCADE` (owner-level): provisioning now moves schema ownership to `guvfx_schema_owner` + revokes CREATE; readiness + verify assert it; step `06-app-cannot-drop-schema` proves it.
- **MED** — a subject's `TradingAccountBrokerContact` binding (PROTECT FK) aborted erasure: the procedure now deletes it (owner-scoped) before identities; seeded + proven at step 11 (`contacts: 1`).
- **MED** — plan→delete ingestion race: the deletable-blob set is now derived at delete time via `RETURNING` (residual documented in ADR-0054 as a production-arming follow-up: ingestion to take the subject advisory lock / mailbox-disconnect precondition).
- **LOW** — worker re-ingestion guard (don't unlink a file a live blob re-references), exact migration-name pins, trigger O/A firing check, `public.digest` qualification, executor `current_user` assertion, executor-id recorded as self-asserted.

## What this proves (and does not)
- The provisioned privilege boundary and the governed SECURITY DEFINER procedure behave as designed against
  synthetic data: the only path that deletes immutable evidence is the procedure, invoked only over the
  dedicated executor connection; the application role cannot delete, truncate, DDL the triggers, or call the
  procedure. Deletion is owner-scoped, shared evidence is retained, physical files are removed only via the
  durable post-commit outbox, audit PII is anonymised while accountability facts are preserved, and the
  lifecycle (two-person SoD, hold, approval validity, plan-token drift, dead-letter, concurrency) holds.
- It does NOT authorise production: nothing here ran against production, staging-with-real-data, NAS, live
  credentials, or a persistent shared database; the disposable environment was destroyed.

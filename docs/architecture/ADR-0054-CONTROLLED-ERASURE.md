# ADR-0054 — Controlled owner-scoped erasure exception to immutable evidence

- **Status:** **Proposed** (NOT ratified — requires Sponsor approval before activation).
- **Date:** 2026-10-21 · **Scope:** Withdrawal Intelligence (`broker_intelligence`) only.
- **Context docs:** `WV1_RETENTION_DELETION_SPEC.md`, `.claude/rules/data.md` (immutability invariant).

## Context
`broker_intelligence` is immutable by design (data rule "quarantine, don't destroy"): raw broker-email
evidence (`EvidenceBlob`) and `BrokerEvent` can be deleted by **no** path — Python `delete()` overrides
raise and DB BEFORE-DELETE triggers (migration 0002) block ORM bulk/raw SQL too; audit tables are
append-only. This conflicts with the GDPR/UK-GDPR Art 17 right to erasure, which a public "delete on
request" promise requires. We need a controlled erasure path **without** weakening ordinary immutability.

## Decision
Introduce a single **governed erasure path** that is the ONLY way a data subject's WV1 personal data can
be removed, gated so ordinary immutability is unchanged everywhere else.

1. **Role-enforced DB triggers (NOT a GUC — corrected per adversarial review Finding 2).** The
   `EvidenceBlob`/`BrokerEvent` BEFORE-DELETE trigger functions (migration 0017) permit a delete **only**
   when the deleting session's `current_user` is the dedicated NOLOGIN role **`guvfx_erasure`**. A session
   GUC was **rejected** because any session on the ordinary application role can `SET`/`SET LOCAL` it — it is
   not proof the delete came through the governed path. `current_user` can become `guvfx_erasure` **only**
   via a SECURITY DEFINER procedure owned by that role; the ordinary application role is not a member and
   cannot `SET ROLE` to it. The privileged role + procedure are **provisioned separately by a superuser**
   The migration (app-role-safe) blocks **every ordinary DML DELETE** (ORM/bulk/raw). It is **not, by itself, a
   complete boundary against a fully-compromised app role**, which still OWNS these tables + trigger functions and
   could **TRUNCATE** (row-level DELETE triggers do not cover TRUNCATE) or strip the triggers via DDL (DISABLE/DROP
   trigger, CREATE OR REPLACE the function). Those **owner-level** vectors are closed by the **separate superuser
   provisioning** (`ops/erasure/provision_erasure_privilege.sql`), which moves object **ownership** off the app
   role and REVOKEs DELETE/TRUNCATE. (A BEFORE-TRUNCATE trigger was considered and rejected: an owner bypasses any
   trigger pre-provisioning, it adds nothing the provisioning REVOKE does not, and it breaks Django's legitimate
   owner-level test-flush.) Until `guvfx_erasure` exists, no governed delete is possible either — fail-closed.
   Proved by `test_ordinary_delete_blocked` and `test_old_guc_cannot_bypass`.
2. **Data-subject identity verification.** An explicit `ErasureRequest` with `identity_verified = True`
   is required (operator attests out-of-band verification). Unverified ⇒ refused.
3. **Owner-scoped authorization.** Erasure acts on exactly ONE owner (`subject_user_id_snapshot`); it
   never ranges over multiple owners. All deletes are `filter(owner=…)`/`filter(user=…)` scoped.
4. **Evidence-reference removal.** The owner's `Withdrawal`, `BrokerMessageObservation`, `BrokerEvent`,
   `ConnectedMailbox`, `BrokerEmailIdentity` rows are removed (in an order that releases PROTECT refs).
5. **Shared-object protection.** A physical blob is deletable only when **no lawful reference remains**:
   the row delete is attempted and a `ProtectedError` (another owner's event/observation still PROTECTs
   it) means **retain**. No reference-count field is relied upon — PROTECT is the source of truth.
6. **Physical blob/credential deletion via a durable outbox (corrected per Findings 3/4).** Irreversible
   filesystem deletion (credential + evidence files) is **never** done inside the DB transaction. The
   governed procedure deletes the DB rows and, in the **same transaction**, writes durable `DeletionTask`
   outbox rows (PENDING); an **idempotent worker** removes the files **after commit** and marks each task
   COMPLETED/FAILED. A crash after commit leaves a **durable PENDING task** (not an orphan file); the request
   is not COMPLETED until all file-deletion tasks finish. A rollback leaves DB + files consistent.
7. **Derived BrokerEvent handling.** Deleted (owner-scoped) via the role-enforced governed path (inside the
   SECURITY DEFINER procedure running as `guvfx_erasure`), after its Withdrawals (PROTECT) and observations
   are gone.
8. **Withdrawal-record handling.** Deleted (owner-scoped); it is a rebuildable projection.
9. **Credential & mailbox deletion.** The encrypted token file is destroyed (`credential_store.delete_token`)
   and the `ConnectedMailbox` row deleted.
10. **Audit metadata without deleted content.** The `ErasureRequest` records who/when/scope/counts/outcome
    — **never** message content (by construction: only counts/ids live in its JSON plan/outcome). The
    end-to-end audit-content assertion runs in the provisioned rehearsal (gated). The append-only
    identity/contact audit tables (which hold no message body) are **retained** (SET_NULL back-refs); the
    personal-data fields they DO hold are classified for anonymisation in `WV1_AUDIT_PII_INVENTORY.md`.
11. **Legal & security holds.** An active `ErasureHold` for the subject blocks erasure (status HELD).
    Owner-level in v1; record-level holds are a documented future refinement.
12. **Backup-expiry handling.** Encrypted backups are immutable snapshots; erased data persists in them
    until they expire (the retention schedule states the max residual window) and erasure must be
    re-applied to any restored copy. (No production backup exists yet.)
13. **Failure recovery.** The mutation is atomic (all-or-nothing); a failure rolls the DB back and records
    a sanitised FAILED status **outside** the rolled-back transaction. Physical files are untouched on failure
    (no outbox task is drained until after commit). The rollback end-to-end test runs in the provisioned
    rehearsal (gated).
14. **Idempotency.** An already-EXECUTED/COMPLETED request is refused (status gate); a fresh request for a
    now-empty owner is a zero-count no-op; the outbox worker is idempotent (missing file ⇒ success,
    `test_worker_missing_file_is_success`, `test_worker_rerun_is_noop`). The end-to-end repeat-execution test
    runs in the provisioned rehearsal (gated).
15. **Concurrency / TOCTOU.** The dry-run produces a confirmation token (digest of the owner-scoped object
    set); execute recomputes it from live state and requires a match, so a state change between plan and
    execute aborts (`test_wrong_token_blocks`).
16. **Operator permissions.** Staff/governance CLI only (`erase_broker_intelligence_owner`), DARK by
    default (`BROKER_INTELLIGENCE_ERASURE_ENABLED`), with a dry-run plan + a second confirmation. **Never**
    an HTTP/customer endpoint.
17. **Separation of ordinary deletion vs governed erasure, and no mass/cross-user deletion.** Ordinary app
    code can never delete evidence/events — the role-enforced BEFORE-DELETE triggers block every ordinary
    DML DELETE (ORM/bulk/raw), because the app role is not `guvfx_erasure`. The governed path requires the
    flag + a verified request + a matching token + no hold, acts on exactly one owner, and runs its deletes
    only inside the SECURITY DEFINER procedure (where `current_user` is `guvfx_erasure`).

## Consequences
- The immutability invariant is preserved for all non-governed code; a narrow, audited, flag-gated,
  double-confirmed exception exists for Art 17.
- "Delete on request" becomes truthful **only once this ADR is ratified and the capability is armed** for
  the relevant cohort; until then the privacy wording must use the interim (Option C) language.
- Two additive migrations: **0017** adds `ErasureRequest`/`ErasureHold` and makes the two trigger functions
  **role-enforced** (`current_user = 'guvfx_erasure'`); **0018** adds the two-person lifecycle fields +
  the `DeletionTask` outbox. Both reverse cleanly (0017's reverse restores the unconditional-raise bodies). No
  data backfill. Neither has been applied to production.

## Post-adversarial-review status (2026-10-21, round 3)
This ADR and PR #543 were **not approved** in their first or second form. Corrections now in the branch:
- **Finding 1:** renumbered 0051 → **0054** (0051/0052/0053 are the MT5 copier's); governance test added. The
  provisioning script's nonexistent `bi_*_block_truncate()` references are removed.
- **Finding 2/8:** the app role no longer owns the evidence objects in the target design — a least-privilege
  role boundary (`guvfx_schema_owner`, `guvfx_migrate`, `guvfx_app`, `guvfx_erasure`, `guvfx_erasure_exec`,
  `guvfx_readonly`) is scripted; the app role has no DELETE/TRUNCATE, no membership, and no EXECUTE on the
  procedure; a separate executor login (credential off the web runtime) is the sole EXECUTE grantee.
- **Findings 3/4/5/6/7 (control plane):** **built and locally tested** — the durable `DeletionTask` outbox +
  idempotent worker + dead-letter, the comprehensive readiness probe, the observation-union + credential-ref-drift
  token, the per-subject concurrency locks, the two-person SoD lifecycle + verification-method allowlist, and the
  `ErasureRequest` CHECK constraints.

## Round-4 status (2026-10-22 — Phase 2 implementation + Phase 3 disposable rehearsal)
Under the PD's conditional authorisation, the destructive backend is now implemented and exercised in a
**disposable PostgreSQL-15 rehearsal with synthetic data only** (`ops/erasure/REHEARSAL_EVIDENCE.md`;
`ALL EVIDENCE PASS`):
- **Finding A** (forgeable lifecycle) — the complete verification record (verifier + timestamp + allow-listed
  method) is required and re-checked at execution; `ErasureRequest` CHECK constraints make an unverified/
  method-less advanced state impossible to persist.
- **Finding B** (overstated SoD) — the enforced policy is stated above and encoded in code + tests + the procedure.
- **Finding C** (no executor connection) — a dedicated `erasure_executor` DATABASES alias (credential absent in
  web/worker/CI/local → fail-closed) is the only way the procedure is invoked; the rehearsal proves the app
  connection cannot call it and the executor connection can.
- **Finding D** (weak readiness) — the probe pins schema, single overload, exact signature/owner/SECURITY
  DEFINER/search_path/version marker, ACL (executor-only, no PUBLIC/app, no unexpected grantees), table +
  sequence + trigger-function ownership, trigger install/enabled, app membership + DELETE/TRUNCATE exclusions,
  and required migrations.
- **Finding E** (doc contradictions) — reconciled into the five categories above.
- **Finding F** (template, not runnable) — `provision_erasure_privilege.sql` is a complete, guarded, idempotent,
  transactional script with the full procedure; `verify_*` and `rollback_*` scripts added; all three ran in the
  rehearsal.
- **Finding G** (audit anonymisation) — implemented in the procedure + the migration-0020 carve-out; the
  rehearsal proves subject PII is removed while accountability facts + the row are preserved, and other users are
  untouched.
`execute_owner_erasure` is **fail-closed in every unprovisioned/unconfigured environment** (web, workers, CI,
local, and production until Sponsor-authorised provisioning). It performs real deletion ONLY where the flag is
on, the executor connection is configured, and the full readiness probe passes — which in this programme means
the disposable rehearsal, and (only after a separate Sponsor GO) production.
**Still PROHIBITED without that per-step Sponsor authorisation:** production provisioning, production migration,
arming the flag, processing a real user, deploying the executor credential to any runtime, and PR merge / ADR
ratification. See `docs/WV1_ERASURE_REWORK_PACKET.md`.

## Separation-of-duties policy (Finding B — exactly what the code enforces)
Four roles are recorded (requester, verifier, approver, executor) and the following distinctness is ENFORCED, in
the Python control plane AND re-checked in the SECURITY DEFINER procedure at execution:
- **requester** recorded (`requested_by`), else nothing may be approved or executed;
- **verifier** recorded (`verified_by` + `verified_at` + a non-empty, allow-listed `verification_method`) and
  **verifier ≠ requester**; verification can only be set before approval;
- **approver** recorded (`approved_by` + `approved_at`, not revoked, not expired) and **approver ≠ requester**
  and **approver ≠ verifier**;
- **executor** recorded and **executor ≠ approver**; the executor's *authority* comes from the dedicated
  `guvfx_erasure_exec` connection (the ordinary app role cannot invoke the procedure at all), not from a
  caller-supplied flag.
So no single operator can perform requester + approver, verifier + approver, or approver + executor — the
approver is distinct from every other role. (We deliberately do NOT claim executor ≠ verifier or ≠ requester,
because the code does not enforce those.)

## Capability status — five categories (what is true right now)
Every element is placed in exactly one category, so no reader mistakes a design for a shipped control.

- **Implemented and LOCALLY TESTED (branch + CI unit tests):** role-enforced BEFORE-DELETE triggers; the
  fail-closed control plane (pure dry-run PLAN with the event+observation union and credential-ref-drift token;
  the full lifecycle gate); the two-person SoD lifecycle + the verification-method allowlist; the durable
  `DeletionTask` outbox model + the idempotent worker + dead-letter; the comprehensive readiness probe; the
  `ErasureRequest` CHECK constraints; the executor-connection fail-closed wiring. Negative controls prove each.
- **Implemented and REHEARSAL-INTEGRATION-TESTED (disposable PostgreSQL-15, synthetic data — see
  `ops/erasure/REHEARSAL_EVIDENCE.md`):** the `bi_governed_erase_owner` SECURITY DEFINER procedure (owner-scoped
  deletes, outbox enqueue, audit anonymisation, all gates re-derived from DB state); the runnable
  provisioning / verification / rollback scripts; the audit-anonymisation carve-out (migration 0020); the
  six-role boundary with the ownership move + DELETE/TRUNCATE revocations; the executor connection. Proven: only
  the procedure deletes; app + PUBLIC cannot EXECUTE/TRUNCATE/DDL; concurrency yields one outcome; crash leaves a
  durable task; shared evidence retained; files removed only via the outbox; audit PII anonymised.
- **DESIGNED but not implemented (production-arming follow-ups; none change the fail-closed stance):**
  - **Ingestion serialisation (adversarial-review MED residual).** The procedure derives its deletable-blob set
    from live rows at delete time (via `RETURNING`), closing the plan→delete window. A row the ingestion worker
    commits for the subject AFTER the procedure's delete statements is new data that survives. Full closure
    requires ingestion to take the same `pg_advisory_xact_lock(26485, owner)`, or a mailbox-disconnect
    precondition before erasure. Required before production arming; ingestion is DARK today.
  - **Executor attribution (adversarial-review LOW).** `executed_by` is a caller-supplied staff id (recorded as
    `executor_id_self_asserted` in the outcome); the real execution *authority* is possession of the
    `guvfx_erasure_exec` connection. Binding `executed_by` to an authenticated operator record is a follow-up.
  - The operational-vs-security retention split for `core.AuditEvent`; record-level (vs owner-level) legal
    holds. Neither is required for owner erasure.
- **REQUIRES PRIVILEGED PROVISIONING IN PRODUCTION (superuser, separately Sponsor-authorised):** running
  `ops/erasure/provision_erasure_privilege.sql` against production, creating the roles there, moving ownership,
  and deploying the `guvfx_erasure_exec` credential into the governed job's environment. Until done,
  `privileged_backend_ready()` is false in production and `execute` fails closed.
- **PROHIBITED (no authorisation sought here):** merging PR #543, ratifying this ADR, running the provisioning
  or any migration against production, arming `BROKER_INTELLIGENCE_ERASURE_ENABLED`, processing any real user,
  deploying the executor credential to the web/worker runtime, and `--execute` against real data.

## Status / ratification
**Proposed — NOT ratified; PR #543 is a governed review packet, not a merge request.** Do not merge, do not
activate (`BROKER_INTELLIGENCE_ERASURE_ENABLED`), do not provision the privileged role/procedure, and do not
run `--execute` against production data, without explicit Sponsor authorization for that specific step.

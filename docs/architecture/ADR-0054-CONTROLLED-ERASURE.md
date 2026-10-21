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
7. **Derived BrokerEvent handling.** Deleted (owner-scoped) via the GUC-gated path, after its Withdrawals
   (PROTECT) and observations are gone.
8. **Withdrawal-record handling.** Deleted (owner-scoped); it is a rebuildable projection.
9. **Credential & mailbox deletion.** The encrypted token file is destroyed (`credential_store.delete_token`)
   and the `ConnectedMailbox` row deleted.
10. **Audit metadata without deleted content.** The `ErasureRequest` records who/when/scope/counts/outcome
    — **never** message content (proved by `test_audit_record_has_no_deleted_content`). The append-only
    identity/contact audit tables (which hold no message body) are **retained** (SET_NULL back-refs).
11. **Legal & security holds.** An active `ErasureHold` for the subject blocks erasure (status HELD).
    Owner-level in v1; record-level holds are a documented future refinement.
12. **Backup-expiry handling.** Encrypted backups are immutable snapshots; erased data persists in them
    until they expire (the retention schedule states the max residual window) and erasure must be
    re-applied to any restored copy. (No production backup exists yet.)
13. **Failure recovery.** The mutation is atomic (all-or-nothing); a failure rolls the DB back and records
    a sanitised FAILED status **outside** the rolled-back transaction (proved by
    `test_partial_failure_is_recoverable`). Physical files are untouched on failure.
14. **Idempotency.** An already-EXECUTED request is refused; a fresh request for a now-empty owner is a
    zero-count no-op (proved by `test_repeated_execution_is_safe`).
15. **Concurrency / TOCTOU.** The dry-run produces a confirmation token (digest of the owner-scoped object
    set); execute recomputes it from live state and requires a match, so a state change between plan and
    execute aborts (`test_wrong_token_blocks`).
16. **Operator permissions.** Staff/governance CLI only (`erase_broker_intelligence_owner`), DARK by
    default (`BROKER_INTELLIGENCE_ERASURE_ENABLED`), with a dry-run plan + a second confirmation. **Never**
    an HTTP/customer endpoint.
17. **Separation of ordinary deletion vs governed erasure, and no mass/cross-user deletion.** Ordinary app
    code can never delete evidence/events (GUC unset). The governed path requires the flag + a verified
    request + a matching token + no hold, acts on exactly one owner, and sets the GUC only around its own
    deletes.

## Consequences
- The immutability invariant is preserved for all non-governed code; a narrow, audited, flag-gated,
  double-confirmed exception exists for Art 17.
- "Delete on request" becomes truthful **only once this ADR is ratified and the capability is armed** for
  the relevant cohort; until then the privacy wording must use the interim (Option C) language.
- A small migration (0017) adds `ErasureRequest`/`ErasureHold` and makes the two trigger functions
  **role-enforced** (`current_user = 'guvfx_erasure'`) — additive; reverse restores the unconditional-raise
  trigger bodies. No data backfill.

## Post-adversarial-review status (2026-10-21)
This ADR and PR #543 were **not approved** in their first form. Corrections now in the branch:
- **Finding 1:** renumbered 0051 → **0054** (0051/0052/0053 are the MT5 copier's); governance test added.
- **Finding 2:** GUC bypass removed → **role-enforced** triggers, fail-closed until the superuser-provisioned
  privileged backend exists; negative controls prove it.
- The destructive deletion path is **fail-closed in this build** (execute refuses — no deletion occurs).
**Specified but gated on the privileged provisioning (next increment):** the SECURITY DEFINER procedure body,
the **durable deletion-outbox + idempotent worker** (Findings 3/4), audit-row anonymisation (Finding 8), and
the **identity-verification separation of duties** / two-person approval (Finding 7). See
`docs/WV1_ERASURE_REWORK_PACKET.md`.

## Status / ratification
**Proposed — NOT ratified; PR #543 is a governed review packet, not a merge request.** Do not merge, do not
activate (`BROKER_INTELLIGENCE_ERASURE_ENABLED`), do not provision the privileged role/procedure, and do not
run `--execute` against production data, without explicit Sponsor authorization for that specific step.

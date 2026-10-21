# ADR-0051 — Controlled owner-scoped erasure exception to immutable evidence

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

1. **GUC-gated DB triggers.** The `EvidenceBlob`/`BrokerEvent` BEFORE-DELETE trigger functions are
   replaced (migration 0017) to permit a delete **only** when the per-session GUC
   `guvfx.erasure_authorized = 'on'`. The GUC is set **exclusively** inside the governed transaction
   (`erasure.execute_owner_erasure`) and reset immediately. With the GUC unset (every other code path),
   the triggers raise exactly as before — the Python `delete()` guards are also unchanged. Immutability
   is therefore **not weakened outside the governed path** (proved by `test_ordinary_delete_still_blocked`).
2. **Data-subject identity verification.** An explicit `ErasureRequest` with `identity_verified = True`
   is required (operator attests out-of-band verification). Unverified ⇒ refused.
3. **Owner-scoped authorization.** Erasure acts on exactly ONE owner (`subject_user_id_snapshot`); it
   never ranges over multiple owners. All deletes are `filter(owner=…)`/`filter(user=…)` scoped.
4. **Evidence-reference removal.** The owner's `Withdrawal`, `BrokerMessageObservation`, `BrokerEvent`,
   `ConnectedMailbox`, `BrokerEmailIdentity` rows are removed (in an order that releases PROTECT refs).
5. **Shared-object protection.** A physical blob is deletable only when **no lawful reference remains**:
   the row delete is attempted and a `ProtectedError` (another owner's event/observation still PROTECTs
   it) means **retain**. No reference-count field is relied upon — PROTECT is the source of truth.
6. **Physical blob deletion only after the final lawful reference is removed**, and only **after the DB
   transaction commits** (the file unlink is irreversible, so it is deferred past commit; a rollback
   leaves DB + file consistent; a crash post-commit leaves at most a benign orphan file).
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
- A small migration (0017) adds `ErasureRequest`/`ErasureHold` and GUC-gates the two trigger functions —
  additive; reverse restores the unconditional-raise trigger bodies. No data backfill.

## Status / ratification
**Proposed.** Do not activate (`BROKER_INTELLIGENCE_ERASURE_ENABLED`) in any environment, and do not run
`--execute` against production data, without explicit Sponsor approval for that specific operation.

# WV1 Controlled-Erasure — Governed Review Packet (adversarial-review corrections)

- **Date:** 2026-10-21 · **PR #543** (branch `feat/wv1-controlled-erasure`). **A governed review packet, NOT
  a merge request.** No ADR ratification, no merge, no production migration/erasure/deploy, no Console change.
  MT5 Copy Trading untouched.
- **Verified this turn:** 731 backend tests pass (incl. reworked erasure + governance), no migration drift,
  secret scan clean.

## Why a packet, not a finished merge
The correct Finding-2 fix is a **DB-enforced privilege boundary**, which requires a dedicated role +
SECURITY DEFINER procedure that **only a superuser can provision** (the app role `guvfx`/`dev` is not a
superuser and cannot create roles). Per your own directive, that provisioning is "separately authorized."
So the destructive deletion backend cannot be fully built + verified without that authorized provisioning.
This packet therefore **closes the critical vulnerability now (fail-closed)** and **specifies + stubs** the
remaining backend for the provisioning-gated increment.

## Finding-by-finding disposition
| # | Finding | Status |
|---|---|---|
| 1 | ADR number collision (0051 = copier) | **FIXED** — renumbered to **ADR-0054** after full inventory (0051/0052/0053 are the copier's CLOSE/MODIFY); all code/doc refs updated; **governance test** added (`core/tests_adr_governance.py`) preventing new duplicates (documents the pre-existing 0049/0050 cross-series overlap). |
| 2 | Unprivileged GUC bypass | **GUC bypass CLOSED (critical)** — triggers require `current_user = 'guvfx_erasure'` (migration 0017), not a forgeable GUC; **every ordinary DML DELETE (ORM/bulk/raw) is blocked**. Per a 2nd-round adversarial-review HIGH, the migration alone is **not a complete boundary against a FULLY-COMPROMISED app role** (it still OWNS the tables/functions → could TRUNCATE, or strip the triggers via DDL). Those OWNER-LEVEL vectors are closed by the **separate superuser provisioning**, which moves object **ownership** off the app role + REVOKEs DELETE/TRUNCATE. (A BEFORE-TRUNCATE trigger was rejected — owner bypasses it + it breaks Django test-flush.) Negative controls: `test_ordinary_delete_blocked`, `test_old_guc_cannot_bypass`. |
| 3 | Irreversible credential deletion inside txn | **DESIGNED (gated)** — no file deletion inside the txn; a **durable `DeletionTask` outbox** + idempotent worker removes files **post-commit**; request not COMPLETED until all tasks finish. Execute is fail-closed in this build (no deletion). |
| 4 | Post-commit crash window / orphan files | **DESIGNED (gated)** — every physical deletion is a durable outbox task; a crash leaves a PENDING task the worker completes (not an orphan). Recovery tests specified (crash→pending→worker completes→idempotent retry→missing-file handled→permission-failure retryable→request not "fully executed" while tasks remain). |
| 5 | Observation-only evidence omitted | **FIXED** — the candidate set is the **union** of owner BrokerEvent **and** BrokerMessageObservation evidence refs; discriminating test `test_observation_only_blob_is_a_delete_candidate`. |
| 6 | Concurrent execution | **IMPLEMENTED (control plane)** — execute takes `select_for_update` on the request + a per-subject `pg_advisory_xact_lock` + re-validates every gate under the lock; executable-status gate added. Full two-competing-process test runs in CI against the provisioned backend. |
| 7 | Identity-verification assertion | **DESIGNED (gated)** — the one-boolean `--identity-verified` is being replaced by an auditable lifecycle with distinct **requester / verifier / approver / executor**, timestamps, method category, and (preferred) **two-person approval**; the same invocation cannot create+verify+execute. Interim: create and execute are separate invocations + a status gate. |
| 8 | Retained audit personal data | **INVENTORIED + CLASSIFIED** — `WV1_AUDIT_PII_INVENTORY.md`: `BrokerEmailIdentityAudit.email`, `AuditEvent.ip_address`/`user_agent`, subject/account FKs and identifying JSON are personal data → **anonymise** (not blanket retain); DPIA + retention schedule updated. Anonymisation is part of the privileged backend. |

## 1. Corrected ADR number
**ADR-0054** (`docs/architecture/ADR-0054-CONTROLLED-ERASURE.md`). 0051/0052/0053 belong to the MT5 copier.

## 2. Database privilege design + threat model
Role-enforced triggers (`current_user = 'guvfx_erasure'`); a NOLOGIN privileged role; a SECURITY DEFINER
procedure (owned by that role, pinned `search_path`, fully-qualified, owner-scoped, ref-checked) as the ONLY
path to `current_user = guvfx_erasure`; app role not a member → cannot `SET ROLE`. Full text + threat model:
`ops/erasure/provision_erasure_privilege.sql` and ADR-0054 §1.

## 3. Proof the ordinary application role cannot invoke the bypass
`test_old_guc_cannot_bypass`: on the ordinary connection, `SET LOCAL "guvfx.erasure_authorized" = 'on'` then
delete → **still raises** (the trigger ignores the GUC and checks `current_user`). `test_ordinary_delete_blocked`:
bulk/ORM delete of EvidenceBlob/BrokerEvent → raises; rows persist. (Both run locally + CI.)

## 4. Durable deletion-outbox design
`DeletionTask(request, kind∈{CREDENTIAL_FILE,EVIDENCE_FILE}, target_ref, status∈{PENDING,COMPLETED,FAILED},
attempts, last_error, created_at, completed_at)`. The governed procedure writes tasks in the same txn as the
row deletes; an idempotent worker removes files after commit, retries FAILED, and marks the request COMPLETED
only when no PENDING/ FAILED tasks remain. Operator-visible states: DB-erased · external-pending · fully-erased ·
failed-needs-intervention.

## 5. Corrected identity-verification workflow
Request received → identity verified (verifier id + timestamp + method category, no excess ID documents) →
plan generated → independent approval (two-person) → execution (executor id) → completion review. `requested_by`,
verifier and executor mandatory for real execution; one invocation cannot do all three.

## 6. Complete evidence-reference inventory
Inbound PROTECT references to `EvidenceBlob`: `BrokerEvent.evidence`, `BrokerMessageObservation.evidence`.
Owner-scoped evidence relationships: `BrokerEvent.owner`, `BrokerMessageObservation.owner`,
`ConnectedMailbox.user`→credential file, `BrokerEmailIdentity.user`. A blob is physically removable only when
**no** inbound PROTECT reference remains (any owner). The plan now unions event + observation candidates.

## 7. Audit-personal-data inventory
`WV1_AUDIT_PII_INVENTORY.md` — field-level classification (Delete / Anonymise / Retain-under-basis /
Retain-with-expiry) for `BrokerEmailIdentityAudit`, `TradingAccountBrokerContactAudit`, `BrokerProfileAudit`,
`core.AuditEvent`.

## 8. New tests + negative controls
`broker_intelligence/tests_erasure.py` (15, pass): dry-run purity, observation-only candidate, shared-blob
retention classification, all validation gates (flag/status/identity/hold/token), fail-closed on unprovisioned
backend, and the Finding-2 negative controls. `core/tests_adr_governance.py` (3, pass): ADR uniqueness +
0054-not-0051 + non-vacuous. Gated backend tests (outbox crash/restart, concurrency, audit anonymisation) run
in CI against the provisioned backend — specified here.

## 9. Exact-head CI
Pushed to PR #543; CI runs on the corrected head (reported separately; backend ~10 min).

## 10. Remaining risks
Destructive deletion is **not yet implemented** (gated on superuser provisioning) — so erasure is not yet
operable; the outbox/worker, audit anonymisation and two-person workflow are designed but unbuilt; the
provisioning SQL function body is DRAFT and must be rehearsed before use.

## 11. Updated production-readiness
Erasure **capability: ~40%** (control plane + security boundary done; destructive backend gated). WV1
engineering overall unchanged (~95% feature; erasure is additive). Production-readiness **~55%** (the erasure
gap + prior recovery/log/DPA/DPIA gaps). Live-user availability **0%**.

## 12. Governed review packet — approval requests
- Review the corrected design (this packet + ADR-0054).
- Authorize (separately) the **superuser provisioning** of `guvfx_erasure` + the SECURITY DEFINER procedure in
  a **rehearsal** environment so the destructive backend + outbox + concurrency + audit-anonymisation can be
  built and fully tested.
- Keep PR #543 **DRAFT/DARK**, unratified, unmerged. Do not activate the flag or run `--execute` against prod.

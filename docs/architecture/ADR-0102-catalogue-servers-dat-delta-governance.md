# ADR-0102 — Governed, hash-pinned catalogue `servers.dat` delta workflow

- Status: Accepted (repository-only; DARK — no host write, not run against production)
- Date: 2026-11-13
- Packet: GFX-WI-SERVERS-DAT-GOVERNANCE-085 (programme lane C)
- Extends: ADR-0068 (catalogue carry-forward / staging / attestation), ADR-0078 (catalogue readiness / capture)
- Isolation: separate from the active Copy Trading workstream; touches no execution / emitter / broker-order path.

## Context

The existing `broker_catalogue` subsystem already provides an immutable, versioned, SHA-bound store of per-broker
`servers.dat` artefacts (`CatalogueVersion` DRAFT/ACTIVE/RETIRED with a single-ACTIVE partial-unique, a strong
`manifest_sha256`, per-artefact `CatalogueArtefact` immutable once the version leaves DRAFT via a model guard + a
DB trigger, an `approvals.ArtefactApproval` human YES bound to exact bytes, a `sanitiser` PII-absence verdict, and
`build`/`activate`/`rollback` commands). But a **new broker/server change is improvised**: there is no reviewable
*delta* object, no DRAFT→REVIEWED→APPROVED→STAGED→ACTIVATED lifecycle, no append-only transition history, and no
expected-previous-version hash pin — activation guards only per-broker byte changes, not the whole base.

## Decision

Add a governance layer **on top of** the existing subsystem — reusing (never re-implementing) its version /
manifest / sanitiser / approval / activation machinery — comprising two additive models (migration
`broker_catalogue/0006`), a tested service, a structural validator, and a governed command.

### 1. `CatalogueDelta` (+ append-only `CatalogueDeltaAudit`)

A `CatalogueDelta` is a hash-pinned proposed change from the current ACTIVE version (the **base**) to a proposed
DRAFT version:

- `base_version` + **`base_manifest_sha256`** (the hash-pin) and `proposed_version` + `proposed_manifest_sha256`.
- the privacy-safe server-identity **diff**: `added_identities` / `removed_identities` / `changed_identities`
  (broker_id + display_name + public server names + SHA-256 integrity hashes — **never bytes, never a credential**).
- a deterministic **`delta_sha256`** over the canonicalised diff (order-independent; tamper-evident).
- the lifecycle **state**: `DRAFT → REVIEWED → APPROVED → STAGED → ACTIVATED` (or `REJECTED` / `ROLLED_BACK`),
  with `proposer` / `reviewer` / `approver`, `ticket` / `reason`, and per-transition timestamps.
- **at most ONE in-flight delta** (a partial-unique on `in_flight`), so two proposals cannot race to activate.
- `CatalogueDeltaAudit`: an **append-only** transition history (model `save()`/`delete()` guards **and** a
  migration-0006 PL/pgSQL `BEFORE UPDATE OR DELETE` trigger, mirroring the 0005 immutability-trigger pattern).

### 2. Service (`delta_governance`)

`propose → validate (dry-run) → approve → stage → activate` (or `reject` / `rollback`), each explicit + audited:

- **Approval is SEPARATE from activation.** `approve` marks APPROVED and does **not** touch the ACTIVE pointer; a
  separate `stage` then `activate` is required.
- **Hash-pin / CAS.** `propose` optionally pins the expected base manifest (fail closed on mismatch); `activate`
  **re-checks** that the pinned base is still the ACTIVE version with the same manifest (`_assert_base_unchanged`),
  inside one `transaction.atomic()` + `select_for_update`, so a stale base (another delta activated in between)
  fails closed.
- **Reuse, not reinvention.** `activate` delegates the version flip to the proven `activate_catalogue_version`
  gates (approval / sanitiser / ownership / duplicate / carried-SHA) — with **no `--attest-host`** → DARK, no host
  write; `rollback` delegates to `rollback_catalogue_version`. A gate failure rolls everything back; the delta
  stays STAGED.
- **Fail-closed propose**: refuses a stale base, a non-DRAFT/absent proposed version, a no-op (identical) delta, a
  duplicate broker_id, an ambiguous/duplicate server name, a cross-broker ownership claim, or a current ACTIVE
  version that fails manifest integrity.

### 3. Structural validator

`structural_problems` validates the proposed version's declared server **identities + metadata** — canonical
non-empty names, no control characters, length-bounded, no cross-broker ownership, no duplicate names, each
artefact's `sanitisation_result == PASS` and a valid SHA-256. (The `servers.dat` **bytes** are the opaque MT5
format — not parsed here; their integrity is bound by SHA + the existing sanitiser verdict + immutability.)

### 4. Deterministic deployment-artifact descriptor

`staging_artifact_descriptor` emits a deterministic, checksummed descriptor (version + manifest + per-artefact
identity/SHA/size/host-relpath) that a **later, separately Sponsor-gated** host STAGE op can act on — this packet
prepares it but **writes no host**.

## Scope / non-goals

- **Repository-only / DARK.** No host contact, no `servers.dat` write, no terminal restart, no broker action; the
  `host_catalogue` seams stay fail-closed and are never invoked here. No fingerprint activation, no reprocessing,
  no publication, no Copy Trading change.
- **Minimal separation of duty**: `approve` refuses when the approver label equals the proposer label. The
  `--actor` values are **audit labels, not authenticated principals**; fuller SoD (reviewer≠approver≠activator,
  quorum) is enforced out-of-band by management-command access control and is a documented follow-up. The
  append-only audit records who did each action.
- **Proposed-content re-pin.** Because a proposed DRAFT version stays mutable, `validate` / `stage` / `activate`
  re-assert that the proposed version's manifest still equals the value recorded at propose (and re-run structural
  validation), so an operator cannot add/remove a broker after approval and ship a change set the reviewer never
  saw. `activate` and `rollback` take the ACTIVE pointer under `select_for_update` (CAS without a TOCTOU window).
- **No credential/account/PII and no raw bytes** ever enter the delta, the descriptor, the audit, evidence or
  Notion — only identity, SHA-256 hashes, enums and counts.

## Consequences — gated sequence (NOT executed here)

Production use is a separate, Sponsor-gated sequence: build the proposed DRAFT version and its approvals → `propose`
(hash-pinned) → `validate` → `approve` → `stage` → host STAGE op (separately gated) → `activate` → monitor →
`rollback` if wrong. This ADR is the reviewable mechanism, not an authorisation to run it in production.

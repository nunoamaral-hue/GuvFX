# ADR-0068 — Broker Catalogue carry-forward-active builder + governed host staging + read-only attestation

- **Status:** Accepted (additive; repository + DARK host primitives; no production mutation in this change).
- **Date:** 2026-10-30
- **Workstream:** Broker Catalogue V1 carry-forward build + governed host staging and attestation (P1)
- **Relates to / refines:** the Broker Catalogue V1 store + resolution (`broker_catalogue/models.py`,
  `service.py`, `preseed.py`, `build_catalogue_version`, `activate_catalogue_version`, `rollback_catalogue_version`),
  the approvals gate (`approvals/`), and the signed host-operation transport (`hosted_workspace/host_protocol.py`,
  `host_agent_dispatch.py`, `host_executor.py`, `deploy/hosted-executor/primitive_runner.py`).

## Context

The shipped `build_catalogue_version --label L` assembles a version from the `broker_servers_dat` approvals whose
ref ends `/L`. That is correct for a same-label build but cannot assemble a **successor** that (a) keeps the current
ACTIVE version's already-approved artefacts and (b) adds a small number of **separately**-approved refs at a
*different* label. Concretely: ACTIVE `v2` = `is6/v2` + `pepperstone/v2` + `taurex/v2`; two new brokers were approved
as `tradersway/v1` (id=7) and `vantage/v1` (id=8). `--label v3` finds no `*/v3` approvals, and the suffix grouping
cannot carry the `*/v2` artefacts forward. Re-approving three unchanged, already-approved artefacts purely to move
them to a `v3` label would be governance theatre.

Two further gaps block a governed promotion to ACTIVE:
1. There was **no host operation** to stage a DRAFT version's bytes into the shared catalogue root
   (`C:\GuvFX\catalogue\versions\<label>\<broker>\servers.dat`); the only catalogue host op copies an approved
   artefact into a *tenant* runtime (preseed).
2. `activate_catalogue_version --attest-host` had **no attestation transport** — `_attest_executor()` returned
   `None`, so `--attest-host` failed closed but could never succeed.

## Decision

### 1. An explicit, fail-closed carry-forward-active builder (Option B)
New mode `build_catalogue_version --carry-forward-active --include-approved-ref <broker>/<label> [...]`
(`broker_catalogue/carry_forward.py`). It copies the **single** ACTIVE version's artefacts into a new DRAFT,
preserving each one's **ref / SHA / size / servers / sanitiser verdict byte-for-byte** (only `host_relpath` is
re-labelled to the successor), and adds **only** the explicitly-named refs.

This is **not** an auto-approve path. Every carried artefact must still resolve to an **APPROVED exact-SHA**
`ArtefactApproval`, and its approval's size/servers must still match (an approval that drifted under a carried
artefact is refused). Every `--include-approved-ref` must be **APPROVED** and carry a sanitiser verdict bound to its
exact bytes (`sanitiser.verify_verdict`). It **refuses**: no/duplicate ACTIVE source; an ACTIVE source whose manifest
fails integrity; a carried broker whose approval is missing/revoked or whose size/servers drifted; a new ref that is
unapproved, sanitiser-failing, duplicated, malformed, or whose broker is **already active** (no *implicit*
replacement of an active broker — a byte change needs its own version + `--allow-byte-change` at activation);
cross-broker server-name collision; duplicate destination `host_relpath`. It creates a **DRAFT only** (no
activation, no staging, no provisioning), is **deterministic + idempotent** (an identical rerun is a no-op), and a
**divergent** rerun is **refused** rather than mutating the existing DRAFT (delete the DRAFT to change inputs).

### 2. Governed host staging (`STAGE_CATALOGUE_ARTEFACT`) — account-independent
A new allow-listed host operation writes one approved `servers.dat` under the shared catalogue root. It reuses the
existing signed transport (`host_protocol` HMAC + nonce + skew + the `/hosted/provision` endpoint) but is
**account-independent**: it carries the sentinel `account_id == 0`, and the dispatcher **neither derives a slot nor
applies the reserved-identity (Customer-Zero) guard** for it (there is no account; it acts on a shared resource).
This honours the architecture boundary — a Windows primitive acts on a fixed resource derived from validated
scalars, never a caller path. The approved **bytes ride in the signed `payload`** (bound via `payload_digest`),
never argv and never a log; the dispatcher decodes them and **refuses** unless `len == size_bytes` and
`sha256 == expected_sha256`. The host primitive (`Stage-GuvfxCatalogueArtefact.ps1`, ASCII-only) re-derives and
**confines** the destination to `versions\<label>\<broker>\servers.dat`, refuses traversal and reparse-point/symlink
escape, writes atomically (temp in the same dir → hash read-back → atomic rename), is **idempotent only on exact
match** (an existing non-identical file is refused, never clobbered), and fails closed unless the on-disk SHA equals
the approved SHA.

### 3. Read-only host attestation (`ATTEST_CATALOGUE_ARTEFACT`) + `--attest-host` wiring
A second account-independent, **read-only** operation reports a staged artefact's SHA-256 + size (per broker) or the
version's **full staged inventory** (when `broker_id` is empty). `activate_catalogue_version._attest_executor()` is
wired to `broker_catalogue.host_catalogue.resolve_catalogue_attest_executor()`, which stays **fail-closed** — `None`
unless `HOSTED_HOST_EXECUTOR_ENABLED` is on **and** the keyring/key_id/base_url are configured — so `--attest-host`
still refuses (no transport) in the DARK/repository phase. Activation now requires **exact agreement with the DRAFT
manifest**: every artefact's staged SHA must match (no missing + exact bytes), **and** — when the attester exposes a
version inventory — the staged broker set must equal the manifest (no **extra** broker directories/files).

## Alternatives considered

- **Re-approve every artefact at the new label** (the shipped builder): rejected — it demands fresh human approval
  to move unchanged, already-approved bytes, which is theatre and trains operators to rubber-stamp.
- **A separate catalogue signing protocol / endpoint:** rejected — reusing the hardened `host_protocol` (constant-
  time HMAC, single-use nonce, bounded skew) with an explicit account-independent operation set is smaller, shares
  the audited crypto, and keeps the per-account transport byte-identical for existing ops.
- **Carry the bytes as a host-local copy from the ACTIVE version dir:** rejected for the primitive contract — it
  would introduce a caller-influenced source path; payload-carried, SHA-bound bytes keep the no-path wire invariant.

## Consequences

- Existing behaviour is unchanged: the legacy `build_catalogue_version --label L`, the per-account host ops, the
  activation/rollback transactions, manifest self-verification and native fallback are all preserved. v2 stays ACTIVE
  until a human-gated activation.
- The two new host primitives require a **host-side dispatcher/service redeploy** (the dispatcher allow-list + the
  two `.ps1`) before they can run — a RULE 1/8/9 operation, **out of scope for this repository-only change** and
  gated in `docs/BROKER_CATALOGUE_V3_RUNBOOK.md`.
- Security boundary: account-independent ops cannot touch an account slot, a tenant workspace, or the golden image;
  staging writes only under the catalogue versions root; attestation is read-only. Customer-Zero is irrelevant to
  these ops (no account) and remains refused for every account-scoped op.

## Tests

`broker_catalogue/tests.py`: 20 carry-forward cases (happy v2+tradersway+vantage, exact inventory retention,
explicit-additions-only, revoked/missing approval, size/server drift, unapproved/sanitiser-fail/duplicate
include-ref, implicit-replacement refusal, server collision, invalid source manifest, idempotent rerun, divergent
rerun refused, DRAFT-only, no-active, DB-prevented second active, activation-refused-after-revoke), plus the
catalogue host client round-trip, the attest adapter (relpath parse, inventory extra/missing), and the activation
inventory gate (exact activates; extra/missing/sha-mismatch refuse; DARK resolver → None).
`hosted_workspace/tests_catalogue_host_ops.py`: the account_id==0 sentinel, payload size/SHA binding, slug/label/
traversal validation, reserved-guard skip, replay/expiry/payload-tamper, and `OP_PRIMITIVES == HOSTED_OPERATIONS`.
`hosted_workspace/tests_host_daemon_packaging.py`: the two primitives are ASCII (RULE 9) and the runner CONTRACT /
stage manifest cover them. No migration. No production mutation in this change.

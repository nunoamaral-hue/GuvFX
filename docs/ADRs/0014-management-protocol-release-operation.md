# 0014 — Management-protocol RELEASE operation (beta per-slot lifecycle)

- Date: 2026-07-24
- Status: **Proposed** (awaiting Nuno's explicit authorisation — do NOT implement or merge the operation until approved)

## Context

The beta per-slot lifecycle is `MATERIALISE → START → (running) → STOP → TOMBSTONE → release`. WS-A/WS-B
(PR #199, `e6cc37b`) restored process observation and implemented the open-handle probe, so TOMBSTONE's
cleanup precheck can now succeed. But completing the lifecycle surfaced a gap: **`pool_op_impls.release`
is implemented but has no protocol operation and no caller** — the agent dispatch handles only
`MATERIALISE / START / STOP / TOMBSTONE` (mutating), `VERIFY` (read-only) and `NEGOTIATE` (handshake). Its
own code comment states the consequence: without a release step "the slot stays assigned, so the pool
exhausts after `pool_size` tombstones and the generation never advances."

Adding an operation to the versioned management channel is an **API-surface / architecture change** (per
`.claude/rules/architecture.md`, it needs an approved decision before merge — not an in-passing edit). This
ADR proposes that operation for review.

## Verified facts (code)

- `PROVISIONING_OPERATIONS = ("MATERIALISE","START","VERIFY","STOP","TOMBSTONE")`; `HANDSHAKE_OPERATIONS =
  ("NEGOTIATE",)` (`lib/mgmt_protocol.py`). No RELEASE. `PROTOCOL_VERSION = 1`.
- `verify_request` rejects `protocol_version != PROTOCOL_VERSION` (exact match) → a version bump is a hard
  gate. NEGOTIATE reports `protocol_version`, `manifest_version` and `supported_operations`.
- `pool_op_impls.release(runtime_uuid, slot, generation, no_ambiguous_provisioning_job,
  no_mutation_lock_held)` builds seven proofs and calls `SlotStore.release_after_tombstone`.
- `release_after_tombstone` advances generation in **one atomic SQLite transaction** (clear runtime_uuid,
  `generation+1`, append the `slot_generations` ledger row, persist free); any failure rolls back and the
  slot stays occupied. `SlotIntegrityError` if the caller's `(slot, generation)` disagrees with the DB.
- The seven `RELEASE_PROOFS`: `runtime_process_stopped`, `process_identity_verified`,
  `canonical_directory_tombstoned`, `tombstone_evidence_persisted`, `no_ambiguous_provisioning_job`,
  `no_mutation_lock_held`, `slot_release_audit_persisted`.
- Architecture invariant (ADR-adjacent, CLAUDE.md): generation is a durable per-slot counter that
  "increments by exactly one on release after TOMBSTONE; it may never remain unchanged after release,
  decrease, or skip a value." A violation is a permanent integrity failure requiring operator intervention.

## Decision drivers

Least-privilege and fail-closed lifecycle; the runtime-identity invariant (generation monotonicity);
backward compatibility of the versioned channel; the "no arbitrary delete is expressible" property
(TOMBSTONE replaced destructive TEARDOWN); minimal new surface.

## Proposed operation

### 1. Protocol semantics
`RELEASE` is a **signed, mutating** provisioning operation carrying the fixed schema (`provisioning_job_id`,
`runtime_uuid`, `operation="RELEASE"`, `timestamp`, `expiry`, `nonce`, `correlation_id`, `key_id`,
`signature`) — no new fields, so `_SIGNED_FIELDS`/`_SEMANTIC_FIELDS` and the HMAC contract are unchanged. It
resolves `runtime_uuid → (slot, generation)` by lookup (never allocates), then **advances the slot's
generation by exactly one and frees the slot** for a runtime whose directory is already tombstoned. It
touches **no filesystem** — it is a pure occupancy-ledger transition.

### 2. Preconditions
- The runtime's `TOMBSTONE` stage is COMPLETED/ALREADY_COMPLETED (directory moved) and `verify_cleanup`
  COMPLETED — i.e. all four pre-move/post-move cleanup proofs are on record for `(slot, generation)`.
- The recorded stage evidence yields the seven `RELEASE_PROOFS` true, including the two the agent cannot
  observe for itself and which the **backend must attest in the signed request context**:
  `no_ambiguous_provisioning_job` (the backend still holds exactly the job this release closes) and
  `no_mutation_lock_held` (see §Interaction — RELEASE runs OUTSIDE the per-runtime lock).
- The DB's `(slot, generation)` for `runtime_uuid` equals the caller's view (else `SlotIntegrityError`).

### 3. Postconditions
- `generation → generation+1` atomically; the `slot_generations` ledger gains one `"release"` row;
  monotonicity (`+1`, never unchanged/decrease/skip) holds and is asserted against the append-only ledger.
- The slot's `runtime_uuid` is cleared and `assigned_at` nulled → the slot is **Available** (a future
  `MATERIALISE` may `assign` it under the NEW generation).
- The occupancy `(slot, old-generation)` is closed; its tombstoned directory + audit chain are retained.

### 4. Audit requirements
- `record_audit(event="tombstone_completed", operation="release", ...)` before the transaction; the
  release itself is the atomic ledger write. `assert_evidence_complete(slot, generation)` must pass first —
  the occupancy's stage sequence and evidence must reconcile, or RELEASE fails closed.

### 5. Idempotency guarantees
- Keyed by `semantic_digest` over `(protocol_version, provisioning_job_id, runtime_uuid, operation)` — a
  resend with a fresh nonce is idempotent. A repeat RELEASE for an already-released `(runtime_uuid, slot,
  generation)` MUST be a no-op returning the recorded result and **MUST NOT advance the generation twice**
  (the `SlotIntegrityError`/lookup guard already prevents a second advance because the runtime_uuid is no
  longer assigned to that slot).

### 6. Failure behaviour
- Any missing proof → `ReleaseProofMissing` → fail closed; the slot **stays occupied**, generation
  unadvanced. Transaction failure/interruption → full rollback (never half-released). A generation
  monotonicity violation → permanent integrity failure → **quarantine the slot for operator review**
  (never silently repaired). Reason codes surfaced to the client are the sanitised
  `release_precheck_failed` / integrity family — no path/secret leakage.

### 7. Interaction with the other operations
- **NEGOTIATE** advertises `RELEASE` in `supported_operations` (and the agreed `protocol_version`); the
  backend sends RELEASE only if it is advertised. **This is the compatibility mechanism** (see below).
- **MATERIALISE** is the only allocator; RELEASE is its inverse on the occupancy ledger (frees what
  MATERIALISE assigned), but only after the full teardown.
- **START / STOP** produce the `confirm_launch` / `confirm_terminated` evidence that RELEASE's
  `process_identity_verified` / `runtime_process_stopped` proofs consume.
- **VERIFY** is read-only and unaffected; it may be used before RELEASE to confirm the occupancy.
- **TOMBSTONE** is RELEASE's immediate predecessor and the two are deliberately split (§ next).

## How RELEASE differs from TOMBSTONE (Nuno point 4)

| | TOMBSTONE | RELEASE |
|---|---|---|
| Effect | Moves the runtime directory to the tombstone root | Advances generation + frees the slot on the ledger |
| Filesystem | Mutates (the move) | **None** |
| Lock | Runs **INSIDE** the per-runtime mutation lock | Runs **OUTSIDE** the lock (required proof `no_mutation_lock_held`) |
| Generation | Does not change it | `+1` (exactly), the sole generation-advancing op |
| Slot availability | Slot still occupied | Slot becomes Available |
| Reversibility | Quarantine-recoverable (dir retained under tombstone) | Ledger-atomic; interruption rolls back |

## Why RELEASE cannot be an existing operation (Nuno point 5)

1. **It must run outside the per-runtime mutation lock.** `no_mutation_lock_held` is one of the seven
   proofs; a release issued from inside TOMBSTONE (which holds the lock) could satisfy that proof only by
   lying. So it cannot be folded into TOMBSTONE, and every other mutating op (MATERIALISE/START/STOP) also
   holds the lock.
2. **It is the only operation that advances the generation counter and frees the slot.** VERIFY is
   read-only; NEGOTIATE is a handshake; TOMBSTONE deliberately does neither. No existing verb expresses
   "close this occupancy and make the slot assignable under the next generation," so overloading one would
   blur the audited stage boundaries the design depends on.

## Required protocol-version change + compatibility strategy (Nuno point 6)

Two options; the ADR **recommends A**:

- **A (recommended) — keep `PROTOCOL_VERSION = 1`; extend `PROVISIONING_OPERATIONS` with `RELEASE`.**
  The channel already negotiates capability via NEGOTIATE's `supported_operations`, not the version int
  alone. A backend sends RELEASE only when the agent advertises it; an agent that predates RELEASE simply
  omits it and rejects a stray RELEASE with `operation_not_allowed` — graceful, backward-compatible, no
  hard break. `verify_request`'s exact-match version gate is unchanged (RELEASE requests still carry
  `protocol_version = 1`). Signing/verification untouched.
- **B — bump `PROTOCOL_VERSION` 1→2.** Explicit about the contract change, but `verify_request`'s exact
  match makes it a **hard break**: every version-1 request is rejected until both sides upgrade, gated by
  NEGOTIATE agreeing v2. Heavier for no security gain over A, since capability is already negotiated.

Either way, `manifest_version` bumps (the impl changes) and NEGOTIATE remains the single point where the
backend learns whether this agent can RELEASE.

## Validating RELEASE on the preserved slot 1 once approved (Nuno point 7)

The preserved occupancy is `1f1b4b83… / slot 1 / generation 1`. **Its evidence is incomplete for release:**
Phase 8 launched and terminated MT5 via **direct scheduled-task triggers** (as that packet instructed), not
via agent START/STOP, so there is **no `confirm_launch` / `confirm_terminated` record** — `release`'s
`process_identity_verified` and `runtime_process_stopped` proofs cannot be met as-is. Two validation paths,
to be chosen at approval time:

- **Complete slot 1's evidence, then release it.** Run the agent-native tail on slot 1: `START` (re-launch
  MT5 once, Session 0, no account → records `confirm_launch`) → `STOP` (→ `confirm_terminated`) →
  `TOMBSTONE` → `RELEASE`. Frees the *actual* preserved slot and proves the end-to-end agent-native
  lifecycle; costs one more bounded MT5 launch.
- **Validate on a fresh runtime; leave slot 1 preserved.** Materialise a new runtime in a free slot and run
  it fully through the agent, releasing it to Available; document slot 1's half-in/half-out Phase-8
  lifecycle as a recorded special case.

## Consequences

Positive: the unattended lifecycle can complete; the pool no longer exhausts after `pool_size` tombstones;
generation advancement becomes observable and auditable. Negative/again-scoped: one new negotiated operation
to review and test; slot 1's release depends on the validation-path choice above.

## Risks and controls

- **Double-advance / generation skip** — controlled by the atomic transaction + the `runtime_uuid` lookup
  guard + the append-only ledger monotonicity assertion; a violation quarantines rather than repairs.
- **Release from inside the lock** — controlled by making `no_mutation_lock_held` a required, caller-stated
  proof and dispatching RELEASE on a path that does not hold the per-runtime lock.
- **Backend attestation trust** (`no_ambiguous_provisioning_job`) — the signed request context carries it;
  a false attestation is a backend-side integrity fault, bounded to closing the wrong (already-tombstoned)
  occupancy, not to any filesystem or trading action.

## Evidence / validation (to run AFTER approval, through the pipeline)

Unit: proof-missing → fail-closed; idempotent double-RELEASE does not double-advance; monotonicity
violation → quarantine; dispatch runs outside the lock. Host: the chosen slot-1 path above; confirm the
slot returns to Available at generation N+1 with a complete audit chain and production untouched.

## Reversal path

RELEASE is additive and negotiated; reverting = removing it from `PROVISIONING_OPERATIONS` and the
dispatch. No stored state depends on its existence beyond the generation advances it performed (which are
themselves the correct end state).

## Revisit trigger

Approval of this ADR (to implement), or a decision to model release differently (e.g. an automatic
post-TOMBSTONE step on a separate worker rather than a backend-driven operation).

## Approval

**Pending — Nuno.** Implementation and merge of the RELEASE operation are blocked until this ADR is
reviewed and explicitly authorised (Nuno, 2026-07-24 instruction). WS-A/WS-B (PR #199) are unaffected and
already merged.

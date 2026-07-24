# 0014 — Management-protocol RELEASE operation (beta per-slot lifecycle)

- Date: 2026-07-24
- Status: **Accepted** (Nuno, 2026-07-24 — "approved in principle; proceed with implementation") with the
  refinement below.

## Refinement adopted at approval (Nuno, 2026-07-24)

RELEASE is **not a filesystem-cleanup operation**; it is the **authoritative completion of the runtime
lifecycle** and the *only* operation permitted to transition `Released → Available`. No other operation may
bypass it. Its responsibilities: validate lifecycle preconditions, STOP evidence, process-observation
evidence, open-handle evidence and tombstone state; complete the audit evidence; return the slot to
Available; and advance lifecycle state atomically. It must be **idempotent, restart-safe, resumable after
interruption, fully audited, and fail-closed.**

Implementation consequence (how this differs from the original proposal): the refined RELEASE sources its
safety from **live re-observation at release time**, not only from historical stage evidence. Concretely it
requires a **live `observe_process` → ABSENT** (no process runs under the slot identity from the — now
tombstoned, hence gone — slot path) and the recorded `tombstone` + `verify_cleanup` (the latter having
proved handles clear via the WS-B open-handle probe). The original `process_identity_verified` proof (which
required a recorded `confirm_launch`) is **satisfied by the live proven-empty slot**: a slot with no running
process has no process identity to mis-attribute, and a live absent-observation is strictly stronger for
release *safety* than a historical launch record. This is what lets the preserved slot 1 — materialised by
the agent but launched/terminated out-of-band in Phase 8 — be released through the native lifecycle
`NEGOTIATE → VERIFY → STOP(if required) → TOMBSTONE → RELEASE → Available` with **no START and no manual
intervention**. The seven `RELEASE_PROOFS` and the atomic `release_after_tombstone` transaction are
unchanged; only the *sourcing* of two proofs (`runtime_process_stopped`, `process_identity_verified`) moves
from recorded stage evidence to live observation.

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
- **START / STOP** produce the `confirm_launch` / `confirm_terminated` evidence. The recorded-evidence
  `release()` builder consumes it; the shipped `op_release` instead derives those two proofs from a live
  proven-empty observation (the refinement), so an agent-native STOP is sufficient and a recorded
  `confirm_launch` is not required.
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

- **A (recommended, implemented) — keep `PROTOCOL_VERSION = 1`; extend `PROVISIONING_OPERATIONS` with
  `RELEASE`.** `verify_request`'s exact-match version gate is unchanged (RELEASE requests still carry
  `protocol_version = 1`); signing/verification untouched; the direction is fail-closed. **Compatibility is
  fail-closed, not tolerant** (this is the accurate framing — an earlier draft overstated "graceful old-agent
  operation"): the backend client's `assert_compatible` requires the agent to advertise the **entire**
  `PROVISIONING_OPERATIONS` set (`set(PROVISIONING_OPERATIONS).issubset(supported)`), so once the backend
  knows RELEASE, an agent that does **not** advertise it is refused the **whole** channel
  (`unsupported_operations`) — RELEASE becomes a **required co-deployed capability**, not an optional one.
  That is the safe direction (a backend never silently drives an agent missing a lifecycle op) and is
  correct because the backend and the agent bundle are versioned and re-staged from the same repo. **Deploy
  ordering:** re-stage the agent bundle (RELEASE present) **before or with** the backend that expects it; a
  drift-guard test pins `manifest.json.supported_operations == PROVISIONING_OPERATIONS` so NEGOTIATE can
  never advertise an op the host's integrity manifest lacks. A truly legacy agent (predating RELEASE) still
  rejects a *stray* RELEASE with `operation_not_allowed`, but the backend would not have negotiated with it
  in the first place.
- **B — bump `PROTOCOL_VERSION` 1→2.** Explicit about the contract change, but `verify_request`'s exact
  match makes it a **hard break**: every version-1 request is rejected until both sides upgrade, gated by
  NEGOTIATE agreeing v2. Heavier for no security gain over A, since capability is already negotiated.

Either way, `manifest_version` bumps (the impl changes) and NEGOTIATE remains the single point where the
backend learns whether this agent can RELEASE.

## Validating RELEASE on the preserved slot 1 once approved (Nuno point 7)

The preserved occupancy is `1f1b4b83… / slot 1 / generation 1`. Phase 8 launched and terminated MT5 via
**direct scheduled-task triggers** (as that packet instructed), not via agent START/STOP, so there is **no
`confirm_launch` / `confirm_terminated` record**. Under the *original* proposal that blocked release; **the
approval refinement removes the block** — `op_release` sources `process_identity_verified` /
`runtime_process_stopped` from a live proven-empty observation, not from those records, so slot 1 is
releasable **as-is** once its process is confirmed stopped and its directory tombstoned.

Adopted validation path (no START needed): run the agent-native tail on the *actual* preserved slot —
`NEGOTIATE → VERIFY` (observe current state) `→ STOP (only if VERIFY finds it running) → TOMBSTONE → RELEASE
→ Available`. This frees slot 1 through the native lifecycle and proves the end-to-end path with **no
re-launch and no manual intervention**, which is precisely why the refinement was adopted. (Had the
refinement not held, the fallback was to validate on a fresh materialised runtime and leave slot 1
preserved; that is no longer necessary.)

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

**Approved in principle (Nuno, 2026-07-24)** — "proceed with implementation through the normal governance
pipeline," with the refinement recorded at the top of this ADR. Option **A** (extend
`PROVISIONING_OPERATIONS`, keep `PROTOCOL_VERSION = 1`) was implemented. WS-A/WS-B (PR #199) are unaffected
and already merged.

## As implemented (2026-07-24)

Shipped exactly as Option A + the approval refinement:

- `RELEASE` added to `PROVISIONING_OPERATIONS` (`lib/mgmt_protocol.py`); `PROTOCOL_VERSION` stays `1`.
- `PoolOpImplementations.op_release` (`pool_op_impls.py`) implements the live-observation sourcing: it
  requires a fresh `observe_process → ABSENT`, refuses on `PRESENT_*` (`release_runtime_present`) **and** on
  an unreadable host (never fabricates "stopped"), then reuses the unchanged seven-proof
  `release_after_tombstone` atomic advance. It is registered in `as_dict()` and is **not** in agent-core's
  `_MUTATING` set, so it dispatches **outside** the per-runtime lock.
- Response sanitiser carries the new `available` signal through **both** the copy loop and the allowlist
  (`lib/mgmt_agent_core.py`); no path/secret crosses the channel.
- `manifest.json.supported_operations` gains `RELEASE` so `build_agent`'s per-op integrity manifest contains
  `op_release` and NEGOTIATE advertises it consistently. A new drift-guard test
  (`test_manifest_supported_operations_match_the_protocol`) fails closed if that list ever diverges from
  `PROVISIONING_OPERATIONS` — the precise failure mode that would otherwise let NEGOTIATE advertise an op the
  host denies with `impl_integrity_mismatch`.
- `release_runtime_present` classified INTEGRITY in `lifecycle.py`. Backend twins of `mgmt_protocol.py` /
  `mgmt_agent_core.py` re-synced byte-for-byte.

**Offline validation (real `build_agent`, `enforce_integrity=True`, signed protocol, FakeWin host):**
NEGOTIATE advertised `[…, RELEASE]`; `MATERIALISE`(gen 1) → `TOMBSTONE`(released=False, release_pending) →
`RELEASE`(released=True, available=True, **gen 1→2**) → slot returned to the free pool — no START, no manual
step. 639 `terminal_provisioning` tests + full `make check` green. **Host slot-1 proof pending re-stage.**

### Post-review remediation (adversarial review, 2026-07-24)

A five-lens adversarial review (each finding independently verified) confirmed 12 findings; the five that
touched behaviour were fixed in this branch, the three that are cross-component were recorded as bounded
follow-ups (below), and the four low/duplicate notes fold into these.

Fixed in code (+ tests):

1. **Quarantine / monotonicity gate bypass (HIGH).** `op_release` skipped the pre-mutation integrity gate
   every other mutating op runs, so a slot quarantined during the release-pending window could be released
   and reused, and a broken generation ledger could be advanced. Fix: the gate now lives at the single
   mutation point (`SlotStore.release_after_tombstone`) — it refuses a quarantined slot and asserts
   generation monotonicity before advancing, fail-closed, covering **both** release paths. (It does not
   quarantine on a stale-caller generation mismatch — that is a deny, not a corruption.)
2. **False `tombstone_completed` audit before the gate.** The audit was written before the proofs were
   evaluated, so a *refused* release left a chain-valid "completed" record for an occupancy that was never
   released. Fix: audit is written **only after** the atomic advance commits, and with the operation's own
   event **`slot_released`** (RELEASE performs no tombstone).
3. **Successor-generation mislabel (LOW).** The response reported `generation = new_gen`; it now reports the
   released occupancy's own generation, with `available: true` signalling the slot is now free.
4. **Asserted containment of a tombstoned-away directory (LOW).** `path_containment_verified` was `True`
   for a directory that no longer exists; now `False` (not observable at release).
5. Added the mandated **`before_release` audit-chain checkpoint** to `op_release`.

Deferred (bounded, recorded — NOT silently accepted):

- **Backend does not yet *send* RELEASE (MEDIUM).** The agent supports RELEASE and NEGOTIATE advertises it,
  but `provisioner._drive_deprovision` still calls only `teardown` (→ TOMBSTONE); no backend path issues
  RELEASE, so a backend-driven deprovision would tombstone without freeing and the pool would exhaust after
  `pool_size` teardowns. This is the **CVM-Inc-5** increment ("disable + remove beta runtime cleanly") and
  is out of scope for "implement the operation + prove it on slot 1" (slot 1 is driven agent-side). No live
  impact today: `BETA_RUNTIMES_ENABLED` is OFF and the beta deprovision path is not exercised.
- **Crash-window idempotency (LOW).** A same-job RELEASE resent after the advance committed but before the
  agent's idempotency record persisted resolves as `runtime_not_assigned` (the runtime is no longer bound) —
  a **safe, fail-closed** denial that never double-advances; the state is already correct. The backend that
  issues RELEASE (CVM-Inc-5) must map "RELEASE → `runtime_not_assigned` for a runtime it tombstoned" to
  *already released*. Recorded there.

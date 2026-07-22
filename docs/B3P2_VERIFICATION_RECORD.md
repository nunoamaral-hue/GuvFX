# B3P-2 verification record

Running evidence log for the per-slot execution model. Records what was **actually observed**, with the
exact trigger and result — not what was expected. Limitations are stated (evidence rule).

---

## EV-1 — Implementation-integrity gate (B-7): demonstrated TWICE, independently

**Status: PASS — positive verification evidence, from two independent unplanned triggers.**

Two separate demonstrations matter more than one: a single occurrence could be coincidence, two show the
mechanism is **repeatable and deterministic**.

### EV-1A — initial `stores.py` modification (slot allocator)

**Trigger (not staged — this happened during ordinary development).** `deploy/beta-agent/stores.py` was
edited to add `SlotStore`. `stores.py` is one of the nine modules covered by the full-bundle integrity
manifest introduced in B3P-1 (verification finding B-7, which expanded coverage from 4 modules to 9).

**Observed result.** Every mutating operation was immediately refused. Four previously-passing tests began
failing with a single, identical cause:

```
AssertionError: Tuples differ: ('denied', 'impl_integrity_mismatch') != ('denied', 'job_op_conflict')
```

Affected: `test_conflicting_duplicate_fails_closed`, `test_start_cannot_launch_twice`,
`test_replay_rejected_and_survives_restart`, `test_valid_request_ok_oversize_413_and_bad_route_404`.

**Resolution.** Regenerating `manifest.json` (→ `2026-07-22.1`) restored the match; all tests passed again.

### EV-1B — generation-counter modification (independent second trigger)

**Trigger.** `stores.py` edited again to add the durable generation counter, plus
`lib/mgmt_agent_core.py` edited to extend the response allowlist — a *second* covered module.

**Observed result.** Identical: mutating operations refused with `impl_integrity_mismatch`.

**Resolution.** `manifest.json` regenerated (→ `2026-07-22.2`, then `2026-07-22.3` after the allowlist
and evidence-builder changes); all tests passed again.

**Why EV-1B strengthens EV-1A.** The two triggers were separated in time, touched *different* module sets
(`stores.py` alone, then `stores.py` + `lib/mgmt_agent_core.py`), and neither was staged to test the gate.
Repeatability across distinct edits establishes the mechanism as deterministic rather than accidental.

**What this proves.**
- The gate is **live, not decorative**: an unannounced change to a covered implementation module caused
  every mutating op to fail closed, with a sanitised `impl_integrity_mismatch`, with no code change needed
  to trigger it.
- The B-7 expansion is **load-bearing**: under the pre-B3P-1 manifest (`op_impls.py`, `win_ops.py`,
  `lib/mgmt_protocol.py`, `lib/mgmt_agent_core.py` only) this edit to `stores.py` would have gone
  **completely undetected**. `stores.py` holds the durable replay/idempotency state — precisely a module
  where silent drift matters.
- The failure mode is the intended one: **deny mutations**, not degrade to permissive behaviour.

**Limitations of this evidence.**
- It exercises the *build-time* snapshot path (`build_agent` hashes disk at startup, and the per-op gate
  re-affirms that snapshot). It does **not** prove detection of on-disk tampering *after* a live agent has
  started — that is caught at next restart, as documented in `build_agent`'s docstring.
- Both triggers were observed in the test harness, not on the Windows host. On-box behaviour remains a
  B3 install item.

---

## EV-2 — Slot allocator (durable, pre-provisioned pool)

**Status: PASS (unit-level).** `deploy/beta-agent/stores.py::SlotStore`, 8 tests.

| Property | Evidence |
|---|---|
| `assign` idempotent per runtime | same `(slot, generation)` returned on repeat |
| Distinct runtimes → distinct slots | asserted |
| Pool exhaustion denied, never over-allocated | `PoolExhausted` (an `AgentError` → `pool_exhausted`) |
| `lookup` never allocates | occupancy stays empty for an unknown runtime |
| `release` idempotent | second call returns `False` |
| Survives restart | new `SlotStore` on the same file resolves the assignment |
| Path derived from slot number only | `slot_runtime_dir` ignores any caller-supplied value |

**Limitation.** No OS object is created or verified here — the pool (identities, tasks, directories, ACLs)
is pre-provisioned by an administrator at install and is **not** exercised by these tests.

---

## EV-3 — Slot generation counter + four-way integrity invariant

**Status: PASS (unit-level).** 11 tests.

**Generation semantics observed:** starts at `1`; re-assigning the same runtime does **not** bump it;
`release()` (post-TOMBSTONE) bumps it; the bump **survives restart**; releasing an unknown runtime bumps
nothing. A reused slot therefore gives its next occupant a **strictly greater** generation, so
`(slot, generation)` uniquely identifies one occupancy for the life of the pool.

**Four-way invariant** (`assert_slot_integrity`) — all of: slot-assignment database, on-disk ownership
marker, runtime UUID, slot generation. Verified to fail closed with a sanitised `slot_integrity_mismatch`
on:
- **stale generation** — a marker left by the *previous* occupant of a reused slot (the exact ambiguity
  this control exists to remove);
- wrong runtime UUID; wrong slot;
- **absent or corrupt marker** — treated as a mismatch, *never* as "free", so a leftover directory can
  never be silently adopted.

**Limitation.** The invariant is proven as a pure function. Wiring it into every mutating op path
(`op_impls`) is the next step and will be re-verified there.

---

## Where generation must appear (tracked to completion)

| Surface | Status |
|---|---|
| Slot ownership marker (on disk) | **Done** — `format_owner_marker` writes `{runtime_uuid, slot, generation}` |
| Provisioning Verification Report | **Done (unit-level)** — `build_verification_evidence` carries it; population from a real cycle lands with the op implementations |
| Management-channel response | **Done** — response allowlist carries `slot`/`generation`/`canonical_path`/`owner_marker_digest` |
| Audit evidence | **Done (unit-level)** — `slot_audit` requires slot+generation on every event; `audit_for_occupancy` filters on both |

---

## Test totals at each step

| Step | Bundle suite | Full suite |
|---|---|---|
| B3P-2 step 1 (slot allocator) | 49 | 1131 |
| B3P-2 step 2 (generation + invariant) | 60 | 1142 |

Scanner: clean at every step. Nothing installed on the Windows host; no OS object created.


---

## EV-4 — Generation monotonicity + quarantine

**Status: PASS (unit-level).** 8 tests.

**Invariant.** For every slot, `new_generation == previous_generation + 1` — never unchanged after
release, never decreasing, never skipping. Asserted against an **append-only ledger**
(`slot_generations`), so a tampered or rolled-forward `slots.generation` is *detected* rather than
trusted.

Verified to fail closed on: a ledger gap; a forged/rolled-forward current generation with no matching
ledger entry; a mismatch between the ledger tail, the stored generation and the expected value; and an
unknown slot.

**Quarantine.** `assert_occupancy_integrity` runs the full pre-mutation gate in order — *not quarantined →
database / marker / UUID / slot / generation agree → generation monotonicity* — and on **any** failure
quarantines the slot and raises a sanitised `slot_integrity_mismatch`. Verified that a quarantined slot is
**still refused even when every other check subsequently agrees**: recovery is an operator action, never a
silent repair. The healthy path is verified *not* to quarantine.

## EV-5 — Generation as first-class report evidence

**Status: PASS (unit-level).** 3 tests.

`build_verification_evidence()` carries all required fields: runtime UUID, slot, **generation**, ownership
marker **digest**, canonical runtime path, PID, session, implementation manifest version, protocol version,
and timestamps. The marker appears only as a 12-hex digest — never its contents. The shared response
allowlist (both byte-identical copies) now carries `slot`, `generation`, `canonical_path` and
`owner_marker_digest`, so this evidence can actually cross the management channel.

**Limitation.** Proven at unit level. Population from a real MATERIALISE/START/VERIFY cycle lands with the
pool-aware op implementations and will be re-verified there.

## Test totals (updated)

| Step | Bundle suite | Full suite |
|---|---|---|
| B3P-2 step 1 (slot allocator) | 49 | 1131 |
| B3P-2 step 2 (generation + 4-way invariant) | 60 | 1142 |
| B3P-2 step 2b (monotonicity, quarantine, report evidence) | 71 | 1153 |
| B3P-2 step 2c (remote-evidence boundary, audit, release order, clearance) | 87 | 1169 |
| B3P-2 step 2d (occupancy_id, audit chain) | 97 | 1179 |


---

## EV-6 — Remote evidence boundary (hardening requirement)

**Status: PASS (unit-level).** 3 tests. **Finding: the full path was NOT required remotely.**

**Question asked.** Does the backend genuinely need `canonical_path` in the management response?

**Evidence gathered.** `grep` over the backend for any consumer of a path returned by an agent found
**none** — the only occurrences were the allowlist entry itself and the agent's own internal variables.
`ProvisioningVerificationReport.runtime_root` exists, but is populated from the backend's own derivation
of the canonical root, not from an agent response.

**Conclusion.** No backend lifecycle decision requires the complete local filesystem path, so the preferred
contract was adopted rather than documented-and-retained. `canonical_path` was **removed** from the shared
response allowlist and replaced by `canonical_path_digest`, `path_containment_verified` and
`executable_containment_verified` — the backend receives the *attestation* that the agent independently
derived and verified containment, not the filesystem layout.

Verified: the local report still retains the full path; `remote_evidence()` strips it (test asserts the
string `GuvFX` appears nowhere in the projection); the allowlist no longer contains `canonical_path`.

## EV-7 — Audit propagation with occupancy identity

**Status: PASS (unit-level).** 5 tests. Every material lifecycle event carries all fourteen required
fields, and `slot` + `generation` are **mandatory** — `record_audit` raises if either is absent, and an
unknown event name is rejected. All twelve required event types are supported.

**The core rule is tested directly:** after a full occupancy → release → re-assign cycle,
`audit_for_occupancy(slot, generation)` returns *only* the current occupant's events; the previous
occupant's history is not attributed to it despite the identical slot number.

## EV-8 — Release order (seven proofs, atomic advancement)

**Status: PASS (unit-level).** 4 tests. A generation advances only when all seven proofs are durably
true; each proof is tested individually as a blocker (`release_proof_missing`, naming the missing proof).

**Ordering guarantee tested:** on a **pool of size 1**, a failed release leaves the slot occupied — a
subsequent `assign` for a different runtime raises `pool_exhausted` and the generation is unadvanced. The
slot is therefore never exposed to another runtime between TOMBSTONE and successful advancement.

Advancement is a single SQLite transaction (`BEGIN IMMEDIATE` → clear UUID → generation +1 → append
ledger → commit), which supplies atomicity and durability; an interruption rolls back entirely, leaving
the slot occupied and fail-closed. A stale caller view (wrong generation) is refused.

## EV-9 — Quarantine clearance

**Status: PASS (unit-level).** 4 tests. Clearance is refused unless a diagnosed reason, operator identity
and evidence reference are all supplied **and** reconciliation, no-runtime-process and directory-safe are
each explicitly confirmed — every one tested individually, with the slot verified still quarantined after
each refusal. Reconciliation is *re-derived* (`assert_generation_monotonic`), not merely asserted by the
operator. Clearing a slot that is not quarantined is refused. A successful clearance emits an auditable
`quarantine_cleared` event and is verified **not to rewrite or delete** any historical ledger entry.


---

## EV-10 — Occupancy ID propagation

**Status: PASS (unit-level).** 4 tests. `occupancy_id = SHA256("slot=<n>|generation=<g>")[:16]` is
deterministic, normalises its inputs, and is distinct across both dimensions. Verified present in all five
required surfaces: **Verification Report**, **remote evidence**, **slot audit**, **quarantine records**, and
the management-channel response allowlist (which carries it to ProvisioningJob evidence). Because it is
recomputable from `(slot, generation)` alone, an investigator can always re-derive it.

## EV-11 — Audit chain verification

**Status: PASS (unit-level).** 6 tests. Every audit record carries `previous_audit_hash` and `audit_hash`,
forming a forward-linked chain from a `genesis` root. `verify_audit_chain()` returns
`{"status": "VALID", "records": n}` on a healthy chain (and on an empty one), and raises
`audit_chain_corrupt` on **deletion**, **content modification** and **insertion** — the three accidental
corruptions it exists to catch.

**No automatic repair — verified.** After tampering, `verify_audit_chain()` was called twice; it raised
both times and the tampered row was confirmed **still tampered**. Recovery is operator investigation only.

**Stated limitation (not a weakness — a scope boundary).** This is *not* cryptographic tamper-proofing:
an actor able to rewrite the database can recompute the chain. It detects accidental deletion, insertion
and ordering corruption, which is precisely what silently misleads an investigation.

## EV-7 (elevated) — Historical attribution is deterministic

Recorded as **permanent verification evidence** at Nuno's direction. The cycle
*occupancy A → release → occupancy B → `audit_for_occupancy(slot, generation)`* returns **only occupancy
B**. Attribution is by `(slot, generation)` — never slot alone — so a previous occupant's history can never
be read as the current one's.

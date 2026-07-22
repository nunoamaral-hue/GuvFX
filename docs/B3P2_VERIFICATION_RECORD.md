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
- It was observed in the test harness, not on the Windows host. On-box behaviour is a B3 install item.
- Both triggers were observed in the harness; on-box behaviour remains a B3 install item.

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
| Provisioning Verification Report | **Open** — to be added with the report-writing step |
| Audit evidence | **Open** — to be added with the op implementations |

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

# B3P-2 verification record

Running evidence log for the per-slot execution model. Records what was **actually observed**, with the
exact trigger and result — not what was expected. Limitations are stated (evidence rule).

---

## EV-1 — Implementation-integrity gate (B-7) verified by accidental trigger

**Status: PASS — positive verification evidence.**

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
- A second regeneration (→ `2026-07-22.2`) was required after the generation-counter change, reproducing
  the same behaviour a second time.

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

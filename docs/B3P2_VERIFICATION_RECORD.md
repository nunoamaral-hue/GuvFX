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
| B3P-2 step 3 (binding, process birth, task identity, checkpoints) | 116 | 1198 |
| B3P-2 step 4 (read-only Windows primitives, stages 1-3) | 116 + 36 | 1234 |


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


---

## EV-12 — Occupancy binding and the primitive boundary

**Status: PASS (unit-level).** 5 tests. `build_occupancy_binding` carries all eight required fields.

The boundary is enforced **structurally, not by convention**: `slot_scoped_view()` returns exactly
`{slot, slot_path, launch_task, terminate_task}` and the test asserts `runtime_uuid`, `generation`,
`occupancy_id`, `provisioning_job_id`, `integrity_outcome` and `quarantined` are **absent**. A primitive
therefore *cannot* make a policy decision even by accident — it never receives the inputs for one.

`reconcile_primitive_result` rejects a result reporting a different slot or a different path, so a
primitive result can never be attributed to an occupancy it was not issued under.

## EV-13 — Process birth identity (PID reuse)

**Status: PASS (unit-level).** 4 tests. Birth evidence records PID, creation timestamp, image digest,
executable-containment result, user SID, session and slot.

`assert_same_process` fails closed when the **PID matches but** creation time, owner, image, session or
slot differ — the exact PID-reuse case in which a later unrelated process would otherwise inherit an
earlier occupancy's identity. Unverified executable containment also fails closed.

## EV-14 — Task identity and drift

**Status: PASS (unit-level).** 4 tests. The approved definition digest covers task name, run-as identity,
executable, working directory, logon type, run level and enabled state, and is order-independent.

Drift **blocks launch** — verified individually for a changed run-as identity (e.g. to `Administrator`),
executable, working directory, logon type (e.g. to `INTERACTIVE_TOKEN`), run level (e.g. to `HIGHEST`) and
task name, plus a disabled task. The agent surfaces drift as a refusal; it never repairs or rewrites a task
at runtime.

## EV-15 — Audit-chain checkpoints as lifecycle gates

**Status: PASS (unit-level).** 6 tests. All five boundaries are defined and a healthy chain passes each.

Corruption behaviour is verified in both attribution cases:
- **attribution possible** (slot in hand) → the slot is **quarantined** and the checkpoint raises;
- **attribution uncertain** (`before_assign`, no slot) → **all new allocation is blocked** —
  a subsequent `assign` for any runtime raises `allocation_blocked`.

Clearing the allocation block requires operator identity **and** an evidence reference; an empty identity
is refused. Chain verification is therefore a lifecycle gate, not a reporting nicety.


---

## EV-16 — Read-only Windows primitives (stages 1–3)

**Status: PASS (unit-level).** 36 tests in `tests_win_primitives.py`. Stages implemented: task-definition
inspection, process observation, filesystem containment + reparse validation. **No mutating primitive
exists yet** — no stage copy, launch, terminate or tombstone code is present.

**Read-only proven mechanically, not by inspection.** `RecordingFakeWin` implements both the read *and*
write surfaces; every write method (`make_dirs`, `copy_golden`, `write_owner_tag`, `move_dir`, `stop_pid`,
`run_task`, `end_task`, `set_acl`, `register_task`, `open_for_write`) **records** the attempt instead of
performing it. Tests assert `side_effects == []` after every primitive, including on the absent,
permission-denied and query-failure paths where sloppy code is most tempted to "fix things up".

**Immutability.** `SlotInput` is a frozen dataclass — assignment to any field raises. `from_scoped_view`
takes a defensive copy, and a test mutates the caller's dict afterwards (to slot 99 and to
`C:\GuvFX\accounts\1`) proving the in-flight input is unaffected.

**Attestation.** Exactly the seven permitted keys; verified across all three primitives that
`runtime_uuid`, `generation`, `occupancy_id`, `provisioning_job_id`, tenant and entitlement fields are
**absent**.

**Time.** Creation time is an integer FILETIME (100-ns ticks). Equality comparison with a documented
tolerance constant of **0**; string/locale representations are refused outright (a test passes
`"2026-07-22 09:00:00"`, `"22/07/2026 09:00"` and the stringified tick value — all reject). A process whose
creation time is not machine-readable is `present_invalid`/`creation_time_unusable`, never valid.

**Absence ≠ success.** Five distinct states verified: a missing task is `task_absent` (not "invalid"); a
task query failure is `task_observation_unavailable`; permission denial is its own state; a process query
failure is `process_observation_unavailable` (**not** "not running"); an unmaterialised slot is
`terminal_path_absent` (not a containment failure).

### Defect found and fixed by these tests

The production-exclusion test caught a **real hole**: `is_beneath` is a *lexical* prefix test that does not
resolve `..`, so `C:\GuvFX\beta\slots\..\..\accounts` passed containment while escaping the namespace.
Traversal components are now rejected outright rather than normalised. This is exactly the class of bug the
production-exclusion requirement exists to surface.

**Production exclusion verified:** the resolver derives paths and task names from the slot NUMBER alone;
Nuno's MT5 path, `C:\GuvFX\accounts`, `C:\GuvFX\terminals`, the bridge script, ports 8788/8787,
production task names (`GuvFX_Autostart`, `GuvFX_SignalBridge`, `GuvFX_BridgeWatchdog`, `GuvFX_LaunchMT5`,
`GFX_LaunchIS6`) and Administrator/SYSTEM/`guvfx-rdp` run-as identities are all refused. Session is not a
parameter of any primitive — a primitive can observe a session but cannot request one.

**Limitations.** All observations run against a fake adapter; no real Windows API is called and nothing has
touched the host. The real adapter is written at the install-only gate.


---

## EV-17 — Primitive capability declarations (READ_ONLY vs MUTATING)

**Status: PASS (unit-level).** Capability is a construction-time property of `PrimitiveContext`, a frozen
dataclass validating its value in `__post_init__` (an unknown capability raises `ValueError` — a typo
cannot silently produce a permissive context). Two module constants exist, `READ_ONLY_CONTEXT` and
`MUTATING_CONTEXT`.

Every mutating primitive begins with `require_mutating(ctx, "<operation>")`, which raises
`CapabilityViolation` (reason code `capability_violation`, operation name in the redactable detail).
A parameterised test invokes **all seven** mutating entry points — `stage_copy`, `request_launch`,
`confirm_launch`, `request_terminate`, `confirm_terminated`, `tombstone`, `verify_cleanup` — with
`READ_ONLY_CONTEXT` and asserts each raises, then asserts the fake host recorded **zero** calls. The guard
is the first statement in each function, so refusal happens before argument validation, before any host
query and before any write.

`require_mutating` also refuses `ctx is None`, so a caller that forgets the parameter fails closed rather
than defaulting to permitted.

**Limitation.** This binds Python call sites within the bundle; it is not an OS-level capability. Its value
is that a future edit invoking a mutating helper from an observation path fails loudly in CI instead of
quietly writing to the operator's host.

---

## EV-18 — Operation sequencing evidence

**Status: PASS (unit-level).** 6 tests. Sequence numbers are allocated by `SlotStore.record_sequence()`
from the `occupancy_sequence` table, whose `PRIMARY KEY (slot, generation, sequence_number)` makes a
duplicate **structurally impossible** — the test proves this by attempting a raw duplicate INSERT and
asserting `sqlite3.IntegrityError`, not by asserting an application-level check.

Numbering starts at **1 within each occupancy**, not per slot: after release and reassignment of the same
slot, the new `(slot, generation)` starts again at 1 and the predecessor's rows are untouched. A reused
slot therefore cannot inherit or extend its predecessor's lifecycle history.

`assert_sequence_valid()` treats a **gap** as `SlotIntegrityError` (verified by deleting sequence 2 of 3),
consistent with the requirement that missing or duplicated sequence numbers are integrity failures rather
than warnings. An occupancy with no operations yet is valid — "nothing has happened" is distinguished from
"something is missing". Sequence state is durable across store reopen.

---

## EV-19 — Golden stage-copy integrity (stage 4)

**Status: PASS (unit-level).** Integrity is proven on **both** sides of the copy.

Six pre-checks run before `copy_golden` is called at all: `source_digest_matches`,
`source_manifest_version_matches`, `destination_absent`, `destination_beneath_slot`,
`destination_not_reparse`, `generation_matches`. Tests for a wrong source digest, a stale source manifest
version, an already-present destination and a generation mismatch each assert the failure **and** that the
fake host recorded no `copy_golden` call — the refusal happens before the host is touched.

Four post-checks then run: `destination_digest_matches`, `executable_digest_matches`,
`portable_marker_present`, `ownership_marker_present`. A parameterised test breaks each one in turn and
asserts the result is `failure` / `stage_copy_incomplete`.

**No partial copy may ever proceed to launch:** a post-check failure returns a refusal carrying the full
per-check evidence map. There is no "probably fine" path — launch requires a successful stage-copy record.

---

## EV-20 — Launch attestation: REQUESTED is not OBSERVED (stages 5–6)

**Status: PASS (unit-level).** Launch is two records with different meanings.

`request_launch` emits `phase: "REQUESTED"` with `trigger_accepted` and **no process facts whatsoever** —
a test asserts `pid`, `birth` and `created_at_filetime` are absent from that evidence. A rejected trigger
is `launch_trigger_rejected`; permission denial and query failure are distinct reason codes.

`confirm_launch` emits `phase: "OBSERVED"` and completes the launch only when the process is observed
`present_valid` **and contained**. The decisive test is the realistic failure: the task trigger is accepted
(`success`), then no process appears — `confirm_launch` returns `failure` / `process_absent`. A trigger is
therefore never sufficient evidence that MT5 started. A process running from Nuno's production MT5 path
also fails confirmation despite existing.

Birth evidence carries pid, creation FILETIME, image digest, containment verification, user SID, session
and slot — the fields `assert_same_process` later needs to prove the process it terminates is the one it
started.

---

## EV-21 — Termination semantics: STOP success means ABSENT (stage 7)

**Status: PASS (unit-level).** `request_terminate` triggers the fixed per-slot terminate task and is
REQUESTED only. `confirm_terminated` returns success **only** when the slot process is `ABSENT`.

Three failure modes are separated rather than collapsed:
- process still present with matching birth identity → `process_still_running` (the trigger succeeded and
  the process lived on — the exact case a "task returned 0" check would have called success);
- process present but **not** the one launched → `unexpected_process_in_slot` (escalation, not success);
- process cannot be observed → `process_observation_unavailable` — an unobservable process is **never**
  reported as terminated.

---

## EV-22 — Tombstone move and cleanup/rollback verification (stages 8–9)

**Status: PASS (unit-level).** `tombstone` performs a **move**; the fake host records exactly one
`move_dir` and no delete call exists on the surface. A cross-volume destination is refused
(`cross_volume_move_refused`) before any host call, because a cross-volume "move" is a copy-plus-delete. An
already-absent slot directory returns success flagged `idempotent`, so a retried rollback does not fail.

`verify_cleanup` proves six independent facts — `slot_directory_empty`, `no_task_running`,
`no_runtime_process`, `no_runtime_handles`, `audit_complete`, `generation_unchanged` — and a test breaks
each one individually, asserting the specific proof appears in `missing`. `generation_unchanged` is the
ordering guard: cleanup runs **before** release, so an already-advanced generation means the release
happened out of order.

Failure still produces evidence: the incomplete result carries the full proof map and a signed evidence
digest, so a failed rollback is auditable rather than silent.

**Limitations (EV-19 → EV-22).** All six mutating stages run against fake host adapters. No real Windows
API has been called, no file copied, no task triggered, no process started or stopped, and nothing has been
installed on `WIN-RD8VDS93DK7`. The real adapter and the pool identities/tasks are created only at the
separately approved install-only gate.

---

## EV-1C — Integrity gate triggered a third time (unplanned, positive evidence)

Editing `win_mutations.py` **after** the manifest was regenerated failed 10 mutating-op tests plus the two
manifest tests, exactly as designed: `drifted: ['win_mutations.py']`. The gate has now caught three
unplanned edits (`stores.py`, `lib/mgmt_agent_core.py`, `win_mutations.py`), each time from a genuine
mistake rather than a rehearsed demonstration. Manifest advanced `2026-07-22.8 → .9 → .10`; the covered set
is now **12 modules**.

---

## EV-23 — Defects found by self-review of the mutating layer (fixed before merge)

Three defects were found reviewing the mutating primitives against the production-exclusion requirement.
All three are fixed and covered by tests.

**1. `tombstone_dir` was trusted, not validated (containment hole).** Every other mutating primitive
derives its target from the slot NUMBER, but the tombstone destination is necessarily caller-supplied —
making it the one place a caller could steer a *write* outside the beta namespace, including on top of the
operator's estate. `assert_authorised_tombstone_dir` now rejects traversal components, anything not beneath
`C:\GuvFX\beta\tombstones\<slot>` (so slot 2 cannot write into slot 3's history either), and the forbidden
production fragments. Tests prove `C:\GuvFX\accounts\1`, `C:\GuvFX\terminals\x`, the IS6 MT5 program
directory, `C:\Windows\System32`, a `..`-escape and another slot's tombstone root are all refused **with
zero host calls recorded** — including on the already-absent path, so the idempotent no-op cannot be used
to smuggle an unvalidated destination past the guard.

**2. `destination_beneath_slot` was a tautology.** It compared the destination against *its own parent* —
true for every possible path. It recorded a passing check that proved nothing while reading as if it had.
Containment is now asserted against the fixed `BETA_SLOTS_ROOT\<slot>`, and the reparse check against
`BETA_SLOTS_ROOT`. A new test makes the slot directory a reparse point onto `C:\GuvFX\accounts\1` and
asserts the copy is refused before any host call; a reparse point resolving to somewhere *inside* the beta
root still passes.

**3. `verify_cleanup` proved only the launch task was idle.** A terminate task still running is as much
"not finished" as a launch task is. Both fixed task names are now checked.

Cross-volume note: with the destination now pinned to a fixed root, a different volume can only arise from
a mount point under that root — which is precisely why the `same_volume` refusal is retained, and the test
was rewritten to model that case rather than an out-of-namespace `D:` path.

---

## EV-24 — Primitive capability declarations enforced across the whole mutating set

**Status: PASS (unit-level).** Superseded scope note: EV-17 recorded the mechanism; this records the
enforcement after the mutating set grew. `PrimitiveContext` validates its capability in `__post_init__`
(an unknown value raises `ValueError`, so a typo cannot yield a permissive context), and all seven mutating
entry points call `require_mutating()` as their first statement — before argument validation, before any
host query, before any write. The parameterised test drives every entry point with `READ_ONLY_CONTEXT`,
asserts `capability_violation`, and asserts the recording fake logged **zero** calls. `require_mutating`
also refuses `ctx is None`, so a forgotten parameter fails closed rather than defaulting to permitted.

---

## EV-25 — Operation idempotency evidence (COMPLETED vs ALREADY_COMPLETED)

**Status: PASS (unit-level).** Six statuses exist: `NOT_STARTED`, `REQUESTED`, `COMPLETED`,
`ALREADY_COMPLETED`, `BLOCKED`, `FAILED`. The distinction that matters on a retry after an ambiguous
failure is proven, not asserted:

- a first run of `stage_copy` is `COMPLETED`;
- a retry that finds the destination present **and passes all four post-checks** is `ALREADY_COMPLETED`,
  performs **no** second copy, and carries `idempotent: true`;
- a destination present with a **wrong digest** is `BLOCKED`, not waved through;
- a **wrong generation** with a perfect-looking destination is `BLOCKED` — the "already done" path requires
  that presence be the *only* failed precondition, so no other disagreement can be masked by a complete
  directory;
- a **partial copy** is `FAILED`, not `BLOCKED`, because `BLOCKED` carries the promise that nothing was
  attempted on the host. A test asserts `copy_golden` was in fact called on that path.

`NO_EFFECT_STATUSES` is checked against the recording fake: every `BLOCKED`/`ALREADY_COMPLETED` result
recorded zero host calls. `attest()` refuses an unrecognised status outright. Read-only observations carry
an **empty** status — an observation is not a lifecycle stage, and claiming otherwise would misrepresent it.

---

## EV-26 — Failure classification is total and single-valued

**Status: PASS (unit-level).** Every sanitised reason code maps to exactly one of `CONFIGURATION`,
`INTEGRITY`, `OBSERVATION`, `WINDOWS`, `OPERATOR`, `TIMEOUT`.

Totality is **not** maintained by hand. A test walks the AST of every bundle module, collects every
reason-code literal reachable from `_fail(...)`, `_wrap(...)`, `AgentError("...")`-style construction and
`reason_code=` keywords, and fails if any is unclassified. A hand-written inventory would drift silently;
this one cannot.

Single-valuedness is structural: the category is derived inside `attest()` from a dict, so a code cannot be
filed under two categories by two call sites. `classify(strict=True)` raises `UnclassifiedReasonCode` (CI);
`classify(strict=False)` degrades to `INTEGRITY` — the conservative reading, since an outcome we cannot
classify is by definition a disagreement — and records `classification_complete=False` so the degradation
is visible rather than mistaken for a real classification.

The semantic separations the categories exist to preserve are asserted individually:
`process_observation_unavailable` is OBSERVATION and never WINDOWS; `process_still_running` is WINDOWS;
`task_absent` is CONFIGURATION; `image_outside_slot` is INTEGRITY; `pool_exhausted` and
`quarantine_clearance_refused` are OPERATOR.

---

## EV-27 — Per-stage contracts, checked against the implementation

**Status: PASS (unit-level).** `STAGE_CONTRACTS` holds preconditions, invariant, postconditions and
permitted statuses for all seven mutating stages, as **data beside the implementation**.

The contract is verified in both directions: a test AST-scans `win_mutations.py` for the status each
`_ok`/`_fail` call site emits (including the helper defaults) and asserts the set a stage **declares**
equals the set it can **produce**. This caught a real inaccuracy immediately — `request_launch` declared a
`BLOCKED` outcome it cannot emit, because an unauthorised slot input raises there rather than returning a
record. The contract was corrected to match the code.

`docs/B3P2_STAGE_CONTRACTS.md` is generated from the same structure by `render_contracts.py`, and a test
asserts the published file matches the code. Editing the doc without changing the code fails CI.

---

## EV-28 — Evidence completeness, structural and detected

**Status: PASS (unit-level).** Success and failure are both first-class audit events; ten stage results
spanning every status are each run through `assert_evidence_present`, which requires all eight attestation
keys, a non-empty evidence digest, a recognised status and an evidence body. Failure results additionally
carry a reason code **and** a category.

Durably, `SlotStore.record_stage` writes the sequence position and the evidence **in one transaction**,
which makes "an operation happened but left no evidence" structurally impossible rather than merely
detectable. A stage offered with no evidence digest is refused at the door and nothing is sequenced.
`assert_evidence_complete` still exists for what the structure cannot cover — later tampering — and is
proven to catch a deleted evidence row, an evidence row for an unsequenced operation, and an evidence row
naming a different operation. A duplicate evidence row for one stage is impossible (primary key).

---

## EV-29 — Slot-aware path resolution

**Status: PASS (unit-level).** `SlotResolver` maps a runtime UUID to its occupancy and derives the path
from the **slot number alone**: tests assert the resolved path is `C:\GuvFX\beta\slots\1\terminal` and
contains neither the UUID nor its hex form, and that the task names are the fixed
`GuvFXBetaRuntime-1` / `GuvFXBetaRuntimeStop-1`.

**Only `MATERIALISE` may allocate.** `START`, `VERIFY`, `STOP` and `TOMBSTONE` resolve by lookup and raise
`runtime_not_assigned` for an unknown runtime; a test asserts the pool remained empty after four such
attempts, so a replayed or out-of-order request cannot consume capacity. Resolution is stable across
operations, and `occupancy_id` matches the `(slot, generation)` pair.

**No silent fallback.** The execution model is explicit and validated at construction: `slot_pool` without
a resolver raises, an unknown model raises, and the default remains the documented B2 compatibility model.
A pool agent missing its resolver therefore fails to start rather than quietly provisioning into the old
UUID-directory namespace — the same reasoning as security RULE 3.

---

## EV-30 — Pool-aware operation implementations

**Status: PASS (unit-level).** The five allowlisted operations are expressed as sequences of the approved
stages, with the integrity gate before every mutation.

- **Ordering proof.** A full lifecycle records exactly `stage_copy → request_launch → confirm_launch →
  request_terminate → confirm_terminated → tombstone → verify_cleanup`, and both `assert_sequence_valid`
  and `assert_evidence_complete` reconcile afterwards.
- **The marker follows the proof.** `write_owner_tag` runs only after stage-copy is `COMPLETED`; when a
  post-check fails, the test asserts no marker was written. A marker vouching for a partial runtime is the
  failure mode this ordering exists to prevent.
- **Integrity gate.** A stale marker from generation 0, a marker naming another runtime, and an absent
  marker are each refused with `slot_integrity_mismatch`, and the slot is **quarantined** rather than
  repaired. A quarantined slot refuses further mutation. A missing slot binding is `slot_binding_missing`.
- **START observes before triggering** — a running runtime returns `idempotent` with **zero** host calls,
  so a second terminal cannot be launched.
- **A trigger that starts nothing is not a start**: the evidence shows `REQUESTED` succeeded and
  `confirm_launch` FAILED with `process_absent`.
- **STOP** fails `process_still_running` when the terminate trigger is accepted but the process survives.
- **Cleanup precedes release**: after tombstone the generation is still 1, and a process reappearing in the
  slot blocks cleanup with `cleanup_incomplete`.
- **Boundary**: no operation's response contains a filesystem path — a test asserts no returned value
  contains `C:\GuvFX`, while `canonical_path_digest` (the attestation) is present.

---

## EV-31 — Windows API boundary enforced by scan

**Status: PASS (unit-level).** A test AST-scans every bundle module for imports of
`win32*`/`win32com`/`pywintypes`/`winreg`/`ctypes`/`subprocess`/`shutil`/`psutil`/`wmi` and for
`os.system`/`os.popen`/`os.spawnl`/`os.startfile` calls, failing if any appears outside two permitted
files: `win_ops.py` (the adapter — every host operation) and `service.py` (the Service Control Manager
harness only). A second test asserts no stage or store layer imports the concrete adapter class.

Recording `service.py` as separately permitted, with its own stated reason, is deliberate: folding it into
"the adapter" would merge two different responsibilities into one statement (Rule 5).

**Limitation.** This proves the boundary in the repository. It does not prove the real adapter works — no
method in it has executed on a Windows host, and the contract's section 6 lists what only the viability
trial can settle.

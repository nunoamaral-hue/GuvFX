# CVM-Inc-3 B3P-2 — Per-stage contracts

<!-- GENERATED FILE — do not edit by hand.
     Source of truth: deploy/beta-agent/lifecycle.py :: STAGE_CONTRACTS
     Regenerate: python3 deploy/beta-agent/render_contracts.py > docs/B3P2_STAGE_CONTRACTS.md
     A test asserts this file matches the code, so an edit here without a code change FAILS CI. -->

Every mutating stage states what had to be true **before** it ran, what must hold **throughout**, and what
is true **after**. These are held as data in `lifecycle.STAGE_CONTRACTS`; the statuses each stage declares
are checked against the statuses its implementation can actually produce, so a stage cannot grow a new
outcome while keeping an old contract.

## `stage_copy`
**Preconditions**
- capability is MUTATING
- slot input is the authorised fixed slot namespace
- golden source digest and manifest version match the approved values
- destination is absent, or already present with a matching digest (ALREADY_COMPLETED)
- expected generation equals actual generation

**Invariant** — the destination path is derived from the slot number alone and never leaves BETA_SLOTS_ROOT\<slot>; a reparse point on the slot directory aborts before any write

**Postconditions**
- destination digest equals the approved golden digest
- runtime executable digest is present
- portable marker present
- ownership marker present
- no partial copy is left eligible for launch

**Statuses it may report** — COMPLETED, ALREADY_COMPLETED, BLOCKED, FAILED

## `request_launch`
**Preconditions**
- capability is MUTATING
- stage copy COMPLETED or ALREADY_COMPLETED
- slot input is authorised

**Invariant** — the occupancy binding is unchanged; only the fixed per-slot launch task is triggered and no process identity is asserted

**Postconditions**
- a REQUESTED record exists carrying no process facts

**Statuses it may report** — REQUESTED, FAILED

## `confirm_launch`
**Preconditions**
- capability is MUTATING
- launch was REQUESTED

**Invariant** — same slot, same occupancy; observation performs no state change

**Postconditions**
- either COMPLETED with process-birth evidence (pid + creation FILETIME + image digest + containment + SID + session), or FAILED with the observation state

**Statuses it may report** — COMPLETED, FAILED

## `request_terminate`
**Preconditions**
- capability is MUTATING
- slot input is authorised

**Invariant** — only the fixed per-slot terminate task is triggered; no process is signalled directly

**Postconditions**
- a REQUESTED record exists; success of the trigger is never success of the stop

**Statuses it may report** — REQUESTED, FAILED

## `confirm_terminated`
**Preconditions**
- capability is MUTATING
- birth evidence from the launch is available

**Invariant** — same slot, same occupancy; an unobservable process is never reported as terminated

**Postconditions**
- COMPLETED only when the slot process is ABSENT; a surviving process is process_still_running and a different process is unexpected_process_in_slot

**Statuses it may report** — COMPLETED, FAILED

## `tombstone`
**Preconditions**
- capability is MUTATING
- process confirmed terminated
- destination is beneath this slot's tombstone root

**Invariant** — a MOVE within one volume; never a delete, never a copy+delete, never another slot's tombstone history

**Postconditions**
- the slot directory no longer exists and its contents are retained under the tombstone root

**Statuses it may report** — COMPLETED, ALREADY_COMPLETED, BLOCKED, FAILED

## `verify_cleanup`
**Preconditions**
- capability is MUTATING
- tombstone COMPLETED or ALREADY_COMPLETED

**Invariant** — generation has NOT advanced — cleanup runs before release, so an advanced generation means the release protocol ran out of order

**Postconditions**
- all six cleanup proofs hold, or the missing ones are named in the evidence

**Statuses it may report** — COMPLETED, FAILED


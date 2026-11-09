# ADR-0084 — Copy Trading bridge lifecycle guard: pre-state / revision / deadline / idempotency + explicit outcomes

- **Status:** Accepted (repository-only / DARK, default-OFF). Bridge wiring, host deployment, flag enablement and
  any live use are separate Sponsor gates. No deploy, no host contact, no broker order is part of this packet.
- **Packet:** GFX-COPIER-LIFECYCLE-COMPLETION-004.
- **Date:** 2026-11-08.
- **ADR number:** 0084 — the next genuinely free number (0083 = ADR-0083 lifecycle completion; no 0084 exists).
- **Builds on / realises:** ADR-0083 (the LIVE lifecycle completion, which documented *bridge-side pre-state and
  deadline enforcement* as a deployment prerequisite and whose `_live_command_envelope` already carries the
  pre-state fingerprint, master revision, expiry/deadline, idempotency key and correlation id). ADR-0057 (the
  order-time identity pin, which the bridge already enforces). Nothing in those ADRs is modified.

## Context — the audited gap (Phase 1)

A direct, assumption-free trace of the real path established that the gap is at the **final bridge boundary**, not
in the backend. Per-event matrix (producer → transport → ingestion → lifecycle → job/intent → bridge op → outcome
ack → alert → **gap**):

| Event | Backend path (exists) | Bridge / outcome | Current gap |
|---|---|---|---|
| Master OPEN | observer/event-ingest → `MasterEvent` → lifecycle `OBSERVED_OPEN` → `admit_open`/`admit_open_live` → `PLACE_ORDER` | bridge `execute_mt5_trade` (PLACE_ORDER only) + identity pin | OK (live OPEN admitted, ADR-0057) |
| Entry ack / final failure | reconcile `MATCHED/UNMATCHED/AMBIGUOUS`; lifecycle `ENTRY_FAILED_FINAL` (no late entry) | job-complete → status | OK — no-late-entry is enforced by the pure engine |
| SL/TP add / change / remove | `change_types`/`diff` → lifecycle `MODIFY_PENDING` → `admit_modify`/`admit_modify_live` → `MODIFY_POSITION` | bridge `modify_position` (identity only) | **No pre-state / revision / deadline / explicit-outcome at the bridge** |
| Partial close | `admit_partial_close(_live)` → `PARTIAL_CLOSE_TRADE`; `reconcile_partial_close` (PCLOSE_OUTCOME_UNKNOWN, read-only, no retry) | dedicated partial-close worker + claim/complete | **No pre-state fingerprint / revision / deadline check; outcome not mapped explicitly** |
| Full close | `admit_close`/`admit_full_close_live` → `CLOSE_TRADE` | bridge `close_position` (identity only) | **Same — no pre-state / revision / deadline / explicit-outcome** |
| Duplicate / replay | monotone `applied_sequence`; `_*_already_targets` idempotency | `find_existing_execution` (PLACE_ORDER) | **No per-mutation idempotency / ALREADY_APPLIED at the bridge** |
| Out-of-order revision | engine rejects stale sequence | — | **Bridge does not reject a stale revision command** |
| Host/bridge timeout; unknown outcome | partial-close has `PCLOSE_OUTCOME_UNKNOWN`; modify/full-close do not | job marked failed | **Uncertain modify/close outcome is not contained as UNKNOWN_OUTCOME** |

The backend already **carries** every field needed (ADR-0083) and already **reconciles** a partial close
read-only (never a blind retry). What is missing is the enforcement + explicit outcome mapping at the point of the
send. This ADR adds exactly that, as a pure, DARK, testable unit — it does **not** re-implement the backend.

## Decision

Add `scripts/mt5_copier_lifecycle_guard.py` — a **pure, importable** module (no MetaTrader5, no network, no
Django) that the MT5 signal bridge consults immediately **before** and **after** it sends a copier
`MODIFY_PROTECTION` / `PARTIAL_CLOSE` / `FULL_CLOSE`, DARK behind `COPIER_LIFECYCLE_GUARD_ENABLED` (default OFF —
with the flag off the bridge path is byte-for-byte unchanged). The guard authorises nothing on its own; it only
rejects, or admits, a send that every existing gate (identity pin, demo/live wall, authorization) already
permitted.

### Pre-send admission — `admit_mutation(command, snapshot, *, now_epoch, last_applied_revision, point, volume_step)`

Returns `(outcome, reason)`: `PROCEED` to send, `ALREADY_APPLIED` when the requested post-state already holds (a
safe no-op success), or a fail-closed terminal outcome. It verifies, **fail-closed on any missing/unparseable
field**:
- a valid **operation** type; a present **ticket**;
- a **hard deadline** (`authorization_expires_at`) — `now ≥ deadline ⇒ DEADLINE_EXCEEDED` (no late action at an
  unpredictable price);
- a **monotonic revision** — `master_revision ≤ last_applied ⇒ STALE_REVISION`;
- **position presence** — absent ⇒ `POSITION_NOT_FOUND` for modify/partial, `ALREADY_APPLIED` for a full close;
- the **pre-state fingerprint** — ticket, identity (login/server, when pinned), symbol, and for a partial the
  `expected_current_volume` — any mismatch ⇒ `PRE_STATE_MISMATCH`;
- **idempotency** — SL/TP already set, or already at `expected_remaining` ⇒ `ALREADY_APPLIED`;
- a partial reduction that is **strictly reducing** and never ≥ the current volume (defence in depth vs the
  backend) ⇒ else `PRE_STATE_MISMATCH`.

All numeric comparisons are **normalised** (price to half a point, volume to half a step) — never display strings.

### Post-send classification — `classify_result(command, *, send_determinate, broker_rejected, post_snapshot, point, volume_step)`

Maps a send result + a **fresh** post-send snapshot to one explicit outcome: `APPLIED` (post-state re-verified),
`BROKER_REJECTED` (definitive refusal), `UNKNOWN_OUTCOME` (timeout / lost connection / no post-snapshot — **never
inferred as success**; requires reconciliation), or `CONTAINED_FAILURE` (a determinate success whose post-state
contradicts the request, e.g. a partial that left no position). **An uncertain result is never success.**

### Explicit outcome vocabulary

`APPLIED, ALREADY_APPLIED, PRE_STATE_MISMATCH, DEADLINE_EXCEEDED, STALE_REVISION, POSITION_NOT_FOUND,
BROKER_REJECTED, UNKNOWN_OUTCOME, CONTAINED_FAILURE` — the same vocabulary the merged ADR-0083 Phase-3 operator
alert classifier already maps (`classify_lifecycle_failure`), so once the bridge reports these the operator
surface lights up with no new category work.

## Consequences

- The enforcement **logic** for the bridge boundary now exists as a reviewed, fully unit-tested (29 synthetic
  cases) pure module. With `COPIER_LIFECYCLE_GUARD_ENABLED` off (default) and no bridge wiring in this packet, the
  running bridge is **unchanged** — zero risk to the live money path.
- The guard never opens or increases exposure and never turns an uncertain result into success.

## Integration points (NOT wired in this packet — the deliberate next, focused step)

The guard is called from the bridge's three mutation paths, each DARK behind `COPIER_LIFECYCLE_GUARD_ENABLED`:
1. `scripts/mt5_signal_bridge.py::modify_position` — read `positions_get(ticket)` for the pre-snapshot, call
   `admit_mutation`; on a non-`PROCEED`/`ALREADY_APPLIED` verdict return the explicit outcome WITHOUT sending;
   after the send, read a fresh snapshot and call `classify_result`.
2. `scripts/mt5_signal_bridge.py::close_position` — same, for a FULL close.
3. `scripts/mt5_partial_close_worker.py` — same, for a PARTIAL close (its existing `PCLOSE_OUTCOME_UNKNOWN`
   reconcile remains the authoritative read).
The explicit outcome is reported to the backend via the existing `complete_job` result, where the ADR-0083
Phase-3 classifier maps it to an operator alert. **Wiring these three paths into live-money code is a separately
reviewed PR** — mis-wiring the send path is the catastrophic risk, so it is not bundled here.

## Separate future Sponsor gates (NOT in this packet)

Bridge wiring (the three hooks above) + the backend outcome→`CopierReplication.reconciliation_status`→alert
recording; the unknown-outcome reconciliation extension for modify/full-close (the partial-close pattern already
exists); provisioning/execution-gate parity (a single shared fail-closed evaluation); then DARK deployment and a
Sponsor-present human-operated live test. No live readiness is claimed on synthetic tests.

## Adversarial review

An independent adversarial lane reviewed the guard against: late-entry (N/A — the guard never opens), exposure
increase, uncertain→success, stale-revision bypass, deadline bypass, pre-state bypass, idempotency, normalisation
(float/precision), and malformed-command fail-closed. Findings + disposition are recorded in the evidence manifest
`evidence/manifests/GFX-EVD-COPIER-LIFECYCLE-COMPLETION-004.json`.

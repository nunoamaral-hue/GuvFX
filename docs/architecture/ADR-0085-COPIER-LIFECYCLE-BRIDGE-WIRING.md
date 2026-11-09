# ADR-0085 — Copy Trading lifecycle-guard bridge wiring (DARK, default-OFF)

- **Status:** Accepted (repository-only / DARK, default-OFF). Host deployment, flag enablement, a DARK demo run
  and any human-operated live test are separate Sponsor gates. No deploy, no host contact, no migration, no
  broker order is part of this packet.
- **Packet:** GFX-COPIER-LIFECYCLE-BRIDGE-WIRING-006.
- **Date:** 2026-11-09.
- **ADR number:** 0085 — the next genuinely free number (0084 = the guard logic; no 0085 exists).
- **Builds on / realises:** ADR-0084 (the PURE guard logic — `scripts/mt5_copier_lifecycle_guard.py`,
  `admit_mutation` / `classify_result`), which was deliberately merged *logic-only, not wired*. ADR-0083 (the LIVE
  lifecycle command envelope that already carries the pre-state fingerprint, master revision, expiry/deadline and
  correlation id) and ADR-0057 (the order-time identity pin the bridge already enforces). **Nothing in those ADRs
  is modified. ADR-0084 remains the guard-logic decision; this ADR is only the integration decision.**

## Decision

Wire the ADR-0084 guard into the bridge's three real follower-mutation paths and record its outcome onto the
existing canonical records — entirely behind the DARK flag `COPIER_LIFECYCLE_GUARD_ENABLED` (default OFF).

### 1. Bridge boundary (`scripts/mt5_signal_bridge.py`)

The three money functions — `close_position`, `modify_position`, `partial_close_position` — are **unchanged
(byte-for-byte)**. Three new thin wrappers are the ONLY place the guard is consulted:

- `guarded_close_position` → FULL_CLOSE, `guarded_modify_position` → MODIFY_PROTECTION,
  `guarded_partial_close_position` → PARTIAL_CLOSE.
- A wrapper runs the guard **only** when `COPIER_LIFECYCLE_GUARD_ENABLED` is on **and** the caller supplied a
  typed copier command (`copier_operation` present). Otherwise the wrapper **is** the money function and returns
  its result verbatim — proven byte-identical by `execution.tests_copier_bridge_wiring` (flag off / malformed /
  no-command).
- When active, `_guarded_mutation` reads an authoritative follower snapshot (`_guard_follower_snapshot`), calls
  `admit_mutation` **before** the send (a terminal verdict sends NOTHING; `ALREADY_APPLIED` is a no-op success;
  only `PROCEED` calls the UNCHANGED money function), then reads a fresh post-snapshot and calls `classify_result`.
  The explicit outcome is attached as `lifecycle_outcome` / `lifecycle_reason`.

**Fail-closed invariants.** A guard-import failure or an unreadable pre-state sends nothing (`CONTAINED_FAILURE`,
`guard_blocked`). An indeterminate send or an unreadable post-state is `UNKNOWN_OUTCOME`, never success. A definite
non-success verdict forces `ok=False`.

**No execution-authority widening (packet tests #25/#26).** The guard adds a *rejection + classification* layer on
top of every existing gate; it authorises nothing. On `PROCEED` it calls exactly the money function the un-guarded
path calls — it reaches no PLACE_ORDER / open path and never opens or increases exposure. The money functions'
own demo wall is untouched: CLOSE / MODIFY / PARTIAL-CLOSE remain **demo-only and independently fail-closed for
LIVE** (`_bridge_live_admitted` is not consulted by them). Enabling LIVE mutation is therefore NOT part of this
packet — it is a separate Sponsor-gated change (see *Remaining work*).

### 2. The shared partial-close worker

`authorize_send` (backend) additionally releases the server-derived typed `command` — ONLY when
`copier_lifecycle_guard_enabled()` AND the job carries a typed copier envelope; otherwise the response is
byte-identical to the pre-ADR-0085 shape. The worker (`scripts/mt5_partial_close_worker.py`) passes it verbatim to
`guarded_partial_close_position` and interprets none of it. A partial-close guard admission that blocks the send is
recorded by the EXISTING consumer as a pre-send dispatch-refusal (`guard_blocked`), so "nothing sent" stays
truthful.

### 3. Persistence → operator alert (`trade_copier/lifecycle_outcome_record.py`)

`record_lifecycle_outcome(replication, bridge_result, *, op)` maps a bridge `lifecycle_outcome` onto the EXISTING
canonical records: it writes `CopierReplication.reconciliation_status` (non-destructively — never over an
authoritative partial-terminal state) and, for a terminal failure, raises ONE de-duplicated governed
`reliability.AlertEvent` via the EXISTING ADR-0083 `failure_alerts` categories (no new competing enum). Success
raises no alert; `UNKNOWN_OUTCOME` alerts (never inferred as success). It is the function the gated close/modify
lifecycle dispatcher calls, and it is fail-open. The read-only operator view (ADR-0083 Phase-3) surfaces these
without new work.

## No-late-recovery (permanent)

The guard never opens a position — it only MODIFY/PARTIAL/FULL on an existing LINKED ticket. A mutation past its
deadline is `DEADLINE_EXCEEDED` (never a late action); a missing position is `POSITION_NOT_FOUND` (modify/partial)
or an idempotent `ALREADY_APPLIED` (full close), never a replacement entry. The only retries anywhere are bounded
read / verification reads that cannot create, modify or close a broker position. A final entry failure stays
failed and operator-visible.

## Monotonic-revision watermark

The bridge passes `command.get("last_applied_revision", -1)` to `admit_mutation`. It is **not** sourced from
`CopierPositionLifecycle.applied_sequence`, which is the master-event *projection* high-water mark (it reaches the
new event's sequence as soon as the event is projected) and would wrongly reject the first legitimate follower
mutation. Until a dedicated per-follower-mutation watermark is wired, bridge-side staleness rejection is a
defence-in-depth backstop fed by the command; the authoritative ordering remains the backend lifecycle projection
plus the governing-authorization staleness guards (`event_not_after_open` / `superseded_by_later_close`).

## Consequences / what stays OFF

No migration (all additive code + a flag + two reused reconciliation strings). The live money path is unchanged
when the flag is OFF (the default). No deploy, host contact, flag enablement, live authorization or broker order.

The guarded path adds two extra `guarded_initialize` / `mt5.shutdown()` cycles per mutation (the pre- and
post-snapshot reads) and a post-send settle (`GUVFX_GUARD_POST_SETTLE_SECONDS`, default 1.5s) before the
authoritative re-read, so a genuinely-successful close/modify is not mis-verdicted "not reflected" on a
just-opened attach. On a confirmed `APPLIED` the wrapper reconciles a stale money-side failure marker (e.g. a
lost-ACK close whose position is now confirmed gone) so the merged result is self-consistent; it only ever
UPGRADES `ok` on an authoritative post-state confirmation, never fabricates one.

**Arming precondition (operational).** Because each `mt5.shutdown()` tears down the process-global MT5 singleton,
the guard's extra attach/shutdown cycles enlarge the pre-existing unlocked-concurrent-MT5 window when
`MT5_BRIDGE_THREADED` is OFF (the serialization lock is then a no-op). `COPIER_LIFECYCLE_GUARD_ENABLED` must
therefore be armed only on a bridge with `MT5_BRIDGE_THREADED` on, so every MT5 touch is serialized. This is a
deployment precondition, enforced at the (separate, Sponsor-gated) arming step — not in this repository-only,
flag-OFF packet.

## Remaining work before a DARK deployment and a human-operated live test

1. A separate, Sponsor-gated change to admit a LIVE copier mutation at the bridge (the demo wall stays until
   then) — the analogue of the PLACE_ORDER `MT5_ALLOW_LIVE` admission, for CLOSE / MODIFY / PARTIAL-CLOSE.
2. The gated armed close/modify lifecycle **dispatcher** that calls the bridge HTTP mutation endpoints with the
   envelope and feeds the result to `record_lifecycle_outcome` (the partial-close path is already wired through
   the worker).
3. A per-follower-mutation monotonic watermark source for `last_applied_revision`.
4. Provisioning / execution-gate parity, a DARK host deployment (flag still OFF), then a Sponsor-present
   human-operated DEMO run, and only then a governed live test. No synthetic test asserts live readiness.

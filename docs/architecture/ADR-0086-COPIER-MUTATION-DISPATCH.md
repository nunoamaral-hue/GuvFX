# ADR-0086 — Copy Trading DARK MODIFY/CLOSE lifecycle-mutation dispatch (+ the RED live-admission design)

- **Status:** Accepted (repository-only / DARK, default-OFF). Deployment, flag enablement, provisioning of a real
  transport, the RED live-mutation admission, and any live use are separate Sponsor gates. No deploy, no host
  contact, no broker order is part of this packet.
- **Packet:** GFX-COPIER-DEMO-READINESS-028 (Phase 6).
- **Date:** 2026-11-09.
- **ADR number:** 0086 — the next genuinely free number.
- **Builds on / realises:** ADR-0083 (`create_live_modify_job` / `create_live_full_close_job` already mint the
  typed `MODIFY_POSITION` / `CLOSE_TRADE` ExecutionJobs with the full command envelope), ADR-0084 (the guard
  logic) and ADR-0085 (the bridge wiring + `record_lifecycle_outcome`). Nothing in those ADRs is modified.

## Context — the gap this closes (in code)

ADR-0085 wired the guard into the bridge and merged the `record_lifecycle_outcome` recorder, but at `a241f317`
**no backend consumer sends a `MODIFY_POSITION` / `CLOSE_TRADE` job to the bridge, and the recorder has no
production caller** — only `PARTIAL_CLOSE_TRADE` has a dedicated worker. So follower MODIFY and FULL-CLOSE are
*produced* (DARK) but never *dispatched*. This ADR adds the missing consumer.

## Decision — the DARK dispatcher (`trade_copier/mutation_dispatch.py`)

A DARK, default-OFF pass that, for each `PENDING` `MODIFY_POSITION` / `CLOSE_TRADE` job:

1. **claims it single-flight** (`PENDING` → `RUNNING` under a row lock) so a job is sent at most once;
2. **re-checks the governing live authorisation at send time** (gate parity with the producer — a revoke /
   expiry / kill-switch committed after the job was minted fails closed here too), via the injectable
   `gate_check` (default: `live_execution._relock_live_authz_for_lifecycle`);
3. **derives the per-ticket revision watermark** and stamps it on the command (see below);
4. **sends** the typed command (the job payload = the ADR-0083 envelope) through an **injectable `transport`**; and
5. **records** the bridge outcome onto the EXISTING canonical records via
   `lifecycle_outcome_record.record_lifecycle_outcome` (`reconciliation_status` + governed operator alert), and
   maps it to the job/replication terminal state: APPLIED/ALREADY_APPLIED → `SUCCESS` / `EXECUTION_VERIFIED`; a
   terminal failure → `FAILED` / `EXECUTION_FAILED`; **UNKNOWN_OUTCOME → leaves the job `RUNNING`** for the
   authoritative reconciler (never inferred as success).

**Fail-closed / DARK invariants.** Gated by `copier_mutation_dispatch_enabled` (default OFF, ANDed with the
LIVE-execute arm stack). The **default `transport` is INERT** — it performs NO network call and contacts NO
broker, returning `mutation_transport_disabled`; the job is then reverted to `PENDING` (nothing was sent). A real
per-tenant push transport (per-follower `HostedExecutionEndpoint` + token + the snapshot-identity firewall) is a
**separate provisioning/arming step**, so this module cannot reach a broker on its own even with every flag on.
It opens no position, creates no ExecutionJob, and never recovers a late entry. `PARTIAL_CLOSE_TRADE` is
intentionally excluded — it keeps its dedicated least-privilege worker (ADR-0056).

## Migration-free per-ticket revision watermark

`follower_mutation_hwm(follower, ticket)` = the highest `MasterEvent.sequence` among `EXECUTION_VERIFIED`
MODIFY/CLOSE replications for that exact follower ticket, or `-1` when none. It is **derived from existing data —
no new field, no migration** — and is stamped as `last_applied_revision` so the bridge guard rejects an
out-of-order mutation (`STALE_REVISION`). It is deliberately NOT `CopierPositionLifecycle.applied_sequence` (the
master-projection HWM, which would wrongly reject the first legitimate follower mutation — see ADR-0085).

## The separate RED live-mutation admission (DESIGNED here, NOT implemented, NOT enabled)

The three bridge money functions (`close_position` / `modify_position` / `partial_close_position`) keep a hard
`account_info.trade_mode != 0 → account_not_demo` wall, so a LIVE follower mutation is independently fail-closed
regardless of the guard or this dispatcher. Enabling a LIVE `#46 → #48` demo therefore needs a **separate,
Sponsor-gated (RED) change** with this contract — specified here, **built in no packet yet**:

- **Per-route, never global.** Admit a LIVE mutation ONLY for an explicitly mapped, authorised follower identity
  (login + server + account), exactly as the PLACE_ORDER path does via `_bridge_live_admitted` /
  `MT5_ALLOW_LIVE` + `MT5_LIVE_EXPIRY` + `MT5_EXPECTED_LOGIN`. The wall is **never weakened globally**; the demo
  follower is the only identity the per-runtime live bind admits, and every other account stays demo-walled.
- **Layered, not a bypass.** The live-mutation admission is ANDed with (never ORs past) the existing identity pin,
  the ADR-0084 guard, the governing live authorisation and the kill switch. It authorises nothing on its own.
- **Time-bound + fail-closed.** It requires a valid, non-expired host time-bound and the exact-account pin;
  absent/expired/mismatched ⇒ refused, exactly as the PLACE_ORDER live admission.
- **No execution enabled.** Implementing it does not arm execution; arming remains the separate
  `copier_live_execute_enabled` + `copier_mutation_dispatch_enabled` + a provisioned transport, each Sponsor-gated.

## Consequences / what stays OFF

No migration. No deploy, host contact, flag enablement, transport provisioning, live authorization or broker
order. With the flag OFF (default) and the inert default transport, the pass is a no-op that contacts no broker.

## Remaining work before a real #46→#48 demo

1. **Sponsor deployment decision** for the copier scope (canonical-main `a241f317` carries unrelated
   broker_intelligence #623 + migrations `trade_copier/0012` + `broker_intelligence/0027`).
2. The **real per-tenant push transport** for this dispatcher (resolve the follower bridge endpoint + token under
   the identity firewall) — the arming step this module is built to accept.
3. The **RED live-mutation admission** above (to clear the demo wall for the `#46→#48` route only).
4. Provisioning / execution-gate parity on the host, a DARK deploy (flags OFF), then a Sponsor-present
   human-operated DEMO run — and only then a governed live test. No synthetic test asserts live readiness.

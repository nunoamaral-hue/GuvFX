# 0057 — Governed LIVE copy execution for one exact pair

- Date: 2026-10-30
- Status: Proposed

## Context

The MT5 master/follower copier (ADR-0051) was built DEMO-followers-only: `execution_producer.admit_open`
fails closed `follower_not_demo` for any non-demo follower, every produced job is `is_demo=True`, and the
copier never consults the LIVE-execution wall. A read-only P0 diagnosis confirmed the live-test pair
#46 TradersWay (master) → #48 IS6FX (follower) is correctly configured and observed end-to-end (master
events + replication intents are created), but the follower is a LIVE account, so **no flag or existing
authorization can cross the DEMO wall** — the first failing boundary is `admit_open → follower_not_demo`.

The Sponsor accepted that diagnosis and authorized building the narrowest possible fail-closed LIVE-copy
capability: permit a copied LIVE order **only** for an explicitly authorized exact pair, bounded to one
future test order, preserving the DEMO path and every existing execution-OFF default. This ADR records why
LIVE copy was previously unreachable and the new authorization chain. It does **not** authorize deployment,
production flags, a live authorization row, host changes, or an order.

## Verified facts

- `backend/trade_copier/execution_producer.py::admit_open` rejected non-demo followers (`follower_not_demo`);
  all four producers (open/close/modify/partial-close) are DEMO-only and set `is_demo=True`.
- `backend/execution/bridge_config.py::allow_live_for_account` governs the per-runtime `MT5_ALLOW_LIVE`
  boundary and was `is_live_environment(account) and live_execution_permitted(account)` — the latter being
  strategy-coupled (`execution.live_authz.is_live_execution_authorized` requires a `StrategyAssignment`-bound
  `LiveExecutionAuthorization`). A pure copier follower has no such assignment, so the host bridge would
  refuse a LIVE copier order even if the backend produced one.
- The order-time bridge gate (`scripts/mt5_signal_bridge.py::evaluate_binding`) re-reads broker truth and
  denies on login/server mismatch, classification clash, or non-demo-without-`allow_live` — the final
  money-path authority, independent of any backend gate. `ExecutionJob.save()` runs the kill-switch /
  account-stopped / broker-gate / identity-pin chain for `PLACE_ORDER`.

## Assumptions

- The follower runtime is re-rendered (re-provisioned/attached) so it picks up `MT5_ALLOW_LIVE=1` before any
  acceptance order — a host/operator step, out of scope here and documented in the runbook.
- The acceptance master OPEN will carry complete SL+TP (an unprotected position is rejected).

## Decision drivers

Safety and reversibility first; narrowest scope; no broad "allow live"; no accidental reuse of a strategy
authorization; truthful payloads; fail-closed by default; auditable; one-action disarm.

## Options considered

- **A — Add a `purpose`/scope to the strategy `LiveExecutionAuthorization`.** Rejected: risks a broad
  strategy authorization accidentally authorizing a copier order, and couples copier admission to
  strategy-assignment material-facts logic that does not apply.
- **B — A separate copier-specific authorization model + a parallel, additive LIVE predicate.** Chosen:
  complete isolation from the strategy path, exact-pair binding, bounded allowance, independent disarm.

## Decision

Introduce `trade_copier.CopierLiveAuthorization` (+ `CopierLiveKillSwitch`) and a new explicit arm flag
`COPIER_LIVE_EXECUTE_ENABLED` (ANDed with the execute master gate). For an **OPEN** only, `admit_open`
delegates a non-demo follower to `trade_copier.live_execution.admit_open_live`, which admits **only** when
every one of the following independently passes (fail-closed, re-verified under the lock at produce time):

global copier enabled · replication enabled · copier execute enabled · `COPIER_LIVE_EXECUTE_ENABLED` · no
durable kill switch · tenant/user matches · exact master account · exact follower account · exact master &
follower login/server/environment identities match the authorization snapshots · exact verified symbol map ·
follower `BrokerInstrument` enabled + authoritative fresh constraints · follower active · an unexpired,
non-revoked, allowance-remaining, human-created `CopierLiveAuthorization` for this pair · the future-event
watermark (strictly later event sequence AND observed time) · not superseded/stale · complete directional
SL+TP · sizing valid and ≤ the authorized maximum · concurrency cap · `allow_live_for_account` (the real
`MT5_ALLOW_LIVE` boundary) · no broker runtime pause.

`follower_not_demo` is **not weakened globally**: it is replaced for OPEN by (DEMO admission) OR (this
complete LIVE admission); LIVE close/modify/partial-close remain demo-only (fail-closed). The produced job is
a **truthful** `PLACE_ORDER` with `is_demo=False`, `execution_mode="LIVE"`, and the order-time identity pin
(`require_identity_pin`, `expected_login/server/environment`) so the bridge binds to the authorized identity.
`bridge_config.allow_live_for_account` is extended **additively** to accept the copier authorization
(`execution.live_authz.copier_live_execution_authorized`, DARK-gated by `HOSTED_LIVE_EXECUTION_ENABLED`); the
strategy path is unchanged. One order allowance is consumed atomically under the same row lock, so a retry or
worker recovery can never place a second order. Authorizations are created ONLY by the staff-gated
`grant_copier_live_authorization` command (never automatic, never LLM); `copier_live_disarm` engages a
durable kill switch that blocks all LIVE dispatch while preserving audit.

## Consequences

LIVE copy becomes possible for exactly one authorized pair, bounded and auditable (MasterEvent →
CopierReplication → ExecutionJob → idempotency key → follower ticket). Everything stays DARK: default-OFF
flags, no authorization rows, no host change. **Limitation (gated + documented):** a copied LIVE OPEN is not
auto-managed — LIVE close/modify/partial-close stay fail-closed, so the operator manually manages/closes the
acceptance position; the bounded allowance (max_orders=1) and concurrency cap (1) contain that risk. See
`docs/KNOWN_ISSUES.md`.

## Risks and controls

- *Unmanaged LIVE position* → bounded allowance + concurrency 1 + operator manual close (runbook). **Amber.**
- *Replay of historical/open positions* → future-event watermark (sequence + time) + supersede + freshness.
- *Identity drift / wrong pair* → full identity pin at admission AND at the bridge; exact-pair authorization.
- *Second order on retry* → allowance consumed under the row lock; idempotency key `copier-live:<pk>`.
- *Runaway* → `COPIER_LIVE_EXECUTE_ENABLED` OFF (flag) and `copier_live_disarm` (durable kill switch).

## Evidence / validation

`backend/trade_copier/tests_live_copier.py` — per-gate adversarial coverage (one killing case per gate),
truthful-LIVE-payload + exactly-once + allowance, demo-path-unchanged, and the bridge identity-pin rejections
via the real `evaluate_binding`. Focused run green (34 tests); `trade_copier` app green (389); existing
`execution` live-authz / allow-live / bridge-binding / bridge-pin suites green. Full backend suite + the
secret scan + governance check recorded in the PR handoff. Not covered here: a real broker order (never run),
host re-provisioning of `MT5_ALLOW_LIVE`, and LIVE close/modify/partial-close (intentionally fail-closed).

## Reversal path

Revert the PR (new app models + migration `0008`, the additive `allow_live_for_account` branch, the OPEN
delegation, the flag, the two commands, tests, docs) to return to the DEMO-only architecture. At runtime:
`COPIER_LIVE_EXECUTE_ENABLED` OFF and/or `copier_live_disarm` immediately disarm; revoking the authorization
denies. Migration `0008` only adds two tables and can be rolled back with `migrate trade_copier 0007`.

## Revisit trigger

Before any LIVE close/modify/partial-close capability is added; or if a second pair is requested (the
one-ACTIVE-per-pair / bounded-allowance assumptions change); or on any observed identity-pin bypass.

## Approval

Sponsor authorized the DARK build (this packet). Red approvals still required before deploy, prod flags, a
live authorization, `is_active` change, or any order. PM owns lifecycle status.

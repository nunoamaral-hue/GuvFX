# ADR-0097 — Copy Trading one-action, authorization-bound ARM / DISARM

- **Status:** Accepted (repository-only / DARK, default-OFF). Merge, deployment, host emitter enablement, secret
  provisioning, signed host admission, and any Sponsor-present live demo are **separate Sponsor gates**. No
  deploy, no host contact, no flag enablement, no authorization, no broker order is part of this packet.
- **Packet:** GFX-COPIER-ONE-ACTION-ARM-DISARM-PR-076.
- **Date:** 2026-11-12.
- **ADR number:** 0097 — the next genuinely free number on `main` (0094 hybrid real-time, 0095 Vantage currency,
  0096 post-fill binding are all merged).
- **Builds on / realises:** the GFX-COPIER-REALTIME-BACKEND-AND-EMITTER-PREFLIGHT-075 finding that the control
  plane has **no one-action ARM/DISARM** — arming a demo required a human to run ~15 env flags + 7 workers and
  re-run them between trade actions. Builds on ADR-0057 (the human `CopierLiveAuthorization` + `admit_open_live`),
  ADR-0083 (the LIVE MODIFY/CLOSE lifecycle + `_live_lifecycle_preamble`), ADR-0094 (immediate dispatch, which
  already drives OPEN/MODIFY/CLOSE event-driven once armed) and ADR-0096 (post-fill ticket binding). Nothing in
  those is weakened: the arm is an **additional AND**, never an OR bypass.

## Context — why the former ceremony was unsafe and too slow

To arm the #46→#48 demo an operator had to, per the old runbook: create the authorization, inject roughly fifteen
`COPIER_*` environment flags into the running workers, start seven single-pass worker commands
(`run_copier_observer/replication/execute/lifecycle/execute_modifies/execute_closes/mutation_dispatch`) and **re-run
them between every OPEN/MODIFY/CLOSE**, then perform the signed host admission. That ceremony is error-prone (a
missed flag silently no-ops; a forgotten worker strands a position), slow (the Sponsor drives commands between
trade actions), and leaves no single audited record of "armed for this authorization/route/window". It also means
the deployment's base flags cannot safely be configured ahead of time, because flags alone would be the only gate.

## Decision — a single-action, authorization-bound, auto-expiring backend arm

A new durable `CopierLiveArm` row is the **additional, route-exact AND-condition** the LIVE admission requires,
alongside the existing env/config gates **and** the human `CopierLiveAuthorization`. It replaces the per-demo
flag/worker ceremony with one audited command — **without lowering any gate**.

### State machine

`ENGAGED → DISENGAGED` (terminal). Absence of an engaged arm ⇒ the LIVE money path is OFF. At most **one engaged
arm per governed route** (a partial-unique constraint on `state=ENGAGED` over `(master_account, follower_account)`;
the runtime evaluator additionally fails closed on any duplicate it sees). An arm is bound to **exactly one**
`CopierLiveAuthorization` and carries an immutable route snapshot (owner, master, follower, source symbol, follower
symbol); the evaluator admits it only when that snapshot matches the authorization exactly.

### One-action ARM (`copier_live_arm`)

`copier_live_arm --authorization-id N --by <staff-email> --reason "..."` engages exactly one arm, fail-closed
unless **every backend-verifiable gate** passes: the authorization is ACTIVE, unexpired, not revoked, the sole
ACTIVE grant for the pair, with entry allowance remaining and a useful remaining window (`--min-window-minutes`);
the approver is **staff**; the kill switch is clear; there is no conflicting engaged arm; the pinned master/follower
identities match; there is no conflicting in-flight follower job; the master is flat (backend observation; waivable
with `--no-require-master-flat`); and the authoritative `copier_live_demo_preflight` reports **no backend
blockers** (host-probe items remain the separate operator step). The arm is persisted atomically under the
authorization row lock, with `expires_at` **capped at the authorization expiry**. Re-running with the same
authorization is an idempotent no-op.

**The ARM command is control-plane only.** It never alters Windows-host admission, enables a scheduled task, starts
a worker, creates an `ExecutionJob`, places/modifies/closes an order, or stores/prints a credential. Signed host
admission remains a **separate** `provision_authorized_live_copier_bridge --commit` after an independent read-back.

### Automatic expiry

Expiry is by **runtime evaluation only** (`trade_copier.arm.engaged_arm_for_authorization` checks `expires_at`):
once the arm's expiry (or the authorization's expiry, or the authorization window) passes, admission fails closed
**immediately, with no cleanup command**. `reconcile_expired_copier_authorizations` optionally marks expired arms
`DISENGAGED` for audit — this changes no runtime behaviour.

### One-action DISARM / return-to-OFF (`copier_live_arm --disengage`)

`copier_live_arm --disengage --authorization-id N --by <staff> --reason "..."` disengages the engaged arm for that
authorization and returns the backend path to OFF **without engaging the durable kill switch** (idempotent;
preserves the authorization row for audit; records operator/time/reason). A dedicated `--disengage` on the arm
command — rather than extending `copier_live_disarm` — was chosen because `copier_live_disarm` **engages the
durable kill switch**, which the normal end-of-demo return-to-OFF must not do. `copier_live_disarm` is itself
extended so the **emergency** path also disengages every engaged arm (the kill switch already blocks all dispatch;
disengaging the arms makes the OFF state unambiguous).

### Fail-closed runtime gating (the additional AND)

`require_engaged_arm(authz)` is threaded as the **final** check in `admit_open_live` and `_live_lifecycle_preamble`
(so every specific gate reason still surfaces first) and **re-checked under the authorization lock** in
`create_live_open_job` (so a disengage/expiry committed mid-flight is seen and a retry can never place an order).
Because every LIVE subgate (`copier_live_execute_enabled` and the per-op arms) is unchanged, the env gate remains
condition 1 and the arm is condition 2: with no effective arm the LIVE path is OFF, and existing behaviour is inert
when no arm exists. Route/owner/symbol drift, an expired arm, a duplicate engaged arm, or any DB error all evaluate
to "not armed" (fail closed). The arm is checked **after** `kill_switch_engaged()`, so the kill switch always wins.
`PARTIAL_CLOSE` is unchanged — it flows through `_live_lifecycle_preamble` and therefore also requires the arm, but
its own `partial_close_authority` + flag remain the separate authority and are not broadened here.

## Relationships (authority layering)

| Layer | Who sets it | What it is |
|-------|-------------|------------|
| `CopierLiveAuthorization` | human, staff-gated | the reviewed, bounded, per-pair entitlement (identity, symbol, caps, window, per-op authorities) |
| env gates (`COPIER_*`) | deployment config | the subsystem/producer master gates (condition 1) |
| **`CopierLiveArm`** | **one staff operator command** | **the per-demo, authorization-bound, auto-expiring switch (condition 2)** |
| immediate dispatcher / workers | event-driven once armed | run the governed producers; create no authority |
| signed host admission (`MT5_ALLOW_LIVE`) | PROD-OPERATOR, signed | the host runtime boundary — **separate**, never set by ARM |
| `CopierLiveKillSwitch` | one emergency command | durable block of ALL dispatch; always wins |

A LIVE order requires **all** of: the env gates, a complete unexpired authorization, an engaged route-exact arm,
the per-op authority, the identity/symbol/sizing gates, the signed host admission, and no kill switch.

## What one-action ARM does and does NOT do

- **Does:** record one audited, authorization-bound, auto-expiring backend arm after re-running the preflight.
- **Does NOT:** contact the host, set `MT5_ALLOW_LIVE`, enable a task, start a worker, create a job, place an
  order, or hold any secret. Arming is necessary but **not sufficient**; the signed host admission is still
  required and is a separate operator action.

## What DISARM does and does NOT prove

Backend DISARM makes the arm-dependent runtime gates false immediately. It **does not prove** the Windows host
admission is OFF — `MT5_ALLOW_LIVE` on the follower bridge is cleared only by the signed
`restore_copier_bridge_execution_off`. The command output states this explicitly; a complete system-wide OFF is
never claimed from the backend arm row alone.

## No-LLM-live-trading boundary

This ADR adds no model/LLM path to arming. ARM is a human staff operator command bound to a human-created
authorization; the arm grants no authority of its own. Nothing here lets model output place, size, or approve a
live or paper order — the human-gated control path is preserved and made auditable, not automated.

## Operator sequence (after eventual deployment — not performed here)

1. Human creates the bounded `CopierLiveAuthorization`.
2. Operator runs one `copier_live_arm` command.
3. Independent read-back verifies the exact bounded arm (authorization, route, window).
4. Operator performs the signed host-admission commit.
5. Independent read-back verifies the follower bridge.
6. Human opens / modifies / closes the master trade.
7. The real-time pipeline (immediate dispatch + post-fill binding) handles the follower lifecycle.
8. Operator runs `copier_live_arm --disengage` and the signed host OFF restoration.
9. Independent zero-state verification.

## Incident / containment path

`copier_live_disarm` engages the durable kill switch (blocks ALL dispatch, survives flag flips/restart) **and**
disengages every engaged arm; then `restore_copier_bridge_execution_off --commit` restores the host; then the
authorization is revoked and any residual follower position is closed manually. The kill switch is cleared only
deliberately.

## Consequences / known limitations

- With no arm (today's state) nothing changes — the LIVE path is OFF exactly as before; this ADR is inert until a
  Sponsor both configures the base gates and runs the ARM command.
- The `#46` emitter is already installed DISABLED and byte-verified (packet 075); secret provisioning and
  ingestion verification remain separate future steps. This ADR neither deploys nor enables the emitter.
- One migration (`0014_copier_live_arm`, additive CreateModel). No change to any existing table.

## Separately gated steps (NOT in this packet)

1. Merge. 2. DARK deployment (+ migration 0014). 3. Host emitter enablement + secret provisioning.
4. Base-flag configuration. 5. `copier_live_arm` + signed host admission. 6. Sponsor-present bounded live demo.

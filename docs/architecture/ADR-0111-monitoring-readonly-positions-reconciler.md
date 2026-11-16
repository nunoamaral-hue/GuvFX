# ADR-0111 — Monitoring-only read-only-positions bridge reconciler

- **Status:** Accepted (repository-only; additive; migration-free; **default DRY-RUN**; no execution / Copy-Trading
  authority; no `MT5_ALLOW_LIVE`; no production/host mutation, no bridge restart, no allow-list population, no flag
  enablement by this packet).
- **Packet:** GFX-DASHBOARD-LIVE-POSITIONS-PROVISIONING-WIRING-137.
- **Date:** 2026-11-15.
- **Builds on (never replaces):** ADR-0110 (the account-level `LIVE_READONLY_POSITIONS_ACCOUNT_ALLOWLIST` decision
  `readonly_positions_for_account` + the host env-key permit), ADR-0072 (the bridge read flag
  `MT5_LIVE_READONLY_POSITIONS_ENABLED`, consulted only in `fetch_positions`), and ADR-0049/0049.1 (the signed
  per-tenant bridge-env transport: `render_bridge_env`, `host_executor`, `RESTORE_TENANT_BRIDGE_EXECUTION_OFF`, the
  `MT5_ALLOW_LIVE` posture gate). The copier `live_bridge_provision.provision_live` / `restore_off` path and every
  execution/admission gate are **unchanged**.

## Context

ADR-0110 merged the account-level read-only allow-list **decision**, but it was inert: `readonly_positions_for_account`
had **no production render/apply call site**, so no account's bridge could ever be rendered with the read flag. The
six host-gated accounts (#48, #53, #54, #56, #57, #59) therefore remained structurally unavailable. This packet
supplies the missing wiring — a **monitoring-only** provisioning operation — without granting any execution authority.

## Decision

Add a dedicated, repository-only reconciler (`hosted_workspace/monitoring_reconcile.py`) and a dry-run-default
management command (`reconcile_monitoring_readonly_positions`). For EXACT, owner-authorized, identity-pinned,
allow-listed, LIVE, persistent-workspace account id(s) it:

1. selects only that owner's named persistent-workspace account(s) — never a global sweep;
2. calls `readonly_positions_for_account(account)` (the sole arming authority) via a fail-closed gate set;
3. renders the execution-OFF env **with** `MT5_LIVE_READONLY_POSITIONS_ENABLED=1` (`render_bridge_env(allow_live=False,
   readonly_positions=True)`) only when the account passes every gate;
4. proves the rendered env differs from the captured byte-exact baseline (read flag absent) by **exactly** the one
   read-only line — any other delta fails closed (`_env_delta_is_only_readonly`);
5. (only with `--commit`) applies it via the **signed `RESTORE_TENANT_BRIDGE_EXECUTION_OFF`** transport — which
   narrowly restarts that one tenant's bridge and attests `MT5_ALLOW_LIVE` is OFF — then verifies the signed
   attestation (`expect_live=False`) and performs a positions **read-back**; on any attestation/read-back failure it
   rolls that account back to its captured baseline.

The command defaults to **DRY-RUN** (renders + previews, contacts no host). `--commit` constructs the real signed
transport; `--teardown` removes only the read-only flag. Bounded concurrency or sequential; per-account isolation and
truthful reporting.

### Why reuse `RESTORE_TENANT_BRIDGE_EXECUTION_OFF`

A monitoring-enabled env **is** an execution-OFF env (no `MT5_ALLOW_LIVE`) that additionally carries the read flag.
The RESTORE transport already (a) requires the env to carry **no** `MT5_ALLOW_LIVE`/`MT5_LIVE_EXPIRY` (host-side
`require_allow_live=False` posture gate), (b) re-validates every env key against `_BRIDGE_ENV_ALLOWED_KEYS` (ADR-0110
permits the read key), and (c) restarts only that tenant's bridge. Reusing it means **no new host primitive** and that
the no-execution contract is enforced by an already-reviewed, signed path. A dedicated execution op would add host
surface for no benefit.

## Safety invariants (enforced here + host-side, tested)

- **Never** renders/sets/infers/carries `MT5_ALLOW_LIVE` or `MT5_LIVE_EXPIRY` — the render is always
  `allow_live=False` and asserted free of both; the only introducible delta is the single read-only line.
- **Never** invokes an execution/admission/arm op; it only ever dispatches RESTORE.
- Copy-Trading / follower execution untouched: enabling an account's read (including #48) can neither arm nor admit
  execution, and the reconciler **refuses** to act on an account that currently holds an active live-execution
  authorization or arm.
- Fail-closed refusals: empty/malformed allow-list, unapproved account, cross-owner, identity mismatch, unsupported
  workspace state, missing endpoint, demo account, any env delta beyond the read-only flag, a mid-execution account.
- Default dry-run mutates nothing; the real host transport (`_RealMonitoringOps`) is operator-run and never exercised
  in CI (tests inject a fake); the read-back uses synthetic fixtures only.

## Consequences / out of scope (separately Sponsor-gated — HARD STOP for this packet)

This PR delivers the **operation**, default dry-run, touching no production/host. Making the six accounts actually
readable still requires, as separate governed steps: (1) populate `LIVE_READONLY_POSITIONS_ACCOUNT_ALLOWLIST`;
(2) a human-approved `--commit` run of this command over RDP/operator path to apply + restart each bridge (**host
mutation**); (3) DARK deploy of `main`; (4) owner VISUAL acceptance. The `_RealMonitoringOps.read_back_positions`
real wiring is likewise left to that operator step so CI contacts no bridge. Prod backend remains `ac5bdf0a`.

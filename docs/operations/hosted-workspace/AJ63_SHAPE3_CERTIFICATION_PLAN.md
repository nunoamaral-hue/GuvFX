# AJ#6.3 — Shape-3 capability recovery + explicit authorization: certification plan

**Status:** plan (branch `feat/aj63-execution-authorization`, DARK). **Do not merge/deploy until Sponsor
approves.** Target certification subject: **support@ / account 24 / `guvfx_u_24` on Node 2** (the live demo
acceptance runtime, CONNECTED + matched + `trade_allowed=False`). Customer Zero (acct 1) and account 18 remain
untouched throughout.

## 1. What is on the branch (the reviewable unit — 3 commits)

| Commit | Contents |
|---|---|
| `require explicit customer authorization before arming (ADR-0047)` | `execution_authorized_at` (migration 0008); `_arm_preconditions` `ARM_NOT_AUTHORIZED` gate (binds cron + operator); readiness belt-and-braces `RW_EXECUTION_NOT_AUTHORIZED`; `auto_arm_runner` authz filter; customer `authorize_workspace_execution` + `OnboardingAuthorizeExecutionView`; journey read-model exposure; admin read-only; ADR-0047 + ADR-0044 Decision-2 superseded. |
| `per-tenant RELAUNCH_TERMINAL signed primitive (Gap-1)` | `RELAUNCH_TERMINAL` host op across the signed no-RCE transport + `Relaunch-GuvfxTerminal.ps1` (ASCII, CZ-refused, tenant-confined, graceful). DARK. |
| `Shape-3 post-login capability recovery + Enable-automated-trading UX` | `capability_recovery.py` runner (migration 0009 loop-safety fields; flag `HOSTED_CAPABILITY_RECOVERY_ENABLED`); wired into `run_cycle`; frontend "Enable automated trading" control + `authorizeExecution()`. DARK. |

Everything is DARK by default. `HOSTED_CAPABILITY_RECOVERY_ENABLED` and the ADR-0047 authorization endpoint
are additive; nothing changes behaviour for any existing account until the flag is enabled and a customer
explicitly authorizes.

## 2. Deployment (DARK-safe, when approved — the proven pipeline)

1. Merge FF to `main` on green CI.
2. **CZ Golden STOP-check BEFORE** — capture the Customer-Zero golden structural SHA (must match the last
   recorded value) and record account/job baselines.
3. Backend: tag rollback image → build from the merged commit → **migrate first** (0008 + 0009 are additive
   `AddField`s; existing rows: `execution_authorized_at=NULL`, `capability_recovery_*` inert) → recreate
   backend + the observation-scheduler service.
4. Re-stage the host-executor bundle so `Relaunch-GuvfxTerminal.ps1` reaches the host (it is in
   `deploy/hosted-executor/stage-manifest.json`); ParseFile-gate it on the host (RULE 9) before first use.
5. Frontend: build + deploy (adds the read-model fields + the Enable control; the control only renders when
   the server reports `can_enable_automated_trading`).
6. **CZ Golden STOP-check AFTER** — byte-identical to BEFORE. Any difference ⇒ STOP + rollback.
7. Enable **only** `HOSTED_CAPABILITY_RECOVERY_ENABLED=1` (the single new flag). Do **not** change any other
   flag. `HOSTED_MT5_EXECUTION_ENABLED` stays as-is; the ADR-0047 gate makes reaching EXECUTION_READY unable to
   auto-arm regardless.

## 3. Pre-certification proofs (must all hold before driving account 24)

Prove and record (read-only) for account 24:

- [ ] **No AUTO_DEMO/LIVE execution authorization exists** — `HostedMt5Workspace.execution_authorized_at IS
  NULL`; no `StrategyAssignment` for account 24 (checked 2026-08-17: zero).
- [ ] **`execution_enabled = False`** (checked: False; and `auto_arm_suppressed=True` interim containment).
- [ ] **Zero routable jobs/orders** — `ExecutionJob` count for account 24 = 0 (checked: 0).
- [ ] **Customer Zero Golden captured** — structural SHA recorded, matches baseline.
- [ ] **Automatic arming is impossible** — with the ADR-0047 gate live: `_arm_preconditions` returns
  `ARM_NOT_AUTHORIZED` and `auto_arm_runner`'s filter excludes any workspace with `execution_authorized_at IS
  NULL`; confirm via a read-only dry check that account 24 is not an auto-arm candidate even at EXECUTION_READY.

## 4. Certify Shape-3 on account 24 (autonomous up to the STOP)

Starting state: `Enabled=0` in the terminal / `trade_allowed=False` / canonical `CONNECTED`.

1. The every-minute cycle runs `run_hosted_capability_recovery` (now armed): account 24 is a candidate
   (CONNECTED + matched + `trade_allowed=False` + fresh + demo, not CZ).
2. It re-asserts `AllowLiveTrading=1 / Enabled=1` (`apply_autotrading_config`) → gracefully relaunches the
   tenant's OWN terminal (`RELAUNCH_TERMINAL`). One attempt claimed (count→1); bounded (≤3, 5-min cooldown).
3. The observer re-proves the **same** broker identity + `trade_allowed=True` on a subsequent cycle.
4. Manager derives canonical **`EXECUTION_READY`**.
5. **Verify (must hold):** `execution_enabled` **remains False**; `execution_authorized_at` **remains NULL**;
   ARMED remains False; **zero** ExecutionJobs/orders; Guacamole reconnects to the same session; no restart
   loop (count stops incrementing once `trade_allowed=True`); no cross-tenant effect; CZ Golden byte-identical.
6. The UI presents the explicit **"Enable automated trading"** control (server `can_enable_automated_trading=
   true`), with the capability≠consent copy.

## 5. STOP for the customer action

**STOP.** The Sponsor personally clicks **Enable automated trading** in the UI. **Claude must not perform this
authorization** (never POST `/onboarding/authorize-execution/`, never arm by any path).

## 6. Post-click verification (after the Sponsor's click)

- [ ] `execution_authorized_at` is stamped + `HOSTED_EXECUTION_AUTHORIZED` audit event exists.
- [ ] `execution_enabled` / ARMED change **only** as designed (armed only after the authorization; the
  `_arm_preconditions` chain re-proved).
- [ ] Wayond becomes operational as the customer's chosen strategy (selected via the marketplace signal-copy
  card; its own arm gates apply), subject to all existing safety gates.
- [ ] The **live order-time bridge gate** (`evaluate_binding`) remains intact and is the sole order authority.
- [ ] Customer Zero Golden remains **byte-identical**; account 18 untouched.

## 7. Rollback

- Backend: recreate from the rollback image tag (migrations are additive; the NULL columns are inert if the
  code is reverted). Frontend: redeploy prior image.
- Disable `HOSTED_CAPABILITY_RECOVERY_ENABLED` to instantly stop the recovery edge (DARK no-op).
- The interim `auto_arm_suppressed=True` on account 24 remains a belt-and-braces guard until the legitimate
  authorization+arm clears it.

## 8. Safety invariants (structurally enforced, verified by adversarial review — 0 breaks)

- Capability recovery **never** arms, authorizes, or routes an order; it only re-asserts config + relaunches,
  bounded/loop-safe; the observer (not the runner) advances state.
- Reaching EXECUTION_READY can **never** auto-arm (ADR-0047): the customer's explicit click is the only path.
- Customer Zero is excluded at four layers (candidate query, signed dispatch reserved-id, executor confinement,
  `.ps1` constant). The relaunch closes/relaunches only the tenant's **own** running terminal.
- Provider A / Customer Zero readiness is byte-unchanged.

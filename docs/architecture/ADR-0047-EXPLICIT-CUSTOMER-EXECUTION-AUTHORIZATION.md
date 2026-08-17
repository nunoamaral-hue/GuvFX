# ADR-0047 — Explicit customer authorization to execute (capability ≠ authorization)

- **Status:** Proposed (Sponsor-directed 2026-08-17; PM owns lifecycle status)
- **Supersedes:** ADR-0044 Decision 2 ("Autonomous customer-specific arming")
- **Scope:** Provider-B hosted workspaces (`readiness_provider == persistent_workspace`). Provider A / Customer
  Zero (`temporary_validation`) is entirely unaffected.
- **Flags:** the whole hosted-execution subsystem remains gated (`HOSTED_PERSISTENT_MT5_ENABLED`,
  `HOSTED_MT5_EXECUTION_ENABLED`). This ADR adds no flag; it narrows an existing behaviour.

## Context

MT5 automation **capability** and customer **authorization** were conflated. A hosted workspace reaches
canonical `EXECUTION_READY` when the observer proves the terminal is connected, matched, fresh, and
`trade_allowed` — a *mechanical capability* fact. Under ADR-0044 Decision 2, the autonomous `auto_arm_runner`
then flipped the durable per-workspace arm (`HostedMt5Workspace.execution_enabled = True`) with **no customer
action** — its candidate filter was `canonical_state == EXECUTION_READY AND execution_enabled == False AND
auto_arm_suppressed == False`.

The product contract (Sponsor 2026-08-17) is the opposite:

> MT5 automation capability ON ≠ customer authorization to trade. The flow must be: broker login → post-login
> capability recovery → `trade_allowed = True` → `EXECUTION_READY` → **execution remains unarmed** → the
> customer explicitly clicks "Enable automated trading" → only then may the strategy/workspace become armed.

There was **no durable per-workspace "customer authorized execution" record**: `execution_enabled` is the arm
*result* (operator/autonomous-written); `auto_arm_suppressed` is an operator *disarm* intent; and
`TradingAccount.workspace_confirmed_at` is the earlier account-*identity* ACK, documented as explicitly
non-arming. So nothing existed for the arm to require.

## Decision

Arming a hosted workspace requires a **durable, explicit, owner-scoped customer authorization**. Reaching
`EXECUTION_READY` can never, by itself, arm.

1. **New durable field** `HostedMt5Workspace.execution_authorized_at` (nullable, default `NULL` = not
   authorized). Written **only** by the customer endpoint below — never by a migration, an observation/
   lifecycle event, the auto-arm runner, or a convenience helper.
2. **Arm chokepoint.** `execution.hosted_provisioning._arm_preconditions` fail-closes with
   `ARM_NOT_AUTHORIZED` while `execution_authorized_at is None`. Because `arm_hosted_workspace_execution` is the
   single writer of `execution_enabled=True` and both the autonomous runner and the operator command call it,
   this one check binds **every** arming path (no operator break-glass; a break-glass, if ever needed, must be a
   separate, explicit, audited action).
3. **Belt-and-braces order gate.** `execution.readiness.PersistentWorkspaceProvider.evaluate` additionally
   denies (`RW_EXECUTION_NOT_AUTHORIZED`) any row where `execution_authorized_at is None`, placed after the
   connected/matched/confirmed terms so the more-specific reasons stay reachable. This fail-closes any row that
   was armed autonomously **before** this correction — with **no data migration**.
4. **Auto-arm narrowed.** `auto_arm_runner` adds `execution_authorized_at__isnull=False` to its candidate
   filter. It is no longer an autonomous *arming* path; it can only **complete** an arm the customer already
   authorized (e.g. re-apply it after a transient `EXECUTION_READY` flap).
5. **The customer authorization** is `hosted_workspace.provisioning.authorize_workspace_execution` (POST
   `/api/hosted-workspace/onboarding/authorize-execution/`): owner-scoped (IDOR-safe), requires the account
   confirmed and the workspace observed CONNECTED + matched and canonically `EXECUTION_READY`, idempotent,
   audited (`HOSTED_EXECUTION_AUTHORIZED`), accepts no secret. It records `execution_authorized_at` and then
   attempts the certified arm.
6. **UI truthfulness.** The onboarding-journey projection exposes `execution_ready` / `execution_authorized` /
   `execution_armed` / `can_enable_automated_trading` so the customer sees a distinct "ready — not yet enabled"
   state and the explicit "Enable automated trading" control appears only at `EXECUTION_READY` while
   unauthorized.

The live order-time bridge gate (`scripts/mt5_signal_bridge.py::evaluate_binding`) remains the **sole order
authority** and is unchanged; nothing here places or authorises an order.

## Consequences

- Reaching `EXECUTION_READY` leaves a workspace **unarmed and not order-eligible** until its owner explicitly
  authorizes — the desired contract. `execution_enabled == True` now **implies** a prior customer authorization.
- The operator `provision_hosted_execution --arm` is also bound by the authorization requirement (intended).
- A future customer "Disable automated trading" should clear `execution_authorized_at` (full revocation) or set
  `auto_arm_suppressed`; otherwise the runner would re-arm an authorized-but-disabled workspace. (Operator
  disarm already sets `auto_arm_suppressed`; the symmetric customer disable is a follow-up.)
- Durable authorization persists across a customer switching their active broker account; that mismatch is
  contained by the live order gate (`proj_account_match` + identity pin re-checked before every order), not by
  this flag.
- Provider A / Customer Zero: the authorization term lives only in `PersistentWorkspaceProvider`, so the
  `TemporaryValidationProvider` path is byte-unchanged.

## Verification

`hosted_workspace/tests_execution_authorization.py` (16 tests) proves: the arm chokepoint refuses while
unauthorized and succeeds once authorized; the auto-arm runner does not even consider an unauthorized ready
workspace; the readiness belt-and-braces denies a legacy armed-unauthorized row; `authorize_workspace_execution`
is owner-scoped / requires-confirmed+ready / idempotent / then arms; the endpoint is 404-dark, 409-until-ready,
200-arms; Provider A is unaffected. The end-to-end journey test
(`tests_beta_journey_e2e::test_full_autonomous_hosted_journey`) now proves reaching `EXECUTION_READY` does NOT
auto-arm and only the explicit authorization arms.

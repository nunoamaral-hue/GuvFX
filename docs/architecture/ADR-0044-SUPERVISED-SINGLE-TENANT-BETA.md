# ADR-0044 — SUPERVISED_SINGLE_TENANT_BETA (bounded interim posture) + autonomous hosted arming

- **Status:** ACCEPTED FOR REPOSITORY IMPLEMENTATION — Sponsor (Nuno), 2026-08-14. Ships **DARK**
  (`SUPERVISED_SINGLE_TENANT_BETA_ENABLED` default OFF; hosted execution/observation flags default OFF). No
  deploy, no arming, no Customer-Zero mutation are authorised by this ADR. *(Notion owns the approved-ADR
  lifecycle; git status may lag.)*
- **Date:** 2026-08-14 · **Programme:** Beta Launch Critical Path.
- **Relates to / amends:** ADR-0041 (Hosted Workspace observation trust model), ADR-0043 (W^X native-code
  elimination + Addendum B co-residency guard), ADR-0033/0034 (persistent workspace + onboarding/execution).

## Amendment 1 — CLOSED TRUSTED BETA host co-residency exception (Chief Architect, 2026-08-14, FINAL)

**Decision (Option A approved).** The supervised single-tenant posture is amended so that **during the CLOSED
TRUSTED BETA only**, a supervised beta user MAY be hosted on the SAME physical Windows VPS as Customer Zero.
This supersedes the earlier interpretation that the supervised beta requires a physically separate host. The
trust model differs from public launch (trusted users, supervised operation, DEMO-only, no hostile tenants, no
public access); STREAM 10E behavioural certification remains mandatory before public launch.

**The ONLY change (repository).** The supervised single-tenant predicate (`node_is_single_tenant_for`) is
re-scoped from the **physical host (`rdp_host`)** to the **`TerminalNode`**: it now requires *one supervised
beta tenant per isolated node*, not *one tenant per physical box*. Concretely, the pre-amendment rdp_host
occupancy aggregation (the finding-I1 hardening) is removed; occupancy is counted on THIS node only. Nothing
else is touched.

**Explicitly retained — NOT relaxed:** G5/NTFS ACL isolation, W^X, AppLocker, RemoteApp isolation, separate
Windows identities, separate MT5 runtimes, separate `TerminalNode`s, the co-residency guard, DEMO-only, and
`AUTO_LIVE` disabled. **Customer Zero protection is intact by construction:** a beta may NEVER bind to Customer
Zero's node — condition (6) (`forbidden_execution_node_ids`, checked unconditionally) still rejects it — so the
beta always occupies its OWN isolated node, and Customer Zero keeps its node untouched.

**Operational note.** For the beta to co-reside, the operator must NOT place Customer Zero's `rdp_host` in
`HOSTED_BETA_FORBIDDEN_RDP_HOSTS` (that setting is the host-level exclusion; the node-level exclusion via CZ's
own node remains regardless).

**Expiry (permanent invariant).** This exception dissolves the instant STREAM 10E completes and
`HOSTED_REMOTEAPP_ISOLATION_CERTIFIED` is set (the cert branch of the trust anchor then governs, with no
single-tenant requirement). It **MUST NOT** survive into Public Launch. The `SUPERVISED_SINGLE_TENANT_BETA_ENABLED`
flag remains default-OFF; this amendment does not arm anything.

**Scope note (mechanism is broader than the framing).** A per-`TerminalNode` predicate cannot distinguish
"the other node on this box holds Customer Zero" from "…holds another beta," so the mechanism also permits
TWO supervised betas to share one physical host (each on its OWN isolated node) — not only beta↔Customer-Zero.
This is the unavoidable consequence of the host→node re-scope and lands inside this ADR's already-accepted
worst-case blast radius ("a beta tenant reaches only other disposable beta tenants on a throwaway host") under
a CLOSED, supervised, DEMO-only, no-hostile-tenant beta. Customer Zero protection is unchanged (its node is
always forbidden). This too expires with STREAM 10E. A residual to note: with the host-level backstop removed,
Customer-Zero protection now rests SOLELY on condition (6) / `forbidden_execution_node_ids` containing CZ's
node — true for a normally-running CZ live terminal; keep CZ's node bound (do not clear `account.terminal_node`
while CZ's workspace binding is also absent).

**Verification.** `hosted_workspace/tests_supervised_beta.py`:
`test_beta_coresident_on_shared_host_own_node_is_single_tenant` (co-residency on own node → single-tenant),
`test_beta_forbidden_on_customer_zero_own_node` (CZ's node still forbidden), and the unchanged same-node tests
(a second tenant on the SAME node still closes the gate).

## Context

The accepted beta architecture is node-based, with co-residency of hosted tenants made safe by the isolation
stack and gated on a behavioural certification, `HOSTED_REMOTEAPP_ISOLATION_CERTIFIED` (ADR-0041). That marker
is the root trust anchor: `live_observe.live_observe_fn` fail-closes on it, so with it absent no Hosted
Workspace can reach `EXECUTION_READY`, and the first end-to-end product journey cannot complete.

Producing that marker requires a physical isolated host + an on-host escape battery + a Nuno-supplied demo
broker login + a live AppLocker Enforce flip — genuinely multi-day, human-gated infrastructure work. The
Sponsor decided (2026-08-14) that the FIRST journey should **prove the product works end-to-end** under an
explicitly bounded interim posture, WITHOUT weakening or faking the certification.

Two concrete repository defects also blocked the autonomous journey and are fixed here (they are not policy):
1. the self-serve arm readiness gate (`strategies._account_execution_ready`) was hard-wired to the legacy
   `terminal_provisioning.AccountRuntime`, which a Provider-B hosted account does not have — so "Enable
   Trading" 409'd `runtime_not_ready` for every hosted account;
2. arming a hosted workspace's execution (`execution_enabled` + node binding) and activating the intent
   account were operator-CLI-only steps (`provision_hosted_execution --arm`; no activation at all) — a manual
   per-customer action, which the acceptance rule defines as a beta failure.

## Decision

### 1. A bounded operational state, `SUPERVISED_SINGLE_TENANT_BETA`

Introduce a flag `SUPERVISED_SINGLE_TENANT_BETA_ENABLED` (default OFF) and a fail-closed predicate
`hosted_workspace.supervised_beta.supervised_single_tenant_beta_active(workspace)`. The live-observe trust
anchor becomes an **OR**: observation is produced when the full cert holds **or** the supervised predicate
holds for *that* workspace. The predicate returns True ONLY when EVERY boundary condition holds:

1. `SUPERVISED_SINGLE_TENANT_BETA_ENABLED` is on;
2. the workspace resolves to a real `TradingAccount`;
3. that account is **not** Customer Zero (`tenant_isolation` canonical definition);
4. the account is a **DEMO** account (demo-only wall);
5. the workspace is bound to an **ACTIVE** execution `TerminalNode`;
6. that node is **not** a Customer-Zero / configured-forbidden node — derived LIVE from the DB via
   `forbidden_execution_node_ids`, checked UNCONDITIONALLY (independent of the co-residency-guard flag);
7. the node is **single-tenant for this account** — no other live legacy account and no other hosted
   workspace (execution OR delivery binding) occupies it.

Any ambiguity or exception → False (fail-closed).

**This is NOT the certification and emits NO certification marker.** `HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`
and `HOSTED_OBSERVATION_CERTIFIED` remain unset and unimplied. The posture is a coarse operational carve-out
that **bounds** the still-un-certified forgeable-observation risk (ADR-0041) to a single supervised,
disposable, demo tenant on a throwaway non-CZ host. Same production code paths in both postures — only the
gate differs. When the full cert lands, the flag is turned OFF and the posture dissolves with **no** code
change (the OR simply resolves via the cert).

### 2. Autonomous customer-specific arming (no per-user operator CLI)

The autonomous journey now performs, idempotently and fail-closed, every customer-specific step needed to make
a hosted workspace executable:
- **Activation:** `confirm_broker_account` (the customer's human ACK on a CONNECTED + matched workspace) sets
  `is_active=True` atomically with the confirmation stamp. (Both readiness and arm require `is_active`; the
  intent account was created `is_active=False`.) The node-occupancy metric already anticipates a live hosted
  account.
- **Execution arming:** a thin driver `auto_arm_runner.run_hosted_auto_arm` (wired into the existing
  `run_hosted_observations` cron cycle) calls the certified `arm_hosted_workspace_execution` for any workspace
  that has reached canonical `EXECUTION_READY` but is not yet armed. It re-proves EVERY arm precondition; it
  cannot arm anything the operator command could not. DARK unless master + execution flags are on.
- **Readiness:** `_account_execution_ready` delegates to the certified persistent-workspace gate for Provider
  B (Provider A byte-unchanged), and the self-serve arm/readiness path skips the stored-credential gate for
  Provider B (which never stores a broker password by design).

## Invariants (permanent while the flag can be on)

- **No faked cert.** The supervised posture never sets/derives `HOSTED_REMOTEAPP_ISOLATION_CERTIFIED`.
- **Single tenant, non-CZ, demo.** The predicate fails closed the instant a second tenant shares the node, the
  node is CZ/forbidden, the account is CZ, the account is not demo, or the node is not ACTIVE.
- **Execution safety unchanged.** Observation is capability-only; the certified order-time bridge gate and the
  per-job runtime-identity pin remain the sole order authority. Demo-only walls (readiness condition 11, arm
  precondition, account `is_demo`) are untouched. Disarm / node-unbind still win (reversible).

## Consequences

- **Reversibility:** instant — turn the flag off (posture gone) or disarm. No schema migration.
- **Scope of trust relaxation:** exactly one demo tenant's OWN readiness on a dedicated disposable host; the
  worst case is that tenant advancing its own demo account's readiness on a forged observation, which the
  demo-only walls + independent order-time authority already contain.
- **Not obsoleting isolation work:** the full escape-battery certification is still required before a SECOND
  tenant, before co-residency with Customer Zero's live host, and before public launch.

## Adversarial-review hardening (2026-08-14, 0 HIGH / 4 MEDIUM, all fixed)

A six-lens adversarial review (multi-tenant escape, CZ/cert-fake, arm bypass, darkness, activation
side-effects, concurrency) surfaced 0 HIGH and 4 MEDIUM findings — all confirmed and closed, all tightening
the single-tenant / reversibility boundary:

1. **Single-tenant at the physical host, not the node row.** `node_is_single_tenant_for` keyed on
   `TerminalNode.pk`; two node rows sharing one `rdp_host` (one box) each read single-tenant. Now occupancy is
   counted across ALL ACTIVE node rows sharing the node's `rdp_host` (the same host identity the co-residency
   guard uses), fail-closed on a blank `rdp_host`.
2. **Disarm survives auto-arm (reversibility).** A deliberate `disarm` was re-armed next cron cycle. Added a
   durable `HostedMt5Workspace.auto_arm_suppressed` (migration 0006): `disarm` sets it, only an explicit `arm`
   clears it, and `auto_arm_runner` excludes suppressed workspaces.
3. **Robust node-capacity accounting.** An activated hosted account whose `terminal_node` was cleared (desync)
   escaped both terms of `_node_has_capacity`, letting the allocator over-fill. Now it counts DISTINCT occupant
   accounts across `terminal_node` ∪ `execution_node`.
4. **Single-tenancy enforced at the ORDER gate, not only at observation.** The supervised single-tenant
   property is now also required in Provider-B readiness and `_arm_preconditions` while uncertified
   (`RW_SUPERVISED_BOUNDARY`) — so a second tenant landing fails the first's next order closed, with no
   observation-freshness window. No-op when certified (co-residency allowed) or when the supervised flag is off.

## Why this is an approved decision (not self-accepted)

It relaxes the observation trust anchor (an ADR-0041 security posture) for a bounded case, so under
`.claude/rules/architecture.md` it requires an approved decision before merge. The Sponsor's 2026-08-14
decision is that approval; the build ships DARK and its activation on a real beta host remains a separate
operational gate.

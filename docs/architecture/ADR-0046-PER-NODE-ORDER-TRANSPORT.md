# ADR-0046 — Per-node ORDER-TRANSPORT selection seam (Closed-Beta co-residency)

- **Status:** ACCEPTED (Sponsor + Chief Architect, 2026-08-14 — "Option A APPROVED", an authorised exception
  to the Closed-Beta engineering freeze for THIS blocker only). Ships **DARK** (inert until
  `HOSTED_PERSISTENT_MT5_ENABLED` is on AND a node carries an explicit order endpoint). No deploy, no arming,
  no Customer-Zero mutation are authorised by this ADR. *(Notion owns the approved-ADR lifecycle; git may lag.)*
- **Date:** 2026-08-14 · **Programme:** Beta Launch Critical Path — First Supervised Beta User.
- **Relates to / amends:** ADR-0034 (Execution Engine — per-job identity pin, hosted routing), ADR-0044
  (supervised single-tenant beta + host co-residency amendment). Closes the order-transport gap that
  amendment left open.

## Production-premise correction (Sponsor + Chief Architect, 2026-08-14, FINAL)

**The original premise "Customer Zero is legacy / non-hosted → global bridge" is FALSE in production.**
Customer Zero (account 1) was migrated to a **Provider-B persistent workspace** (`readiness_provider =
persistent_workspace`, a `HostedMt5Workspace` on node 1) and `HOSTED_PERSISTENT_MT5_ENABLED = 1` is ON in
prod. So `is_hosted_workspace_account(CZ)` is True and the resolver classifies **Customer Zero's own orders
as HOSTED**. The corrected architecture keeps ZERO hosted→global fallback and is uniform:

> **Provider-B execution → authoritative execution node → that node's EXPLICIT `order_bridge_base_url`.**

- **Customer Zero** = Provider-B on node 1 → node 1's explicit endpoint = the **existing CZ `:8788` bridge**.
  (Node 1's `order_bridge_base_url` records where CZ's bridge already is; it is metadata alignment, NOT a new
  route, NOT a port change, NOT a global pin flip on CZ's bridge.)
- **Each beta** = Provider-B on its OWN node N → node N's explicit endpoint = a dedicated pin-enforcing bridge
  on its own port. A beta can NEVER resolve to CZ's `:8788`.
- A Provider-B node with **no** explicit endpoint fails **closed** — never a global fallback.

**Deployment ordering is load-bearing** (a first attempt deployed the new code with node 1's endpoint still
blank, which would have fail-closed CZ's ExecutionJob path — caught by the Golden AFTER-check and rolled back
with zero CZ impact). The corrected sequence is: Golden BEFORE + backup → **migrate schema while OLD code
runs** → **populate node 1's `order_bridge_base_url` = the existing `:8788` URL (derived from the running
config, read back, verified) BEFORE any code cutover** → pre-cutover assertions → recreate services on the new
image → Golden AFTER. Rollback covers BOTH the runtime image AND node 1's metadata (its prior value is
recorded before the write). The **primary Customer-Zero safety proof is a production-like fixture where
Customer Zero itself is Provider-B** (`execution/tests_order_transport.py::ProductionPremiseProviderBRoutingTests`);
the Provider-A/legacy fixtures are retained but are NOT the CZ safety proof.

## Context — the blocker

The ADR-0044 amendment lets a supervised beta tenant co-reside with Customer Zero on ONE Windows VPS, each on
its OWN isolated `TerminalNode` (separate Windows identity, MT5 runtime, NTFS/G5 ACL, W^X, AppLocker,
RemoteApp). A read-only host preflight (2026-08-14) found that amendment isolated everything **except the
order transport**:

- The dispatch worker (`mt5_trade_ingest_worker`) POSTs every order to **one global** bridge URL
  (`AGENT_ORDER_BASE = AGENT_BASE`); there is no per-node order-bridge selection. The worker also has no
  node/account claim scope of its own (the claim endpoint scopes by the worker's `authorized_nodes`).
- A HOSTED (Provider-B) order is safe only on a bridge running `MT5_REQUIRE_IDENTITY_PIN=1`
  (`scripts/mt5_signal_bridge.py::verify_execution_binding`, line ~506: under the pin every payload without
  `expected_login`/`expected_server` fails closed `identity_pin_required`).
- On the co-resident box that single global bridge **is Customer Zero's legacy `:8788` bridge**, and CZ's
  legacy orders carry no payload pin. So setting `MT5_REQUIRE_IDENTITY_PIN=1` there to make the beta safe
  would fail-close CZ's execution — an unacceptable Customer-Zero regression.

CZ's OWN `ExecutionJob`s are node-bound (the field is "Snapshotted from account.terminal_node at job
creation"), so the fix cannot key on the mere presence of a node binding.

## Decision — the smallest generic node-aware transport seam

**Order destination FOLLOWS the job's AUTHORITATIVE execution node**, keyed on the SAME canonical hosted
classifier the identity-pin injection and claim-entitlement gate already use
(`execution.hosted_pin.is_hosted_workspace_account`), never on the node binding alone.

1. **Model.** `TerminalNode.order_bridge_base_url` (optional, blank default; migration `0030`). The base URL
   of THIS node's dedicated pin-enforcing order bridge. Deliberately separate from `hostname` (execution
   identity) and `rdp_host` (delivery-only transport).
2. **Resolver.** `execution.order_transport.resolve_order_transport(job, *, global_base_url)`:
   - **LEGACY / Provider-A / Customer Zero / dark subsystem** → `(ok, OT_LEGACY_GLOBAL, <global>, hosted=False)`
     — the existing global bridge, **byte-identical**, whatever the job's node binding.
   - **HOSTED (Provider-B, subsystem on)** → the job's snapshotted node must exist AND agree with the
     account's node (the `resolve_hosted_route` agreement invariant) AND carry an explicit
     `order_bridge_base_url` → `(ok, OT_NODE_OK, <node url>, hosted=True)`. Otherwise **fail closed**
     (`OT_NODE_UNBOUND` / `OT_NODE_MISMATCH` / `OT_ENDPOINT_UNCONFIGURED` / `OT_RESOLVE_ERROR`) — and a hosted
     job is **NEVER** routed to the global bridge.
3. **Worker.** `agent_order` / `agent_order_check` / `agent_modify` / `agent_close` now take an explicit
   `order_base` (no module-global default). A `resolve_order_base(job_id)` helper gates every one of the four
   dispatch sites (PLACE_ORDER/PLACE_TEST_ORDER, MODIFY_POSITION, CLOSE_TRADE, and the SHADOW dry-run); a
   refusal completes the job **FAILED** (no order) and the loop continues. The identity pin
   (`apply_identity_pin`) is still forwarded on every hosted site — transport selection only picks a URL and
   never touches the payload.
4. **One worker, node-aware.** No competing workers claiming the same global queue: the claim endpoint
   already scopes a node-aware worker to its nodes' jobs and a legacy worker to NULL-node jobs (disjoint).
   The single dispatch worker selects the correct bridge from the job's authoritative node.

## Invariants (permanent)

- **No hosted→global fallback.** A hosted order reaches only its node's bridge or fails closed. Never CZ's.
- **Customer Zero byte-identical.** A non-hosted job resolves to the global bridge unchanged, regardless of
  its node. **DARK:** while `HOSTED_PERSISTENT_MT5_ENABLED` is off, `resolve_order_base` short-circuits to the
  global bridge with **no** extra query — dispatch is byte-for-byte the pre-seam path.
- **Fail-closed.** No node, node mismatch, blank endpoint, classifier error, or missing job row ⇒ refuse.
- **Pin preserved.** The mandatory per-job identity pin remains the order authority at the bridge; this seam
  only chooses which pin-enforcing bridge receives it.

## Consequences

- **Reversibility:** instant — a node with a blank `order_bridge_base_url` (default) plus the dark flag = the
  pre-seam behaviour. No data migration beyond the additive nullable column.
- **Scope:** exactly "a hosted job goes to its node's own pin-enforcing bridge; everyone else keeps the global
  bridge." It does not change who is hosted, arming, or the bridge's order authority.
- **Generic, not beta-specific.** No hard-coded beta account ids or emails; every future beta node uses the
  same field + resolver. The mechanism is permanent even though the co-residency posture that motivated it
  (ADR-0044) expires at STREAM 10E.

## Verification

- `execution/tests_order_transport.py` — the Sponsor's 10-point bar (legacy→global; hosted→node bridge;
  hosted never→global; two nodes→two bridges; wrong-node fail-closed; missing endpoint fail-closed; pin
  survives; CZ unchanged when no endpoint; AUTO_SHADOW unchanged) + a `TerminalNode` field roundtrip + two
  **AST wiring guards** that fail if a resolve call is removed or a dispatch call reverts to the single-arg
  global form. Full execution + hosted_workspace regression (1713 tests) green.

## Why this is an approved decision (not self-accepted)

It changes how orders route on the execution path — an "execution path" change that
`.claude/rules/architecture.md` ("no silent architecture replacement") requires be recorded as an approved
decision. The Sponsor's 2026-08-14 "Option A APPROVED" is that approval; the build ships DARK and its
activation on a real beta node remains a separate operational step.

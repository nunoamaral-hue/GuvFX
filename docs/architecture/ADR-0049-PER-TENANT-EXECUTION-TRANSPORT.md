# ADR-0049 — Per-Tenant Hosted Execution Transport

- **Status:** Proposed / DARK (implemented behind `HOSTED_PER_TENANT_TRANSPORT_ENABLED`, default OFF).
- **Date:** 2026-08-19
- **Supersedes-in-part:** ADR-0046 (per-node order transport) for the *hosted multi-tenant* case.
- **Related:** ADR-0044 (supervised single-tenant beta), ADR-0047 (explicit customer execution
  authorization), ADR-0048 (node commissioning / execution-path gate).

## Context

The P0-B fresh-customer acceptance blocked at provisioning: a second non-CZ beta customer could not be
allocated because the single beta node (`guvfx-beta-node-1`) had `max_accounts=1` and its slot was held by
support@ (account 25). The subsequent forensic (verdict **B — architecture change required**) established:

- The **relational/capacity** layer already supports N accounts per node (`TerminalNode.max_accounts`,
  many-to-one account/workspace bindings, `_node_has_capacity` counts distinct accounts).
- The **order-execution transport** does not: `TerminalNode.order_bridge_base_url` is a single per-node
  field, `ORDER_BRIDGE_PORT = 8789` is hard-coded, the node order-worker is node-scoped (claims every
  account's job on the node), and the `:8789` bridge process (`scripts/mt5_signal_bridge.py`) reads
  `MT5_TERMINAL_PATH` once at module load — hard-pinned to ONE account's terminal for its whole lifetime,
  with the MetaTrader5 client itself process-global.

Consequently raising `max_accounts` is unsafe: a second account's orders would route to the account-25-pinned
`:8789` bridge and fail closed on the identity pin (no cross-tenant leak, but no execution). Isolation today
rests entirely on the terminal-side login/server pin — a fail-closed *guard*, not a *router*.

## Decision

Introduce a **per-tenant execution transport**: one dedicated pin-enforcing bridge process per hosted
customer, addressed by a unique `host:port`, under the *same* execution node — so one physical host serves
many isolated tenants (NOT one host per customer, and NOT a bare "Node 3", which would re-collide on `:8789`).

1. **Authority model** — new `execution.HostedExecutionEndpoint` (OneToOne per `HostedMt5Workspace`): the
   authoritative, server-derived routing target (host, unique port, base_url) plus the identity snapshot the
   bridge is configured from (windows_username, runtime_path, expected_login/server, is_demo, workspace_uuid)
   and a lifecycle state (`ALLOCATED → READY → RETIRED`). No customer-controlled routing field exists.
   Only `READY` is routable. DB constraints enforce one live endpoint per `(host, port)` and per account.

2. **Deterministic port allocation** — `endpoint_service.allocate_port` hands out the lowest free port in a
   bounded range (`8800–8899`), excluding the reserved GuvFX ports (`8787` backtest agent, `8788` CZ order
   bridge, `8789` beta node bridge, `8791` validation agent) and ports held by live endpoints on that host.
   Durable (restart-safe), collision-safe (row-locked scan + DB unique constraint), reclaimable on retire.

3. **Account-aware routing** — `order_transport.resolve_order_transport` gains a per-tenant branch: with the
   flag ON, a hosted job resolves to *its own account's* `READY` endpoint (keyed on the job's account,
   ownership re-asserted, node-agreement preserved) and FAILS CLOSED otherwise — never another tenant, never
   the node URL, never the global bridge. With the flag OFF the resolution is **byte-identical** to the
   per-node behaviour (support@ untouched).

4. **Per-tenant bridge config** — `bridge_config.render_bridge_env` renders each tenant's bridge env
   (ASCII-only, RULE 9) from its endpoint: its own `MT5_ACCOUNT_ID`, terminal path, port, and the full
   server-derived identity, with the certified DEMO-only posture (`MT5_REQUIRE_IDENTITY_PIN=1`,
   `MT5_GUARDED_ATTACH=1`, `MT5_ALLOW_LIVE` never set).

## Darkness & preservation

- `HOSTED_PER_TENANT_TRANSPORT_ENABLED` defaults OFF; nothing reads the endpoint until it is on.
- support@ is preserved by seeding its endpoint at its EXISTING `:8789` bridge (`explicit_port=8789`), so
  flipping the flag does not re-home it. Its sizing/authorization/execution are untouched.
- ADR-0047 authorization, the order-time pin, and the node-commissioning gate remain the sole order
  authority; this ADR adds a *routing* isolation layer, it grants no execution authority.

## Wire-up contract (for the future provisioning integrator)

- **New hosted tenants** must be `is_active=False` intent accounts at endpoint-allocation time (they are —
  a hosted intent account carries no live broker identity until confirmed). `allocate_endpoint` **refuses to
  auto-mint a port for a live (`is_active`) account** (`EP_LIVE_ACCOUNT_REQUIRES_EXPLICIT`) so a live account
  (support@/CZ) can never be silently re-homed onto a fresh portless bridge; seed those with `explicit_port`
  (matching the existing per-node bridge) or `allow_rehome=True`.
- **support@ / Customer Zero** are seeded EXPLICITLY at their existing bridge (`explicit_port=8788/8789`);
  the seed reproduces the node's current `order_bridge_base_url` exactly (a test asserts this), so turning the
  flag on does not move them.
- An endpoint only becomes routable via `mark_ready(health_ok=True)`, which the per-tenant **bridge
  supervisor** calls after it has stood the bridge up, hit `/health`, and verified the identity pin — this
  service performs no probe itself.

## Adversarial review

Round 1 (4 skeptics vs 20 vectors): 0 HIGH, 4 MEDIUM (node-unbound fail-open; empty `expected_server` from a
wrong field; fresh-host port-allocation race; live-account re-home), fixed in the follow-up commit. Round 2
(re-verify): CLEAN — all four closed, core isolation invariants intact, only fail-closed LOW nits remaining.

## Consequences / still required before enablement (separately gated)

- **Host multi-tenant isolation certification** (STREAM-10E escape battery, W^X G5v2, per-tenant NTFS/AppLocker
  actually applied, concurrent RemoteApp/Guacamole/RDS) — `HOSTED_REMOTEAPP_ISOLATION_CERTIFIED` is still
  withheld and is the precondition for a second live tenant.
- **Per-tenant bridge supervision** (a scheduled task per tenant per RULE 1; reboot reconstruction; crash
  isolation) — host-side lifecycle, deployed and certified separately.
- **Production capacity change** (`max_accounts` raise) is a distinct final gate, taken only after the
  two-tenant certification passes.

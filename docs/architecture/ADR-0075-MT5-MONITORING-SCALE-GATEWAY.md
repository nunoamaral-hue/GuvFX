# ADR-0075 — Scalable per-host MT5 monitoring gateway + deterministic route allocation + identity-attested readiness

- **Status:** Accepted (repository foundation, DARK / default-OFF). Live host certification deferred to a separate DARK deployment packet.
- **Packet:** GFX-MT5-BRIDGE-SCALE-001.
- **Date:** 2026-11-05.
- **Supersedes / relates to:** ADR-0049 (per-tenant execution transport, 8800–8899 allocator — reused), ADR-0069 (physical-host capacity guard — composed, never overridden), ADR-0072 (LIVE read-only positions monitoring gate — composed, not replaced). Does not modify any existing ADR.
- **ADR number rationale:** 0073 is the highest present; **0074 is claimed by the active parallel Copy-Trading PR #612** (`docs/architecture/ADR-0074-COPIER-HYBRID-COMPLETION.md`). 0075 is the next genuinely free number in both ADR directories, chosen to avoid colliding with that in-flight PR.

## Context — the proven defect

A read-only production diagnosis on 2026-11-05 (prod backend `2009a8a7`) established, for the 11 live support@ accounts on the single Windows host `100.79.101.19`:

- Each hosted account gets **one per-account bridge port** via `execution.endpoint_service` (lowest-free in **8800–8899**, 100 ports) and a `HostedExecutionEndpoint`. The backend reaches it over the Tailscale tailnet.
- **Reachability is a hand-maintained sub-window of allocation.** The Tailscale ACL grant currently permits only `~8804–8814`; ports `8800–8803` TIME OUT and `8815–8899` would too. As allocation climbed (accounts #53/#54/#56/#57/#58 → 8810–8814), each new port fell outside the grant and required a **manual ACL edit** (the recurring "onboarding bridge-port ACL" incident; repaired reactively on 2026-11-04).
- **Per-account bridge processes drift.** Identical live account types return divergent results: some bridges serve positions (`trade_mode=2`, readable); others return `account_not_demo` (`trade_mode=None`) or `mt5_init_failed`, because each `GuvFX_TenantBridge_NN` is a separate process with its own build/config.
- The dashboard's "Monitoring only" is gated on a **balance** read, not a positions read, and the ~10 distinct per-account failure causes all collapse to one bare `stale`/`None` with **no reason code reaching the UI** — so "unavailable" is indistinguishable from "zero".

Current five "live positions unavailable" accounts (verified, superseding the 2026-11-04 ACL-era set): **#50** (`mt5_init_failed`), **#53/#54/#56/#57** (`account_not_demo`). #58, previously ACL-blocked, now reads fine — the unavailable set *churns*, which is itself the symptom.

## Decision

Adopt **Design A — a per-host authenticated monitoring gateway** (preferred), implemented repository-side, DARK. Retain the existing deterministic allocator (`endpoint_service`) and reuse it for the gateway's *internal, host-loopback* bridge registry. Keep **Design B — a certified per-host port pool** as the documented fallback.

### Why the gateway (A) over the port pool (B)

| | A — Per-host authenticated gateway (CHOSEN) | B — Certified per-host port pool (FALLBACK) |
|---|---|---|
| Tailnet ports per host | **One** (forever) | A certified RANGE (opened once) |
| Future per-account ACL edits | **Never** | None until range exhaustion |
| Config/build drift across accounts | Eliminated (one process, one config, one attestation point) | Persists (N bridge processes) |
| Cross-tenant routing / arbitrary forwarding | Prevented at an authenticated boundary | Relies on per-port isolation |
| Multi-host expansion | One grant per new host | One range per new host |
| New host-side component | **Yes** (needs a separate DARK deploy + cert) | No (backend-only) |
| CI-provable today | Core logic yes (in-process); live behaviour no | Fully |

B still eliminates per-account ACL churn *if* the ACL certifies the whole range once, but keeps N Tailnet-exposed ports and the per-bridge drift. A structurally removes both. The gateway's one defining cost — a new host-side process this packet cannot deploy — is handled by shipping the gateway as a repository artifact that CI exercises in-process (no sockets, no host), with live certification deferred.

## Requirements

1. Support more accounts and future hosts **without a manual Tailscale edit per account**.
2. Deterministic, collision-free, restart-safe, reclaimable route ownership per physical host.
3. "Broker connected" is **not** sufficient for monitoring readiness — an identity-attested, reachable, capability-advertised, fresh route is.
4. Never collapse "unavailable" into "zero"; surface the precise cause.
5. Additive + DARK; no order/execution/host/Tailscale/account mutation; no agent-token exposure.

## Component / data flow

```
backend ──(ONE Tailnet port, authenticated)──▶ PhysicalHostGateway
                                                  │  (auth, allow-list, route-by-registry)
                                                  ▼
                                 local-loopback per-account bridges (8800–8899, NOT Tailnet-exposed)
                                                  ▼
                                               MT5 terminals

MonitoringRoute(account) ── gateway ──▶ PhysicalHostGateway
            └─ execution_endpoint (reuse) ─▶ HostedExecutionEndpoint (physical port authority, ADR-0049)
```

Backend client (`execution.monitoring_gateway_client`) → gateway (routing flag on) or legacy per-account endpoint (transition). Readiness (`execution.monitoring_readiness`) composes the probe result with ADR-0072 workspace monitoring eligibility. Reconcile (`execution.monitoring_reconcile`) refreshes readiness and raises governed `reliability.AlertEvent`s.

## API / protocol contract (`host_gateway`, protocol `mg-1`)

Read-only op vocabulary only: `health` (gateway-level), `attest_identity`, `account_snapshot`, `positions`. **No write/order/modify/close op exists.** A request carries `{op, account_id, workspace_uuid, expected_generation?, nonce, ts}` + an `X-GuvFX-Agent-Token` header. The gateway:

- authenticates with a **constant-time, total, fail-closed** token compare (unset credential → deny all + refuse startup);
- rejects any op outside the allow-list (and therefore any path-shaped op — ops are an enum, not a path);
- resolves the target bridge **only** from its local registry by account id (no host/port/path from the request → no arbitrary forwarding, no path traversal);
- refuses a request whose `workspace_uuid`/`expected_generation` disagree with the registered occupancy (cross-tenant + stale-occupancy guard) and any non-`SERVING` registration;
- enforces a per-request nonce (replay) + timestamp skew;
- gates data ops on an advertised capability;
- returns sanitised short error codes only (no secret, no login in clear; attestation masks the login).

## Registry / data model (additive; `execution` app, migration 0037)

- **`PhysicalHostGateway`** — physical host identity (`host_key == rdp_host`) + the one gateway endpoint + attested health + an optional host-wide `max_routes` ceiling (narrows, never overrides, ADR-0069 `HostCapacity`).
- **`MonitoringRoute`** — one immutable occupancy per account: `(host_key, local_port)` slot, `allocation_generation` (monotonic, +1 on release after TOMBSTONE), lifecycle `state` (RESERVED→REGISTERED→VERIFIED→READY, DRAINING, TOMBSTONED, QUARANTINED), structured `readiness` + `last_reason_code`, verified identity snapshot, `capability_set`, health. Soft `OneToOne` to the existing `HostedExecutionEndpoint` (reuse, never replace). Constraints: one active route per account; no active `(host_key, local_port)` collision; unique `(account, generation)`.
- **`MonitoringRouteAudit`** — append-only state/generation provenance (monotonicity evidence + forensics).

## Allocation lifecycle & failure handling

Row-locked `transaction.atomic`, deterministic lowest-free local port, DB-unique backstop, retry on contention. Fail-closed on pool exhaustion (`route_pool_exhausted`) and the certified ceiling (`route_host_capacity_exceeded`). Occupancy-identity integrity is asserted before every mutation; any disagreement **QUARANTINES the route in its own committed transaction** (so the quarantine persists even though the operation raises) — never silently repaired (`.claude/rules/architecture`). Identity mismatch on verify → quarantine (`bridge_identity_mismatch`).

## Readiness contract (8 gates) & "no zero-collapse"

MONITORING_READY requires all of: workspace active & not tombstoned; terminal identity match; boundary healthy; reachable; authenticated identity match; capability advertised; freshness; no duplicate route. The result is a structured `(readiness, reason_code, ui_state, gates)`. A flat 0-position reachable account is **READY** (`no_open_position`); `account_not_demo` is **PARTIAL** (`feature_disabled` — equity monitorable, positions gated); `mt5_init_failed`/unreachable/mismatch are **UNAVAILABLE** with their precise codes. UNAVAILABLE is never reported as zero.

## Scaling behaviour & capacity

Allocation is O(1)-ish per reserve (constant bounded query count; measured p50 ≈ 3 ms, p95/p99 < 10 ms at 11 and 20 routes, no N+1). Concurrency is race-free (12-thread test: distinct ports, no collision). **Allocation capacity (pool = 100; certified ceiling via `HostCapacity`) is explicitly distinct from measured host/terminal capacity** — the latter is a separate certified number (ADR-0069) and is NOT claimed from a config integer or from this foundation.

## Transition from existing ports & multi-host expansion

`monitoring_onboarding.sync_routes_from_endpoints` builds a deterministic `MonitoringRoute` for every current READY per-account endpoint (DARK), so the estate is represented before the gateway is deployed. In the transition (routing flag off) the client reads the legacy per-account endpoint byte-identically. Each new physical host gets one `PhysicalHostGateway` and one ACL grant; its local bridges never touch the Tailnet.

## Governance / boundaries (DARK)

- Flags default OFF (`MONITORING_GATEWAY_ENABLED`, `..._ROUTING_ENABLED`, `MONITORING_ROUTE_RECONCILE_ENABLED`, `MONITORING_READINESS_API_ENABLED`) — in the `execution` app, **not `settings.py`**.
- Migration 0037 is additive (only `CreateModel`) + reversible; depends on `execution 0036` (no reliability/trade_copier dependency).
- Reconcile reuses `reliability.AlertEvent` with a raw namespaced component — **no `reliability/constants.py` edit, no reliability migration** (clear of PR #612).
- Nothing contacts a host, mutates Tailscale, places/sizes/arms an order, or reads/prints the agent token value. No production schedule is added.

## Consequences / deferred (Sponsor-gated)

Live host certification of the gateway process; the actual ACL delta (ideally: keep only the one gateway port, retire the per-account range); wiring the readiness contract into the dashboard UI and the onboarding writer; enabling the ADR-0072 positions gate and repairing #50's terminal. All separate, DARK-deploy / Sponsor decisions — see the DARK deployment packet.

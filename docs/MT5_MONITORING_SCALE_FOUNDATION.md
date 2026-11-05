# MT5 Monitoring Scale Foundation (GFX-MT5-BRIDGE-SCALE-001, ADR-0075, DARK)

Repository-only, default-OFF foundation that replaces per-account/manual-port bridge expansion with a
scalable, governed **per-host authenticated monitoring gateway** + deterministic route allocation +
identity-attested readiness. Nothing here is deployed, scheduled, or armed by this packet.

> **Handoff-doc note (deviation, Amber, intentional).** This packet runs in parallel with Copy-Trading
> PR #612, which edits `docs/STATUS.md`, `docs/HANDOFF.md`, `docs/NEXT.md`, `docs/KNOWN_ISSUES.md`. To
> keep this PR CLEAN/MERGEABLE per the packet's Phase-11 gate and honour the no-overlap boundary, those
> four shared docs are **not** edited here; handoff continuity lives in ADR-0075, this doc, the evidence
> manifest `evidence/manifests/GFX-EVD-MT5-BRIDGE-SCALE-001.json`, and the Notion records.

## Phase 1 — five-account production diagnosis (read-only, 2026-11-05, prod `2009a8a7`)

All 11 live support@ accounts sit on one Windows host (`100.79.101.19`), each with its own per-account
bridge port + READY `HostedExecutionEndpoint`. **All 11 are now TCP-reachable** (the 2026-11-04 ACL block
on 8810–8814 is resolved). Current five "live positions unavailable" accounts and their verified causes:

| Acct | Port | Verified current cause (masked) |
|------|------|----------------------------------|
| #50 | 8807 | `mt5_init_failed` — MT5 terminal not initialised (lifecycle fault) |
| #53 | 8810 | `account_not_demo` (`trade_mode=None`) — positions gate refuses at that bridge |
| #54 | 8811 | `account_not_demo` |
| #56 | 8813 | `account_not_demo` |
| #57 | 8814 | `account_not_demo` |

Readable + flat (0 open): #46, #48, #49, #51, #52, #58 (`trade_mode=2`). #58 — previously ACL-blocked —
now reads fine; the unavailable set *churns* because each account has its own Tailnet-exposed bridge
process + port + ACL grant. Two structural defects: (1) **reachability is a hand-maintained sub-window of
allocation** (allocator 8800–8899 vs ACL grant ~8804–8814; 8800–8803 and 8815+ time out); (2) **per-bridge
build/config drift** (identical account types diverge). No production repair was performed.

Packet Phase-1 taxonomy (current): terminal init failure → #50; feature disabled (positions gate) →
#53/#54/#56/#57; genuinely flat → #46/#48/#49/#51/#52/#58; no endpoint / listener absent / identity
mismatch / unsupported / stale / unknown → none among the 11.

## What this foundation adds (all additive, DARK)

- **Data model** (`execution` app, migration `0037_monitoring_gateway`): `PhysicalHostGateway`,
  `MonitoringRoute` (occupancy identity + generation), `MonitoringRouteAudit`.
- **Allocator + lifecycle** (`execution/monitoring_allocation.py`): deterministic, row-locked,
  fail-closed; generation monotonicity; integrity quarantine that persists.
- **Host gateway reference component** (`backend/host_gateway/`, protocol `mg-1`): auth boundary,
  read-only op allow-list, route-by-registered-identity, cross-tenant/replay/stale rejection, attestation.
  NOT a Django app, NOT run on Windows by this packet.
- **Transport client** (`execution/monitoring_gateway_client.py`): gateway or legacy-endpoint transition;
  token from env, never logged.
- **Readiness contract** (`execution/monitoring_readiness.py`): 8 gates, structured
  `(readiness, reason_code, ui_state, gates)`, composes with ADR-0072; never collapses unavailable→zero.
- **Reconcile + governed alerting** (`execution/monitoring_reconcile.py` + command
  `reconcile_monitoring_routes`): default-OFF, read-only, uses existing `reliability.AlertEvent` with a raw
  namespaced component — no `reliability/constants.py` edit, no reliability migration, no production schedule.
- **Onboarding hooks** (`execution/monitoring_onboarding.py`): composable, DARK; NOT wired into the live
  provisioning writer.
- **Flags** (`execution/monitoring_flags.py`): all default OFF; in the `execution` app, not `settings.py`.

## Tests (run locally on PostgreSQL; covered by the backend CI lane)

65 new tests across: gateway component security (20, pure), allocation/integrity (14), readiness (9),
client (8), reconcile/alerting (8), capacity/concurrency (7). Existing `tests_per_tenant_transport` (32)
re-run green (the `execution/models.py` import is additive). Capacity: reserve p50 ≈ 3 ms, p95/p99 < 10 ms
at 11 and 20 routes; 12-thread concurrency yields distinct ports, no collision.

## Boundaries honoured

No broker order; no Copy-Trading change; no edit to any PR #612 file (incl. `settings.py`,
`reliability/constants.py`, `reliability/services/operations_summary.py`, `reliability/migrations/0005`,
all `trade_copier/**`, ADR-0071/0074, the four shared handoff docs); no Tailscale mutation; no Windows-host
deployment; no production deploy/restart; no NAS/TIM/n8n change; no credential access; agent token never
read/exposed; no account deletion; no new live account; execution OFF; all repository changes default OFF.

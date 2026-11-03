# ADR-0069 — Physical-Host-Wide Capacity Guard

- **Status:** Accepted — shipped DARK (flag `HOSTED_HOST_CAPACITY_GUARD_ENABLED`, default OFF).
- **Date:** 2026-11-03
- **Area:** hosted-workspace node allocation / execution capacity.
- **Relates to:** ADR-0034 (allocation), ADR-0043 Addendum B (CZ co-residency), ADR-0046 (per-node order transport),
  ADR-0048 (execution-path gate). `reference_windows_host_capacity`.

## Context

The node allocator (`hosted_workspace.provisioning.allocate_workspace_node`) enforces capacity **per logical
`TerminalNode`** only — `node_occupant_count(node) < node.max_accounts`. It never computes a total across logical
nodes that share one **physical** host. In production two logical nodes — `guvfx-windows-mt5` (Customer-Zero,
`max_accounts=10`) and `guvfx-beta-node-1` (beta, `max_accounts=12`) — are **co-resident on the one physical box
`100.79.101.19`** (both carry that `rdp_host`). Their per-node ceilings sum to 22 against a box whose certified
capacity is far lower. Nothing in code stops:

1. raising a node's `max_accounts`, or
2. registering a **third** logical node on the same `rdp_host`,

from over-committing the single physical host. The 12-ceiling has been a **procedural** discipline, not a code
guard (the two `_HARD_MAX_WORKERS = 12` / `_HARD_MAX_PER_CYCLE = 12` literals cap cron fan-out, not allocation).

This surfaced while recertifying the box: it was upgraded (8→16 logical CPUs, 32→64 GB) since the `SAFE_FOR_12`
record, and we want to raise `guvfx-beta-node-1` to 16 (total 17 incl. CZ). The governing packet mandates a
physical-host-wide guard **before** any such raise.

## Decision

Add a **certified, documented, audited host-wide ceiling** and enforce it in the one allocation choke point.

1. **`execution.HostCapacity`** — one row per physical host, keyed on `physical_host_key` (== the co-resident
   nodes' `rdp_host`, the only physical-host signal in the schema). Holds the enforced `max_total_occupants`, an
   informational `certified_safe`, a secret-free `basis`, and `certified_at`/`certified_by`. Read **live**.
2. **`execution.HostCapacityAudit`** — append-only history of every ceiling change (actor, reason, prev→new), so
   the ceiling is never an undocumented constant (governance: evidence.md).
3. **Allocator guard** — inside `allocate_workspace_node`, the ACTIVE node set is materialised up-front under the
   existing `select_for_update` (locking **all** co-resident rows for the txn), then for a candidate node the
   guard sums `node_occupant_count` over the co-resident ACTIVE nodes (same `rdp_host`) and refuses the bind when
   that total has reached the host's certified ceiling. Factored into the pure, unit-tested `host_capacity_block`.
4. **Flag `HOSTED_HOST_CAPACITY_GUARD_ENABLED`** (default OFF). OFF ⇒ the host sum is never computed and
   allocation is **byte-identical** (the only internal difference is lock breadth — all ACTIVE nodes locked vs
   up-to-candidate — which changes no allocation outcome). Deploy DARK → seed the ceiling → arm; rollback = flip OFF.
5. **Fail-closed.** With the guard ON, a host that has nodes but a blank `rdp_host` or **no** `HostCapacity` row is
   refused (`host_capacity_unconfigured`) — an uncertified host is never filled. A full host is refused
   (`host_capacity_exceeded`). Both are distinct, operator-actionable reason codes.
6. **Applies to every tenant.** The limit is physical; Customer Zero's own occupancy counts toward the host total.
   (CZ isolation is unchanged and still forbids a non-CZ tenant from binding to the CZ node.)
7. **Supported primitive `set_host_capacity`** (dry-run by default, `--apply` to persist) writes the ceiling +
   an audit row — the only sanctioned way to set it; never a raw DB edit. `hosted_capacity_report` gains a
   host-wide rollup (occupancy vs ceiling per physical host).

**Race-safety:** two concurrent allocators serialise on the `select_for_update` locks over the ACTIVE node rows,
so the second observes the first's committed occupancy; with one free slot exactly one bind succeeds. Covered by a
two-thread test (`HostCeilingConcurrencyTests`).

## Consequences

- Closes the same-host over-subscription hole; a raised `max_accounts` or a new same-host node can no longer exceed
  the certified physical ceiling.
- The host ceiling becomes a single documented, audited, live-readable source.
- **Out of scope (follow-up):** the two scattered `= 12` literals are not yet consolidated to read from
  `HostCapacity`; and no **immutable** host-key exists — the guard keys on the operator-mutable `rdp_host` and
  fails closed on a blank. A future ADR may add an immutable physical-host key.
- This ADR provides the **enforcement mechanism**, not the number: the certified ceiling (e.g. 20) is set by a
  separate governed capacity certification and recorded in `HostCapacity.basis` + `HostCapacityAudit`.

## Alternatives considered

- **Per-node only (status quo):** rejected — it is the hole.
- **Immutable physical-host key field:** deferred — larger schema change; `rdp_host` grouping + fail-closed-on-blank
  is sufficient today.
- **`terminal_provisioning.beta_capacity` host-probe seam (control 15):** rejected as the enforcement point — it is
  an estate-global beta-pool cap not in the hosted allocator path and not host-keyed.

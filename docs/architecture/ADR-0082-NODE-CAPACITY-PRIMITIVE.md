# ADR-0082 — Governed Terminal-Node Capacity Primitive (`set_node_capacity`)

- **Status:** Accepted — repository-only; no behaviour change until an operator runs the command. No flag (a
  management command is inert until invoked).
- **Date:** 2026-11-06
- **Area:** hosted-workspace node allocation / execution capacity (operational primitive).
- **Relates to:** ADR-0069 (physical-host capacity guard; `set_host_capacity`, `HostCapacity`), ADR-0043
  Addendum B (Customer-Zero co-residency isolation), ADR-0034 (allocation). `reference_windows_host_capacity`,
  `project_node2_capacity_recovery_stream_b`.

## Context

`TerminalNode.max_accounts` is the **per-node logical capacity ceiling** the allocator enforces
(`node_occupant_count(node) < node.max_accounts`). It is operator-set (migration default 50) but there was **no
supported, audited primitive to change it**: the field is written only by test factories, so the only production
mechanisms were the Django admin, an ORM shell edit, or raw SQL — all **unaudited manual model edits**.

This gap blocked a bounded production recovery (`GFX-TAUREX-CAPACITY-REPAIR-002`): a stuck onboarding needed node
2 raised 16→17 within the certified-safe host occupancy, but no audited command existed, so the repair correctly
STOPPED. ADR-0069 already ships the **host-wide** analogue (`set_host_capacity` writing `HostCapacity` +
`HostCapacityAudit`); this ADR adds the missing **per-node** analogue with the same discipline, plus a
fail-closed, **point-in-time** guard that refuses a **RAISE** when the proposed ceiling plus current
co-resident occupancy would exceed the host's certified-safe occupancy. A no-op or a lowering is always
permitted (it cannot worsen occupancy), which is what makes the rollback/tighten path work. The guard is
point-in-time against current occupancy; it does **not** bound the sum of co-resident ceilings, so durable
occupancy remains governed by the ADR-0069 runtime guard (`max_total_occupants`) and operator discipline.

Four capacity axes are distinct and **must never be conflated** (the incident's "12/20" confusion came from
conflating the first two):

| Axis | Meaning | Source of truth | Enforced by |
|---|---|---|---|
| **Account entitlement** | per-USER owned-account cap (e.g. "12/20") | `UserEntitlement.max_trading_accounts` | `check_can_add_account` (account creation) |
| **Node capacity** | per-NODE logical ceiling | `TerminalNode.max_accounts` | allocator `node_occupant_count < max_accounts` |
| **Certified-safe occupancy** | recommended-safe physical occupancy | `HostCapacity.certified_safe` | **this primitive** (capacity changes) |
| **Absolute host ceiling** | hard physical maximum | `HostCapacity.max_total_occupants` | ADR-0069 allocator host guard (runtime) |

## Decision

Add `python manage.py set_node_capacity` (service: `execution/node_capacity.py`; audit model
`execution.NodeCapacityAudit`, migration `0038`). It is the **only supported way** to change
`TerminalNode.max_accounts` outside a reviewed migration.

### Command contract

```
python manage.py set_node_capacity (--node-hostname <exact> | --node-id <int>) --max <int>
                                    [--apply] [--reason <text>] [--actor <id>] [--correlation-id <id>] [--json]
```

- **Dry-run by default**; `--apply` persists. `--apply` requires a non-empty `--reason` (recorded in the audit row).
- Resolves the node unambiguously by immutable id or exact (unique) hostname.
- Changes **only** `max_accounts` (+ one audit row). Never touches `HostCapacity`, workspace assignments, tenant
  isolation, or execution controls; places no order; contacts no host.
- Emits a plan: target node, old→proposed max, node occupancy, physical-host occupancy, certified-safe, absolute
  ceiling, remaining certified slots before/after, and whether it would apply or refuse (with a stable reason code).

### Fail-closed rules (refuse, no write)

1. `MAX_BELOW_NODE_OCCUPANCY` — proposed max < current node occupancy (never strand a live occupant).
2. `MAX_OUT_OF_RANGE` — max < 1 or max > the host's absolute ceiling (`max_total_occupants`).
3. `NODE_NOT_FOUND` / `NODE_AMBIGUOUS` / `NODE_INACTIVE` — unknown, duplicate, or non-ACTIVE node;
   `HOST_IDENTITY_AMBIGUOUS` — blank `rdp_host` (physical host unresolvable).
4. `HOST_ALREADY_OVER_CERTIFIED_SAFE` — **on a RAISE**, current physical-host occupancy already exceeds
   certified-safe. (A no-op or lowering is still permitted here — it cannot worsen occupancy — which is exactly
   what enables the rollback/tighten path when a host legitimately sits above certified-safe.)
5. `WOULD_EXCEED_CERTIFIED_SAFE` — **on a RAISE**, `proposed_max + other_co-resident_occupancy > certified_safe`.
6. `HOST_CAPACITY_CERTIFICATION_MISSING` / `CERTIFIED_SAFE_MISSING` — no `HostCapacity` row, or `certified_safe`
   unset (the existing ADR-0069 contract defines no staleness window; a present, live-read certified row with a
   `certified_safe` value is required, else fail closed).
7. Concurrency — the apply transaction `select_for_update`-locks every co-resident node row (by pk) and the
   `HostCapacity` row, and **re-evaluates the full guard under the lock**, so a concurrent capacity change or an
   allocation that moved occupancy is observed and the guard fails closed rather than oversubscribing.
8. `ISOLATION_INVARIANT_VIOLATED` — the Customer-Zero forbidden-node set is captured before and re-checked after
   the write; any difference rolls the whole transaction back (structurally unreachable, since `max_accounts`
   does not affect isolation — a loud belt-and-suspenders).

### Host-wide accounting (anti-undercount)

Host occupancy is computed with `hosted_workspace.provisioning.host_occupant_count`, which sums
`node_occupant_count` over **all** co-resident logical nodes grouped by `rdp_host` **regardless of node status**
(ACTIVE / DRAINING / OFFLINE / DISABLED) — a drained co-resident node still physically loads the box. The
primitive never counts ACTIVE-only, so it cannot reproduce the earlier under-count defect.

`other_co-resident_occupancy = host_occupant_count(host) − node_occupant_count(target)`. Raising a ceiling adds
no occupant, so **remaining certified slots is unchanged by the change itself** (`certified_safe − host_occupancy`);
the raise merely permits the node to accept up to that remaining headroom at future allocation, which the ADR-0069
runtime guard independently caps at `max_total_occupants`.

### Audit semantics

Each `--apply` writes exactly one append-only `NodeCapacityAudit` row **inside the same transaction** as the
field write: target node + physical host, previous/new `max_accounts`, node and host occupancy at the decision
moment, certified-safe and absolute ceiling, actor, mandatory reason, correlation id, UTC timestamp. A dry-run
writes **no** row. If the audit cannot be written, the whole transaction (including the `max_accounts` change)
rolls back — a certified change is never recorded without its evidence, and evidence is never recorded without
the change.

### Worked example (synthetic identifiers)

Host with `certified_safe=18`; node `example-beta-node` occupancy 16, one co-resident node occupancy 1
(`other=1`):

```
# preview (no write):
python manage.py set_node_capacity --node-hostname example-beta-node --max 17
#   -> WOULD APPLY  (17 + 1 = 18 <= certified_safe 18)
python manage.py set_node_capacity --node-hostname example-beta-node --max 18
#   -> REFUSED reason=WOULD_EXCEED_CERTIFIED_SAFE  (18 + 1 = 19 > 18)

# persist:
python manage.py set_node_capacity --node-hostname example-beta-node --max 17 \
    --apply --reason "GFX-TICKET-123: recover one stuck onboarding within certified-safe" --actor ops@example
```

### Rollback procedure

To undo a raise, re-run with the previous value: `set_node_capacity --node-hostname <h> --max <old> --apply
--reason "rollback of <ticket>"`. Lowering is permitted only down to current node occupancy (never below —
`MAX_BELOW_NODE_OCCUPANCY`); to reduce effective capacity further, drain/decommission occupants first via the
supported decommission path. A lowering changes only the ceiling; it does not remove or move any occupant.

## Consequences

- An audited, fail-closed path replaces unaudited admin/ORM/SQL edits of `max_accounts`; every change carries a
  mandatory reason, an actor and the occupancy context, satisfying the capacity-change governance (ADR-0069 ethos:
  "a capacity ceiling is never an undocumented constant").
- **Scope boundary:** this primitive enforces `certified_safe` for capacity *changes*. The *runtime* allocator
  guard (ADR-0069) continues to enforce `max_total_occupants`, unchanged — this ADR does not alter the allocator.
  `certified_safe` thus becomes a hard gate for operator-initiated raises while remaining advisory at allocation.
- Repository-only; no production deployment, host contact, or capacity mutation is performed by this change. The
  broader allocation/UI observability work (persisting the allocator's refusal reason, surfacing the real capacity
  blocker in the UI, differencing delivery-state errors) is explicitly deferred to a separate packet.

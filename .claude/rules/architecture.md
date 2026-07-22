# Rule — Architecture

Scope: read when changing structure, adding services, or altering how data, research,
risk, execution or AI responsibilities interact in the GuvFX platform.

- Prefer **small, additive changes**. Extend the existing Django apps and Next.js
  structure rather than replacing them. No whole-subsystem rewrites in a single packet.
- Keep a clear **separation of responsibilities**: data ingestion/storage, research /
  backtesting, risk, execution, and AI assistance stay in distinct, well-named modules.
  Do not let one concern silently leak into another.
- **No silent architecture replacement.** If a change alters an established pattern
  (auth, API surface, execution path, storage layout), it requires an approved decision
  (ADR / Notion record) before merge — not an in-passing edit.
- **No LLM live-trading authority.** Model-generated output may inform research and
  suggestions only. It must never directly place, size, or approve live or paper orders
  without an explicit human-gated control path.
- **No speculative infrastructure.** Do not add Kafka, Kubernetes, agent frameworks,
  feature stores, message buses, or similar heavy machinery without an approved decision
  *and* a measured, documented need. Default to the simplest thing that works.
- Preserve existing behaviour unless the packet explicitly asks to change it.

## Runtime identity invariant (adopted 2026-07-22, B3P-2)

Permanent architectural invariant for the beta per-slot execution model:

- **Slot number** identifies *physical capacity* — one pre-provisioned execution slot (its non-admin
  identity, fixed directory, fixed launch/terminate tasks and ACLs).
- **`(slot, generation)`** identifies **one immutable runtime occupancy** of that slot. Generation is a
  durable per-slot counter that increments by exactly one on release after TOMBSTONE; it may never remain
  unchanged after release, decrease, or skip a value. A violation is a permanent integrity failure
  requiring operator intervention and must never be silently repaired.
- **Runtime UUID** remains the *logical identity* of the runtime.

Generation is part of runtime identity, not an implementation detail: it appears in the slot ownership
marker, the Provisioning Verification Report and audit evidence. Before every mutating operation the slot
assignment database, the ownership marker, the runtime UUID, the slot, the generation and generation
monotonicity must all agree; any disagreement fails closed with a sanitised integrity error and
quarantines the slot for operator review.

### Windows primitive layer boundary (adopted 2026-07-22, before B3P-2 `win_ops`)

The Windows primitives are the highest-risk layer: they are the only code that touches the operator's
live host. Their responsibility is therefore deliberately narrow.

**A Windows primitive MUST NOT know:** runtime-UUID semantics, ProvisioningJob semantics, GuvFX business
rules, entitlement, or slot-allocation policy.

**A Windows primitive MAY only do:** act on a fixed slot identity; act on a fixed slot directory; trigger a
fixed scheduled task; observe a process; launch a process; terminate a process; move a directory to
tombstone; validate the filesystem.

Everything else — occupancy identity, generation, integrity assertions, audit, allocation, entitlement —
belongs **above** the primitive layer. A primitive that needs a UUID or a job id to do its work is a design
error: pass it the slot.

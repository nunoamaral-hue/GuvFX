# Backlog — Independent Signal Sources per Customer (architecture, future)

Status: **BACKLOG (not scheduled).** Recorded 2026-07-29 during Customer Zero enablement, per
Sponsor decision B4.

## Context
For Customer Zero certification (and the initial Trusted Beta configuration), a beta customer
**consumes the same Telegram signal source as the Golden Execution Reference** (`nuno.amaral@live.com`).
With `MULTI_ACCOUNT_ROUTING_ENABLED=1`, a signal on a source fans out to **every** routable
account bound to that source — so the Golden Reference's signals also drive the beta customer's
demo account. This is the **accepted** Trusted Beta config; it is deliberately simple and correct
for demo copy-trading, but it is not the desired end-state for a commercial multi-tenant product.

## What to investigate (future release)
- **Per-customer independent sources** — each customer binds to their **own** signal source
  (their own Telegram channel / provider), so one customer's activity never fans into another's,
  and the Golden Reference stays fully isolated from customer traffic.
- **Source ownership & entitlement** — model which customer may bind which source; prevent a
  customer from attaching to a source they do not own.
- **Provider abstraction** — reuse the existing `trading/brokers/` provider pattern for signal
  providers, so a source is a first-class, per-customer configured entity.
- **Migration path** — how existing fan-out beta accounts move to independent sources without
  disrupting the Golden Reference or losing history.
- **Routing implications** — `auto_router._resolve_targets` already fans out per source; with
  independent sources the fan-out set for each source becomes a single customer, which restores
  strict per-customer isolation while keeping the same routing code.

## Why not now
Deferred deliberately: independent sources require new provider onboarding + entitlement modelling
and are **out of scope** for Customer Zero (a single-customer certification). The shared-source
fan-out is sufficient and accepted for the Trusted Beta phase.

## Reference
Router: `execution/auto_router.py` (`_resolve_targets`, `_multi_account_routing_enabled`).
Source config: `execution.models.SignalSourceConfig`. ADR-0020 (multi-account routing).

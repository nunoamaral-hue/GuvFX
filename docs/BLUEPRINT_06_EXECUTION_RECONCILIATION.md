# Blueprint Doc 06 — Live ↔ Packet Execution-Path Reconciliation

| | |
|---|---|
| **Status** | **PROPOSED (v0.1)** — requires Nuno ratification (Proposed → Approved) before E3 |
| **Packet** | GFX-PKT-BLUEPRINT-06-RECONCILIATION |
| **Author** | Claude Code (draft) |
| **Date** | 2026-07-01 |
| **Decision type** | RED / architectural — Nuno owns lifecycle status (do not self-approve) |
| **Supersedes** | backlog item G ("Live Trading path not reconciled") in `docs/KNOWN_ISSUES.md` |

> This is a **draft for review**. It records the *current* facts (read-only) and a
> *proposed* reconciliation. Nothing here is implemented; no code, prod, migration,
> restart, or order was touched producing it. It must be ratified by Nuno before any
> E3 (real demo order placement) work proceeds.

---

## 1. Purpose

Two order-creating paths exist in the platform. This document reconciles them into a
single governed model so E3 (signal-driven **real demo** orders) can proceed without
architectural conflict, and identifies the residual risks Nuno must accept.

---

## 2. The three paths

### 2a. Legacy live path (GREEN "Trading" domain — LIVE TODAY)
Strategy engine / operator creates an **order-opening** `ExecutionJob`
(`OPEN_TRADE` via `execution.services.create_open_trade_job` / `CreateDemoTradeJobView`,
or `PLACE_ORDER` via the strategy poller, `execution/models.py:265,280`). The normal
ingest worker claims it and calls the bridge **`POST /mt5/order`** →
`execute_demo_order` → **`mt5.order_send`** (`scripts/mt5_signal_bridge.py`). Places a
**real** order on the demo account.

```
strategy/operator → ExecutionJob(OPEN_TRADE|PLACE_ORDER) → normal worker
    → agent_order → bridge /mt5/order → mt5.order_send   ← REAL order
```

### 2b. Signal shadow path (E0–E2b — DEPLOYED, NO ORDER)
Wayond signal → `PendingSignalApproval` → (approve) → `SignalExecutionPlan` →
`promote_plan_to_shadow_jobs` → one **`PLACE_ORDER_SHADOW`** job per leg
(`execution_mode=SHADOW`). A dedicated shadow worker claims it and calls
**`POST /mt5/order_check`** → `shadow_order_check` → **`mt5.order_check`** (validation
only). **Never** `order_send`.

```
signal → approval → plan → PLACE_ORDER_SHADOW → shadow worker
    → agent_order_check → bridge /mt5/order_check → mt5.order_check   ← NO order
```

### 2c. Proposed E3 demo-live path (NOT BUILT)
The signal path, when a new **demo-only LIVE mode** is engaged under all gates,
promotes to a **real `PLACE_ORDER`** job (not SHADOW). The normal worker executes it
via the *same* legacy `/mt5/order` → `order_send`. **This deliberately re-uses the
legacy live path's job type + bridge endpoint** — E3 adds no new order primitive; it
adds the governance ladder (approval → plan → promotion → gated real placement) *in
front of* the existing execution mechanism.

```
signal → approval → plan → [E3 gate] → PLACE_ORDER (real, demo-only) → normal worker
    → agent_order → bridge /mt5/order → mt5.order_send   ← REAL demo order
```

**Reconciliation principle:** legacy and E3 converge on the *same* `PLACE_ORDER` job
type, worker, bridge endpoint, and kill switch. The signal path is a **governed
producer** feeding the existing execution mechanism — not a parallel order stack.

---

## 3. Shared MT5 account / bridge resources

Both paths share, today, the **same** runtime:
- **MT5 account** — TradersWay-Demo `1121106` (`terminal64`, Windows box).
- **Bridge** — single instance on `:8788` (`mt5_signal_bridge.py`); `/mt5/order`
  (live) and `/mt5/order_check` (shadow) endpoints; single MT5 connection.
- **Normal ingest worker** — claims `OPEN_TRADE`/`PLACE_ORDER`/`PLACE_TEST_ORDER`.
- **Shadow worker** — claims `PLACE_ORDER_SHADOW` only (E2b-R2).

There is **one broker connection**; orders from either path land on the same account.

---

## 4. Mutual-exclusion rules (proposed)

1. **Distinct magic numbers** — legacy strategy trades vs signal-driven trades must
   use disjoint MT5 `magic` ranges so positions are attributable and separable.
2. **Shared exposure budget** — per-account exposure / max-open / concurrency limits
   (gap Area 7, pre-E3) must count **both** paths' open positions on the shared
   account, not just the signal path's.
3. **Worker segregation preserved** — the shadow worker remains shadow-only; only the
   normal worker executes real orders (legacy or E3). E3 does **not** grant the shadow
   worker order authority.
4. **Single bridge, serialised** — the bridge processes one order at a time; no change
   needed, but both paths must tolerate the other's in-flight order.

---

## 5. Unified kill switch (ALREADY CONVERGED — verified)

`KILL_SWITCH_BLOCKED_JOB_TYPES = {OPEN_TRADE, PLACE_ORDER, PLACE_TEST_ORDER,
PLACE_ORDER_SHADOW}` and the model-layer guard `ExecutionJob.save()`
(`execution/models.py:232`) raise `ExecutionKillSwitchEngaged` on creation of **any**
order-opening job while the kill switch is engaged. `order_creation_kill_reason()`
honours **both** the DB `ExecutionControl.kill_switch_engaged` flag **and** the
`GUVFX_EXECUTION_DISABLED` env flag.

**Finding:** the legacy path (`OPEN_TRADE`/`PLACE_ORDER`) and the signal path
(`PLACE_ORDER_SHADOW`, and future E3 `PLACE_ORDER`) already share **one** order-creation
choke point. No new kill-switch work is required for reconciliation — engaging it stops
*all* real orders from *both* paths. (Recommendation: add an operator "panic-stop"
one-command wrapper — gap Area 11.)

---

## 6. Demo-only boundary

Enforced at multiple layers, both paths:
- **Planning/promotion** — `account.is_demo` required; live `broker_server.environment`
  rejected (`signal_planning.py`, `signal_promotion.py`).
- **Bridge** — `execute_demo_order`/`shadow_order_check` reject `account_info.trade_mode
  != 0` (non-DEMO) before any MT5 call (`mt5_signal_bridge.py:~823`).
- **E3 proposal** — the demo-LIVE mode must be **account-scoped to demo only**; live
  accounts stay SHADOW-suppressed. E3 never enables real orders on a live account.

---

## 7. Rollback model

- **Kill switch** — engage `ExecutionControl.kill_switch_engaged` (or
  `GUVFX_EXECUTION_DISABLED=1`) to immediately stop *all* real order creation (both paths).
- **Mode revert** — set `ExecutionControl.signal_execution_mode` back to `SHADOW`
  (1-row DB update) to return the signal path to no-order.
- **Code/deploy** — image `rollback-*` tags + worker `.bak.*` scripts (established this
  programme) revert the worker/bridge/backend.
- **Legacy path unaffected** — reverting E3 does not touch the legacy Trading domain.

---

## 8. Operator approval gates

- **Signal path is 4 manual steps** (ingest → approve → plan → promote), each an
  operator command; **no auto-promotion** (D17 governance).
- **Approval RBAC / 2-person** (gap Area 7) required before E3 real placement.
- **E3 activation gate** — a demo-LIVE mode may be engaged **only** when: (a) broker
  timezone verified (**done — UTC+3 summer, PR #57**), (b) **this Blueprint ratified**,
  (c) **Nuno's recorded E3 sign-off**. The mode change is a deliberate, logged operator
  action, reversible in one step.

---

## 9. Reconciliation with the packet architecture

The packet architecture (E0→E2b) is an **additive governance ladder** on the existing
`ExecutionJob` execution mechanism. It does **not** replace the legacy path; it:
- reuses the same `ExecutionJob` model, worker, bridge, and **unified kill switch**;
- inserts approval + planning + promotion + suppression **in front of** order creation;
- keeps a clean separation (`signal_intake` never imports `execution`); and
- at E3, emits the **same `PLACE_ORDER` job** the legacy path already uses, under
  additional gates — so there is **one** real-order execution path, two governed
  producers (strategy engine, signal pipeline).

This satisfies `.claude/rules/architecture.md` (no silent architecture replacement;
separation of concerns preserved; simplest thing that works).

---

## 10. Reconciliation decision (PROPOSED)

**Adopt the "one execution mechanism, two governed producers" model.** E3 reuses the
legacy `PLACE_ORDER` → `/mt5/order` → `order_send` path, gated by a demo-only LIVE mode
behind the three E3 gates, sharing the unified kill switch and the demo-only boundary,
with distinct magic ranges and a shared exposure budget. No parallel order stack; no
new order primitive.

---

## 11. Open risks (for Nuno's acceptance)

| # | Risk | Severity | Mitigation |
|---|---|---|---|
| R1 | Single bridge / single broker connection = SPOF for all order flow | MEDIUM | Accept for pilot; document; consider redundancy post-E3 (gap Area 14 DR). |
| R2 | Shared demo account mixes legacy + signal trades | MEDIUM | Distinct magic ranges + shared exposure budget (Area 7); or a **dedicated demo account** for the signal path (recommended). |
| R3 | Missing runtime risk controls (exposure/drawdown/concurrency) become live-critical under E3 | HIGH | Land `E3-RUNTIME-RISK-CONTROLS` before enabling (gap Area 7). |
| R4 | Not all accounts have a `terminal_node`; legacy null-node fallback | MEDIUM | Node-assignment enforcement before E3 (gap Area 5/8). |
| R5 | Approval authZ weak (any admin) | MEDIUM | Approval RBAC + 2-person gate before E3 (Area 6/7). |
| R6 | Timezone offset is DST-dependent (summer +3 verified; winter unproven) | LOW | Re-probe after late-Oct-2026 DST; read recorded offset, never hardcode (PR #57). |

---

## 12. Ratification (Nuno) — REQUIRED

- [ ] **Nuno reviews** this reconciliation and the open risks (R1–R6).
- [ ] **Nuno records** acceptance of the residual risks (or requires mitigations first).
- [ ] **Nuno sets Status → APPROVED** (with date + any conditions) — the second of the
      three E3 hard blockers is then cleared.

Until this document is **Approved by Nuno**, it remains **PROPOSED** and E3 must not
proceed. Per `.claude/rules/notion.md`, the lifecycle status is the PM's/Nuno's to
advance — not the author's.

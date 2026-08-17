# AJ#7 — Strategy Customer Journey: Get → Configure → Enable → My Strategies

**Status:** design + implementation (frontend + minimal backend). **No deploy** — stops at the Sponsor visual/
product review gate. **No execution-engine changes.**

## 1. Product contract

```
MARKETPLACE  →  GET STRATEGY  →  CONFIGURE  →  ENABLE STRATEGY  →  (confirm modal)  →  MY STRATEGIES
```

- **Get** = acquire/own the strategy (does NOT trade).
- **Configure** = review + select the *genuinely supported* settings.
- **Enable** = the customer's explicit permission for THIS strategy to trade (an explicit confirmation modal).
- **My Strategies** = manage afterward (Edit / Enable-Disable / Remove), customer-safe state labels.

Customer-facing copy only ("Get Strategy", "Configure", "Enable Strategy", "Disable Strategy", "Free",
"Draft/Configured/Enabled/Paused/Needs attention"). Internal terms (arm, AUTO_DEMO, runtime_ready,
execution_ready, workspace execution, signal-copy readiness, broker-connectivity gate, authorization state)
stay in staff diagnostics only.

## 2. Forensic reality (why the design is shaped this way)

- **Only Wayond (mp-010, `signal_source=ti_signals`) has an automated-execution path.** Generic strategies
  (mp-001…mp-009) create `MANUAL`/`stage=TEST` assignments that the auto-router never routes (TEST excluded),
  and the assignments API cannot set `execution_mode=AUTO_*`. They are research/backtest definitions.
- **Wayond has NO per-customer execution config today.** The order is built entirely from **(a) the Telegram
  signal content** (symbol/SL/TPs) and **(b) `SignalSourceConfig`** — a single operator/per-provider row
  (unique on `source`), not per-customer. The customer's `StrategyAssignment` feeds *zero* sizing/TP/SL into
  execution; it is routing/entitlement only. Sizing = per-source global (`total_lot_target`); TP/SL =
  signal-driven; trailing = not implemented in this path; risk limits = global env. `AssignmentLegSizing`
  exists but is explicitly inert ("not wired to live execution"). **The only real per-customer knobs are
  account selection and enable/disable.**

**Sponsor decision (recorded):** build the honest journey shell. Do NOT create cosmetic sizing/TP/SL/trailing
controls. The Configure page shows the real contract honestly and is architecturally extensible so future
real controls plug into the SAME page. See §6 for the deferred backend workstream.

## 3. Domain contract (Phase 2 decision — NO migration)

Reuse `StrategyAssignment` as the ownership + lifecycle record; **no schema change**. The non-executing
"owned" state is expressed by `is_active=False` (the auto-router requires `execution_mode=AUTO_*` **and**
`stage=LIVE` **and** `is_active=True` **and** ADR-0047 authorized+enabled — an `is_active=False` row is inert).

| Product state | Backing (existing model) |
|---|---|
| Not owned | no `StrategyAssignment` |
| **Draft / Configured** | `StrategyAssignment(AUTO_DEMO, ti_signals, stage=LIVE, is_active=False)` |
| **Enabled** | `is_active=True` **and** ADR-0047 `execution_authorized_at` set **and** `execution_enabled` |
| **Paused** | previously enabled, now `is_active=False` |
| **Needs attention** | readiness `ambiguous` / a fail-closed gate |

- **Get** = create the assignment `is_active=False` (owner-scoped, demo-only, cohort + `BETA_SELF_SERVE_ARM_ENABLED`
  gated — the same acquisition gates, minus execution readiness, since nothing executes yet). Idempotent
  (find-or-return). Redirects to Configure.
- **Enable** = the existing `signal_copy_arm` (reactivates → `is_active=True`) behind the full fail-closed gate
  set (readiness incl. ADR-0047, cohort, single-tenant), preceded by the ADR-0047 authorization fold (§5).
- **Disable** = `signal-copy/toggle` `is_active=False`. **Remove** = delete the assignment.

No order can arise from Get or Configure: an `is_active=False` assignment is never routable, and an order still
requires BOTH an active assignment AND ADR-0047 authorized+enabled (unchanged order-time gate).

## 4. Configure page (Phase 5 — honest, extensible, no cosmetic controls)

Driven by a **strategy configuration schema** (a per-strategy descriptor: field key, label, type, editable?,
value, help) so the page is not hard-coded to Wayond and future controls attach here. For Wayond today the
schema contains **editable: account selection** and **read-only (managed) rows**: provider, market (XAUUSD),
timeframe, position sizing = "Managed by GuvFX/Wayond (beta)", take-profit = "Follows the signal provider's
TP1/TP2/TP3", stop loss = "Set by the signal provider", trailing = "Not used by this strategy", plus a beta
note that advanced customisation is not yet available. Final CTA: **Enable Strategy** (if not enabled) /
**Save Changes** + **Disable Strategy** (if enabled).

## 5. Enable + ADR-0047 authorization fold (Phase 7 decision)

`authorize_workspace_execution` is idempotent, owner-scoped, and requires EXECUTION_READY+confirmed+matched;
it is **safe to drive from the Enable action**. Chosen approach: **frontend orchestration** of the two
existing endpoints (no new combined auth-path endpoint — avoids an AMBER API-surface change): on the Enable
**confirmation** the client calls `authorizeExecution()` (writes `execution_authorized_at` + arms the
workspace) then `signal-copy/arm` (activates the assignment). **Consent integrity (ADR-0047):**
`execution_authorized_at` is written ONLY from the explicit confirmation click — never on page load or as an
implicit side effect — and the modal copy states the authorization plainly ("This will allow GuvFX to place
trades automatically on <account> using this strategy"). If the workspace is not yet EXECUTION_READY (the
normal fresh/weekend state), Enable degrades to a clear "getting ready" state, never an error, never a bounce.

## 6. DEFERRED — next Wayond product workstream (per-customer configuration)

AJ#7 deliberately does NOT build these; it makes the Configure page ready to host them. Each requires backend
execution-path work (RED — order path) and its own packet:

- **Customer lot sizing** — activate `AssignmentLegSizing.lot_per_leg` (`strategies/models.py:577-679`, currently
  inert) in the live planning/promotion path (`execution/signal_planning.py`, `signal_promotion.py`) so a
  per-assignment lot overrides the per-source `total_lot_target`.
- **TP selection (e.g. TP1-only) + partial ratios** — a per-assignment TP policy consumed in
  `signal_planning.py` leg construction (currently 1 leg per parsed signal TP).
- **Trailing-stop enable/disable** — trailing is not implemented in the ti_signals path at all; needs a new
  trade-management capability + per-assignment toggle.
- **Per-customer risk limits** (daily drawdown, max concurrent, exposure) — currently global env in
  `risk_controls.py`; would need per-assignment overrides threaded through the risk checks.

These controls must plug into the SAME Configure page schema built in AJ#7 (extend the descriptor with
editable fields), not a Wayond-specific dead end.

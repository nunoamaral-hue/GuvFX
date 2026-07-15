# 0012 — Source-scoped lot sizing (ti_signals 0.40/leg) + free-margin guard

- Date: 2026-07-15
- Status: Accepted

## Context

The "Wayond WIM Strategy" (`ti_signals`) must size 0.40 lots per TP leg (1.20 lots per 3-leg
signal), **TI-only** — the original Wayond auto-demo (`wayond`) keeps its sizing. A lot-gate
inventory found every lot/exposure cap in the pipeline is a **global, source-blind constant**; the
only per-source knob was `SignalSourceConfig.total_lot_target`, and it is a *requested* amount that
the split silently clamps to the global caps. So no global constant could be raised without
enlarging wayond (and manual demo trades) in lockstep. Reaching 0.40/leg required source-scoping
the caps at every stage: planning split → promotion re-validation → order payload → worker → bridge,
plus an exposure raise and a free-margin guard (there was **no** margin/equity check anywhere).

## Verified facts

- Sizing gates were global: `SIGNAL_MAX_LOT_SIZE=0.02`, `MAX_TOTAL_LOT_PER_SIGNAL="0.06"`
  (`execution/models.py`); the split `min(requested, n·max_per_leg, max_total)`
  (`signal_planning.py`) silently clamps; promotion re-validates against the same constants
  (`signal_promotion.py`); worker cap `0.02` (`mt5_trade_ingest_worker.py`); bridge caps `0.02` at
  4 sites (`scripts/mt5_signal_bridge.py`).
- Exposure caps were `0.50` account + `0.50` symbol (`risk_controls.py`), source-blind; one
  1.20-lot TI signal is blocked by them.
- No free-margin/equity/margin-level check existed anywhere (grep-verified) — only a $100 realized
  daily-drawdown gate.
- `ti_signals` trades **XAUUSD only**; `wayond` has **no plans** on the shared demo account #1 yet;
  the order payload carried no `signal_source` and hardcoded `comment="WAY.."` for both sources.
- The bridge `order_check` (no order placed) already returns `margin` + `free_margin`; it also
  exposes `equity` + `margin_level` on the MT5 result.

## Assumptions

- The order payload is trusted (constructed by promotion in the listener) — the worker/bridge honour
  its `max_lot`, bounded by a hard technical ceiling that no payload can exceed.
- Broker XAUUSD `volume_step` admits 0.40 (a clean 0.01 multiple) — confirmed by live `order_check`
  preflight before enabling (see Evidence).
- 50,000 USD demo leverage gives ample margin at 2.40 lots (worst case XAUUSD 1:100 ≈ $5.8k, ~11%).

## Decision drivers

Smallest safe additive change; wayond behaviour-preserving; fail-closed sizing; bounded exposure
(not a blind 20×); a real margin safety net at 20× lot size; reversibility; governance (Amber).

## Options considered

- **Per-source SIZING via `SignalSourceConfig` caps (chosen).** Add `max_lot_per_leg` /
  `max_total_lot` (defaults = the global constants). Thread them through split + promotion; carry
  `signal_source` + `max_lot` in the payload; worker/bridge admit up to the payload cap, fail-closed
  to 0.02, bounded by a hard ceiling. No global constant changes → wayond untouched.
- **Exposure: raise the shared caps to 2.40 + free-margin guard (chosen)** vs per-source exposure
  filtering vs a dedicated demo account. The shared raise is safe because per-source *sizing* keeps
  wayond ≤ ~0.40 aggregate; the free-margin guard is the real aggregate protection. Per-source
  filtering removes the true account aggregate; a dedicated account is a large infra change.

## Decision

Source-scope sizing at every gate via per-source `SignalSourceConfig.max_lot_per_leg` /
`max_total_lot` (ti → 0.40 / 1.20; all others default 0.02 / 0.06, fail-closed). Add `signal_source`
+ `max_lot` to the promotion order payload; the worker + bridge (no DB) enforce the payload cap,
fail-closed to 0.02, bounded by a hard technical ceiling (1.0 worker / bridge). Raise the shared
account + symbol exposure ceilings 0.50 → **2.40** (env-tunable), and add a **free-margin guard** at
promotion that reads live projected margin level via the bridge `order_check` and rejects below a
300% floor (fail-open on read error; only runs for orders above the 0.06 default total).
Nuno-approved (Amber): shared-caps-to-2.40 + margin guard; deploy once green + preflight passes.

## Consequences

- ti_signals sizes 0.40/leg (1.20/signal); wayond unchanged (its per-leg cap stays 0.02).
- Additive migration `execution/0017` (two nullable-default Decimal fields). Config: ti row set to
  total 1.20 / per-leg 0.40 / total 1.20.
- New reject codes stay the same strings (`lot_out_of_range`, `total_lot_exceeds_cap`) plus
  `margin_level_too_low`. Promotion now makes one bridge `order_check` for larger orders (fail-open).
- The shared exposure raise couples TI and wayond only at the 2.40 account aggregate; in practice
  wayond's tiny sizing never approaches it.

## Risks and controls

- **20× lot on the live order path (Red).** Controlled: source-scoped fail-closed caps at 6 gates;
  hard technical ceilings; the free-margin guard; a kill-switch deploy window; live `order_check`
  preflight (no `order_send`). Both strategies stay armed.
- **Cap leakage to wayond.** Prevented: no global constant changed; defaults preserve wayond; a test
  asserts a high wayond `total_lot_target` still clamps to 0.02/leg.
- **Silent bridge clamp desync.** All 4 bridge sites (incl. the `min()` clamp) moved to the
  source-scoped `_effective_max_lot` in lockstep.

## Evidence / validation

- +13 CI tests (`execution/tests_source_scoped_sizing.py` + payload/worker/guard/exposure). Full
  deterministic backend suite green in CI.
- Live broker `order_check` preflight at 0.40 XAUUSD on the demo (retcode, margin, projected
  margin_level; `order_send_called=False`) — recorded in the packet evidence. Not covered by CI
  (needs live MT5): the bridge cap sites + broker volume are validated by the preflight + deploy.

## Reversal path

Independent rollbacks: set the ti `SignalSourceConfig` caps back to 0.02/0.06/0.03 (DB, instant);
env-override `RISK_MAX_ACCOUNT/SYMBOL_EXPOSURE_LOT=0.50`, `RISK_MARGIN_GUARD_ENABLED=false`; retag
`:rollback-preTiSizing` backend/worker/listener images; the migration is additive (columns stay,
harmless). No open position is closed or corrupted by any rollback.

## Revisit trigger

If wayond starts trading the shared account materially, or a second large-size source is added
(re-evaluate per-source exposure isolation / a dedicated account), or the demo margin headroom
changes.

## Approval

Nuno — Amber approvals recorded: exposure posture (shared caps → 2.40 + margin guard) and deploy
timing (deploy once CI green + preflight passes). PM owns lifecycle status.

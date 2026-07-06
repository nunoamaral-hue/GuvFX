# E3 Demo-Execution Roadmap — shortest path to the first automatic Wayond demo trade

> **Packet:** GFX-PKT-E3-DEMO-EXECUTION-PROGRAMME (planning/architecture review only — no
> implementation, no deploy, no arming, no production change). Compiled 2026-07-06 from an
> assumption-challenging review of the **current** codebase + production state (6-agent read-only
> sweep). **E3 remains RED.**

**E3 defined:** one fully-automatic Wayond **demo** trade — armed signal → auto-plan → **real
`order_send` on a demo account** → monitored to close → win/loss → notification candidate.

## Executive summary
E3 needs **no new execution engine.** The real-order path already runs in production for the
strategy engines: `ExecutionJob.PLACE_ORDER → guvfx-mt5-trade-ingest-worker → agent_order() →
bridge :8788 /mt5/order → order_send → real demo ticket` (verified end-to-end). `SYNC_POSITIONS`
is **already auto-enqueued** on `PLACE_ORDER` completion ([views.py:390](../backend/execution/views.py:390)) —
a once-assumed gap that is **done**. The whole downstream close→classify→route→candidate chain
(`close_monitor`, `outcome_router`, `TradeOutcomeRecord`, `NotificationCandidate`, and the three
`run_*` management commands) exists and is idempotent. `Trade.correlation_id` **exists as a field**
([trading/models.py:197](../backend/trading/models.py:197)) — only its *population* is missing.

So the true engineering surface is small "ignition wiring." The real blockers are **not
engineering** — they are two **Nuno-only** governance approvals (Blueprint-06 ratification + the
recorded E3 sign-off) plus operational scheduling/preflight. **A dry-run notification is sufficient
for the first demo trade;** the real Telegram transport is explicitly *not* on the critical path.

## Current E3 completion: ~62%
Done + directly reusable (the expensive ~70–80% of machinery): the live real-order path; the
`SYNC_POSITIONS` auto-enqueue; the full close→candidate chain; the `Trade.correlation_id` field; the
`auto_router` AND-of-gates skeleton; risk controls (`evaluate_promotion_risk`); RBAC
(`review_signals`); node-assignment enforcement (flag-gated); all three monitor commands; **timezone
cleared for summer** (UTC+3, PR #57). Not 80% because the ignition wiring is unbuilt, the
correlation-linkage gap is **critical** (without it a WIN never traces to the signal → orphaned
candidate), the two governance approvals are hard gate-closed blockers, and the ops estate (no DB
backup, bridge SPOF, monitors unscheduled) is materially incomplete even for a gated pilot. Not
counted against E3: real Telegram transport, multi-leg hedging, auto-live, slippage fields, WIMS
loss rework.

## Remaining engineering (all repo-only, fail-closed by default)
| # | Item | Effort | Blocking |
|---|---|---|---|
| E1 | Add `SignalExecutionMode.DEMO` enum to `ExecutionControl` (+ additive migration); nothing wired | S | ✅ |
| E2 | `promote_plan_to_demo_jobs()` — parallel of `promote_plan_to_shadow_jobs`, creates **`PLACE_ORDER`** (not `_SHADOW`) under DEMO mode; reuse `_validate` (accept DEMO) + demo-only guards | M | ✅ |
| E3 | The DEMO real-order **payload** — market-only, symbol/side/lots/SL/TP/windows_username, **+ `correlation_id` + `signal_timestamp`** (both required downstream) | M | ✅ |
| E4 | **Populate `Trade.correlation_id` in `upsert_trades`** (thread the originating job's `correlation_id` via the `SYNC_POSITIONS` trigger). **CRITICAL** — closes the signal→trade linkage so the WIN candidate isn't orphaned | M | ✅ |
| E5 | Wire `StrategyAssignment.AUTO_DEMO` + `SignalExecutionMode.DEMO` through `auto_router.effective_mode()` (new `MODE_AUTO_DEMO` branch → `promote_plan_to_demo_jobs`); no-op at defaults | M | ✅ |
| E6 | SL/TP-side-of-market execution gate (§6A) — reject a signal whose absolute SL/TP already crossed the market | M | ⬜ optional |
| E7 | Tag `NotificationCandidate`/`TradeOutcomeRecord` with `is_demo`/tier (message reads "(DEMO)") | S | ⬜ optional |
| E8 | **Real** Telegram transport (`RealTelegramTransport`) — credential-gated; **not on the critical path** (dry-run proves the loop) | M | ⬜ later |

## Remaining operational
| # | Item | Blocking |
|---|---|---|
| O1 | Schedule the 3 monitor crons: `run_close_monitor`, `run_outcome_router`, `dispatch_notifications` (dry-run) — commands exist, only the schedule is missing | ✅ |
| O2 | Deploy E3 code to the **shared** `guvfx-prod-guvfx-backend` image (rebuilds the live trading image) — needs a kill-switch window: engage kill switch + pause strategy crons → rsync+rebuild → additive migration → force-recreate → un-pause | ✅ |
| O3 | Confirm the normal ingest worker healthy + that a Wayond `PLACE_ORDER` is claimed by it (shadow worker only claims `_SHADOW`); confirm the demo account's `windows_username`/node | ✅ |
| O4 | Confirm the target demo account has an ACTIVE `TerminalNode` (`audit_node_assignments --strict` = 0 FAIL) if `RISK_REQUIRE_TERMINAL_NODE` is on | ✅ |
| O5 | MT5 bridge SPOF (single Windows box, manual/autologon, not health-polled) — acceptable for a **supervised** first trade if confirmed up; add health-poll+alert before unattended running | ⬜ accept-for-pilot |
| O6 | No automated DB backup (RED) — reversible/low-value for a demo trade; document risk acceptance, schedule daily off-host backup before any ramp | ⬜ accept-for-pilot |
| O7 | Confirmed alert delivery + container healthchecks — needed before unattended operation, not a single supervised trade | ⬜ later |

## Remaining governance
| # | Item | Nuno-only | Blocking |
|---|---|---|---|
| G1 | **Nuno's recorded E3 sign-off** — explicit approval for the first real demo `order_send` from a Wayond signal (record in STATUS.md) | ✅ | ✅ |
| G2 | **Blueprint-06 ratification** (PROPOSED, PR #58 held open) — Nuno reviews R1–R6, checks the ratification boxes, records APPROVED, merges | ✅ | ✅ |
| G3 | Decide `RISK_REQUIRE_TERMINAL_NODE` for E3 (on → all demo accounts need a node; or accept off for the pilot) | ✅ | ✅ |
| G4 | Confirm the **DEMO risk caps** (recommend restricting E3 to EURUSD, tiny lot; `SIGNAL_MAX_*`) | ✅ | ✅ |
| G5 | Exposed-secret rotation (Nuno-held) — recommended pre-real-order; may be risk-accepted + documented for a low-value pilot | ✅ | ⬜ |
| G6 | Confirm market-only policy (already true) + whether the §6A side-of-market gate is required for the first trade | ✅ | ⬜ |

## Dependency graph (what blocks what)
```
Blueprint-06 ratified (G2) ─► E3 sign-off + caps + node decision (G1,G3,G4) ─► ARM the 5 gates
SignalExecutionMode.DEMO (E1) ─► promote_plan_to_demo_jobs (E2) ─► DEMO payload w/ correlation_id+ts (E3)
                                       └─► auto_router AUTO_DEMO wiring (E5)
DEMO payload (E3) ─► Trade.correlation_id populated (E4) ─► close_monitor linkage ─► outcome_router candidate
E1..E5 built ─► deploy to shared image (O2) ─► real PLACE_ORDER path live
3 monitor crons (O1) ─► close→outcome→candidate actually runs
worker health + node (O3,O4) + bridge up (O5) ─► order reaches the bridge and returns a ticket
ALL of the above + provider ARMED + 5 flags ─► one INTAKEN Wayond signal auto-fires a real demo order
```

## Critical path
1. **Nuno ratifies Blueprint-06** (merge to main) — hard gate.
2. **Nuno records E3 sign-off** + confirms risk caps + node-enforcement decision — hard gate.
3. Eng E1 — `SignalExecutionMode.DEMO` enum (+migration).
4. Eng E2+E3 — `promote_plan_to_demo_jobs()` + DEMO real-order payload (correlation_id + signal_timestamp).
5. Eng E4 — populate `Trade.correlation_id` in `upsert_trades` (**critical linkage**).
6. Eng E5 — `auto_router` AUTO_DEMO wiring.
7. Deploy E3 code to the shared image (kill-switch window, additive migration).
8. Schedule the 3 monitor crons.
9. Preflight: worker healthy + demo account node assigned + bridge up.
10. Arm the 5 gates under Nuno's authority.
11. Observe **one** Wayond signal → real demo ticket → SYNC → close → outcome → candidate (dry-run render). Disarm; capture evidence.

## Recommended packet sequence (today → first demo trade)
1. **GFX-PKT-BLUEPRINT-06-RATIFICATION** *(Nuno, RED)* — ratify Blueprint-06 + record E3 sign-off, caps, node decision. Unblocks all arming.
2. **GFX-PKT-E3-DEMO-PROMOTION** *(eng, repo-only; AMBER to merge)* — `SignalExecutionMode.DEMO` + `promote_plan_to_demo_jobs` + DEMO payload; tests prove `PLACE_ORDER`-only-under-DEMO, fail-closed at defaults.
3. **GFX-PKT-E3-TRADE-LINKAGE** *(eng, repo-only; GREEN)* — populate `Trade.correlation_id`; tests prove a linked, non-orphaned candidate. *(Closes the critical gap.)*
4. **GFX-PKT-E3-AUTO-DEMO-ROUTER** *(eng, repo-only; AMBER)* — wire AUTO_DEMO through `auto_router`; disabled by default.
5. **GFX-PKT-E3-MONITOR-SCHEDULING** *(ops; GREEN)* — cron the 3 dry-run monitors.
6. **GFX-PKT-E3-DEPLOY-AND-PREFLIGHT** *(ops; AMBER)* — deploy to the shared image in a kill-switch window; confirm worker/node/bridge; document DB-backup + bridge-SPOF risk acceptance.
7. **GFX-PKT-E3-FIRST-DEMO-TRADE** *(ops, Nuno-supervised; RED)* — arm the 5 gates, observe exactly one trade end-to-end, disarm, capture the E3 evidence + handoff.

Packets 2–5 are buildable/mergeable now behind fail-closed defaults (referencing the ratified
Blueprint-06); nothing **arms** until packets 1 + 6 + the operator flips are done. E8 (real Telegram
transport) and the winter DST re-probe are deliberately **after** the first demo trade.

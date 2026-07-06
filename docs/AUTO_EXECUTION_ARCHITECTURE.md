# Wayond Auto-Execution Architecture (PROPOSED — design only)

> **Status:** PROPOSED — governance decision, awaiting Nuno's ratification.
> **Packet:** GFX-PKT-AUTO-EXECUTION-ARCHITECTURE. **No code or behaviour change** ships with
> this document. No provider arming, no order_send, no E3, no deploy. E3 remains **RED**.
> **Scope:** the target Wayond strategy flow — remove *per-signal* human approval from the live
> path, move the human gate *upstream* to configuration/governance, keep a manual path for
> onboarding/testing/debug/validation.

## 1. Principle — the safety inversion

Per-signal human review is replaced by **config-time arming** plus a chain of **automated,
fail-closed gates**. The human still authorises — **once, at arming, with an audit event** —
instead of once per signal. Crucially, **auto mode introduces no new execution path**: a new
downstream router calls the *same* `approve() → plan_demo_execution() → promote_plan_to_shadow_jobs()`
functions Nuno already invokes by hand, each of which re-validates its own gates independently.
The manual `PendingSignalApproval` path is **preserved unchanged** and remains the default.

## 2. The four modes

| Mode | Per-signal approval | Execution | Real order? | Account | Master enable |
|---|---|---|---|---|---|
| **Manual** (default) | **Human** approve/reject | shadow after approval | No | demo | — (always available) |
| **Auto-shadow** | none (config-armed) | `PLACE_ORDER_SHADOW` → `order_check` **dry-run** | **No** (structural) | demo | flags below, `signal_execution_mode=SHADOW` |
| **Auto-demo** *(future, RED)* | none (config-armed) | real `PLACE_ORDER` → `order_send` | **Yes**, demo acct | demo | + `SignalExecutionMode.DEMO` enum + E3-equiv sign-off |
| **Auto-live** *(future, fully RED)* | none (config-armed) | real `order_send` | **Yes**, live acct | live | + `SignalExecutionMode.LIVE` + own blueprint packet |

Each tier adds exactly **one** capability over the previous; every tier defaults **OFF/Manual**.
**Auto-shadow is the first buildable milestone and is structurally incapable of placing a real
order** (three independent layers: `next_job` shadow-permission guard, worker `execution_mode != SHADOW`
refusal, `agent_order_check` is validation-only).

## 3. How each mode handles each concern

| Concern | Manual | Auto-shadow | Auto-demo (future) | Auto-live (future) |
|---|---|---|---|---|
| Signal acquisition | `acquire_message` (unchanged, all modes) | same | same | same |
| Parser result | human reads it | must pass **certification ≥ MEDIUM** (gate 0) | same | same |
| Approval gate | **human** `PendingSignalApproval` | auto `approve()` by system actor (config-armed) | same | same |
| Planning | `plan_demo_execution` after approve | same (auto-invoked) | same | same |
| Risk controls | run at plan/promote | same 12-gate chain, auto | same + tightest caps | same + live caps |
| Execution | `PLACE_ORDER_SHADOW` (dry-run) | `PLACE_ORDER_SHADOW` (dry-run) | real `PLACE_ORDER`→demo | real `PLACE_ORDER`→live |
| Trade lifecycle | n/a (no real trade) | n/a (no real trade) | SYNC_POSITIONS monitors to close | same |
| Close detection | n/a | n/a (validate on demo/fixtures) | **new** close-monitor | same |
| Win/loss | n/a | n/a | `TradeResultProducer` + `is_winning_trade` | same |
| Telegram notify | none | none | **profit-only DM to Nuno** (new sink) | same |
| WIMS handoff | manual | manual | WIN → `ingest_winning_trade` → WIMS review | same |

## 4. Approval-bypass design

**Fork point:** a **new** `execution/auto_router.py`, invoked **after** `acquire_message` returns
`INTAKEN` — **not** inside `signal_intake` (which must stay a one-way dependency: `execution`
imports `signal_intake`, never the reverse). `services.intake_parsed` **still creates the
`PendingSignalApproval` PENDING row** — that row is the audit anchor and `correlation_id` mint
point in every mode. The router then decides whether to auto-advance:

```
acquire_message → INTAKEN (+ PENDING approval created, unchanged)
        │
        ▼
execution.auto_router.should_auto_execute(approval)   # NEW, downstream
        │  all config gates true AND not edited AND certification ≥ MEDIUM ?
   ┌────┴─────────────────────────┐
  no                             yes
   │                              │
 manual gate                approve(system_actor) → plan_demo_execution() → promote_plan_to_shadow_jobs()
 (unchanged)                (the EXACT existing functions; each re-validates its own gates)
```

**Fail-closed / behaviour-preserving:** with all mode flags at default, `should_auto_execute`
returns `(False, reason)` → the router is a **no-op** and every signal lands in the existing
manual gate exactly as today. Any router exception → log and fall through to manual (approval
stays PENDING). **Edited signals (`source_edited` / `reason='edited_review'`) are hard-excluded
from auto** — always manual, per the ratified WAYOND-EDIT-MEDIA policy.

## 5. Mode source of truth (SSOT)

There is **no single mode field** and there must not be — the effective mode is the **AND**
(minimum/safest) of independent, defense-in-depth flags. `execution/auto_router.py:effective_mode()`
composes them:

- **Global** — `ExecutionControl` (singleton): `signal_execution_mode` (today **SHADOW only** —
  this is the demo/live master lever; DEMO/LIVE enum values are added only in later gated packets)
  **+ NEW** `auto_execution_enabled` (BooleanField default **False**) — an auto-only soft pause,
  independent of `kill_switch_engaged` (kill switch blocks *all* order creation incl. manual; the
  new flag pauses *auto only*, leaving manual approval fully working).
- **Per-source** — `SignalSourceConfig.auto_demo_execution_enabled` (default **False**) — the
  existing per-provider arming gate, already wired into plan + promotion.
- **Per-strategy** — **NEW** `StrategyAssignment.execution_mode` (MANUAL/AUTO_SHADOW/AUTO_DEMO/
  AUTO_LIVE, default **MANUAL**), kept separate from the existing `.stage`; also honours `is_active`.
- **Account** — `account.is_demo` must match the tier.
- **Parser** — `certification_confidence` level ≥ threshold (proposed MEDIUM).

**Config-time arming (the human gate, moved upstream)** — each a deliberate, audited action
(`updated_by` FK): (1) arm provider → ARMED; (2) arm source (`auto_demo_execution_enabled=True`);
(3) set `StrategyAssignment.execution_mode=AUTO_SHADOW`, active, stage LIVE; (4) confirm
`signal_execution_mode` tier and flip `auto_execution_enabled=True` (the last, most-visible switch);
(5) ensure kill switch off. A **new RBAC permission `execution.configure_auto_execution`** (distinct
from `signal_intake.review_signals`) gates arming.

## 6. Risk-control gates (every auto signal must clear all)

Reuses the existing fail-closed chain; only **gate 0 is new**:

0. **Parse confidence** (NEW, in the router): `certification_confidence ≥ MEDIUM`, else MANUAL.
1. Provider **ARMED** (`SignalProvider.is_armed()`; non-armed already `DROPPED_NOT_ARMED` upstream).
2. Approval **APPROVED** (set by the router's `approve()`; permission-denied → stop).
3. **Kill switch** off (`order_creation_kill_reason()==None`) — re-checked at plan, promote, and
   innermost in `ExecutionJob.save()` (`KILL_SWITCH_BLOCKED_JOB_TYPES`). Three layers.
4. Global **execution mode** matches tier (`signal_execution_mode==SHADOW` for auto-shadow).
5. Per-source **armed** (`SignalSourceConfig.auto_demo_execution_enabled`).
6. **Demo-only** account (`is_demo` + `environment != live`) — live structurally rejected pre-E3.
7. **Symbol allowlist** + stop-loss present + ≥1 TP + valid direction.
8. **Staleness** re-check (plan, promote, and worker via payload timestamp).
9. **Volume/lot caps** (`SIGNAL_MAX_LOT_SIZE`, `MAX_TOTAL_LOT_PER_SIGNAL`).
10. **Runtime risk controls** (`evaluate_promotion_risk`): account/symbol exposure, max-open,
    daily drawdown, concurrent groups, node assignment — any exception → blocks.
11. **Structural un-claimability** (not bypassable): `next_job` shadow-permission guard + worker
    `execution_mode != SHADOW` refusal + `order_check`-only. Guarantees no order in auto-shadow.

## 7. Trade close handling + win/loss

Classification exists and is pure; only the **trigger** is missing.

- Order → worker → `complete()` auto-queues **SYNC_POSITIONS** → `upsert_trades()` writes
  `Trade.close_time / close_price / profit / commission / swap` (sole writer of `close_time`).
- **GAP (to build):** a background **close-monitor** (scheduled) querying closed-not-yet-delivered
  trades. Requires **`Trade.delivered`** idempotency marker + **`Trade.execution_job_id`/
  `correlation_id`** link (so a closed trade traces to its signal and demo-vs-live is tagged).
- Classification (**exists**): `TradeResultProducer.produce(trade)` (fail-closed: raises on
  open/corrupt) → `net_pnl = profit+commission+swap`, outcome WIN/LOSS/BREAKEVEN.
- Fork (**exists**): `is_winning_trade(trade)` → **strictly `net_pnl > 0`** (breakeven is *not* a win).

## 8. Profit-only Telegram notification design (design only — not implemented)

- **Attach point:** a **new** `execution/notifications.py:notify_nuno_winning_trade(contract)`
  invoked **only** on the WIN branch, immediately after `ingest_winning_trade` succeeds — so it
  rides the same profit-only gate that already rejected losers. Belt-and-braces: the function
  re-checks `result_type==WIN`.
- **Transport:** the pattern exists in `reliability/services/alerting.py:_deliver`
  (POST `api.telegram.org/bot<TOKEN>/sendMessage`). **Use a distinct bot token + Nuno's chat_id
  in env** (never git) — do **not** reuse the reliability credentials (research/paper/prod
  credential separation, per `security.md`).
- **Content:** the agreed win-story format = `results_card.render_card` + `caption.build_caption`.
- **Fail-closed:** token/chat unset → no-op (log only); POST failure → log and continue; never
  blocks/reverses the trade or the WIMS contract, and **never fires on a loss**.

## 9. Loss handling (+ a required behaviour change to flag)

Target: **LOSS/BREAKEVEN → internal record only, no Telegram, no WIMS public story.** The
profit-only gate already blocks Telegram. **But today the generic `ingest_trade_result` path still
creates a WIMS `ConsumptionContract` for losers.** To meet the target, losers must be routed to an
**internal-only record** (new `TradeResultLog` or `Trade.outcome`) and **must not** call
`deliver_trade_result`. **This is an AMBER change** (touches the WIMS boundary) and needs Nuno's
explicit approval — flagged, not silently made.

## 10. WIMS handoff design

Mostly exists: WIN → `ingest_winning_trade` → `deliver_trade_result` → `wims.create_contract`
(`ConsumptionContract` result_type=WIN, media = card+caption) → WP-3 workflow. The
**`AWAITING_REVIEW` human gate is mandatory today** — no win auto-publishes to the public channel.
**Recommendation: keep this gate** initially; auto-publishing public wins is a separate Nuno
decision. So: Nuno's private DM (§8) can fire on every win; **public** WIMS publication keeps a human.

## 11. Implementation sequence (all repo-only, behaviour-preserving, auto-shadow first)

0. **Verify fail-closed defaults** in prod (SHADOW, all sources un-armed, tight caps) — evidence only.
1. **Additive schema, nothing wired:** `ExecutionControl.auto_execution_enabled` (default False),
   `StrategyAssignment.execution_mode` (default MANUAL). `make check`.
2. **Wire parse-confidence** (read-only) so `effective_mode` can read a stored level.
3. **Build `auto_router.py` disabled-by-default:** `effective_mode()` (AND-of-gates) +
   `should_auto_execute()`; invoked after INTAKEN; no-op at defaults; edited hard-excluded. Tests
   prove behaviour-preservation. **This is the auto-shadow milestone** (shadow jobs only).
4. **Enable auto-shadow for ONE provider** in a demo/staging context; observe dry-run, no order_send.
5. **Close-monitor + profit-only fork** (`Trade.delivered` idempotency + signal link); losers →
   internal-only; keep WIMS review gate.
6. **Profit-only Telegram sink** (`notify_nuno_winning_trade`), distinct token, agreed format.
7. **Auto-demo (RED, separate packet):** add `SignalExecutionMode.DEMO` + a real `PLACE_ORDER`
   promotion branch (demo only). Requires **broker-timezone verification + Blueprint-06 + explicit
   Nuno sign-off**. Tightest caps.
8. **Auto-live (fully RED, own blueprint packet):** LIVE enum + live promotion branch + per-account
   live-approval + live kill switch + kill-switch alerting + heartbeat dead-man-switch.

## 12. Risks

1. **Mis-parse** — auto removes the last human eyeball on entry/SL/TP; a HIGH-confidence parser can
   still mis-read an unusual message. Mitigated by gate-0 confidence, allowlist, mandatory SL,
   staleness — argues for a long auto-shadow soak to accumulate parser evidence.
2. **Edited-signal leakage** — must hard-gate `source_edited → MANUAL` in the router.
3. **Velocity/exposure** — auto fires instantly on every armed signal; a too-loose cap is not an
   exception, so it silently permits more. Review caps before each ramp step.
4. **Silent live-arming via config creep** — the SSOT is an AND of several flags; require the
   `execution.configure_auto_execution` permission, audited `updated_by`, and the final
   `auto_execution_enabled` flip as the deliberate go-live switch.
5. **No kill-switch alerting / dead-man-switch** — kill switch is manual-only; needs monitoring on
   transitions + heartbeat auto-engage before real orders.
6. **Loss over-delivery** — until the loss path is re-routed, losers still leak into WIMS.
7. **Double-publication** — a polling monitor needs the `Trade.delivered` idempotency marker.
8. **Demo/live confusion** — the notify path needs `source_stage`/`execution_mode` tagging.
9. **Timezone unverified** — auto-demo/live must wait on broker-tz verification (staleness + timing).
10. **Governance** — auto-demo/live are RED; the human-gated config path is the control; the
    router/model must never self-arm (no unrestricted LLM live-trading authority).

## 13. Nuno decisions required

1. Approve the **approval-bypass fork** (new downstream router; `intake_parsed` still mints the
   PENDING approval as audit anchor; no edit to `signal_intake`).
2. Approve **moving the human gate to config-time arming** + the new
   `execution.configure_auto_execution` permission.
3. Confirm the **mode SSOT** (keep `signal_execution_mode` as master lever; add
   `auto_execution_enabled` + `StrategyAssignment.execution_mode`).
4. Decide the **parse-confidence threshold** for auto (proposed **MEDIUM**).
5. Confirm **edited signals never auto-trade** (recommended: NO).
6. **RED sign-off** required before Step 7 (auto-demo, real demo order_send) — needs timezone +
   Blueprint-06 + explicit approval.
7. **RED sign-off** required before Step 8 (auto-live) — own blueprint packet.
8. Decide **notification policy:** DM to Nuno on every win via a distinct bot token; keep WIMS
   `AWAITING_REVIEW` for public wins (recommended); confirm the win-story content format.
9. Approve the **loss-path behaviour change** (losers → internal-only, no WIMS) — AMBER.
10. Confirm **risk caps** per ramp step + whether `RISK_REQUIRE_TERMINAL_NODE` must be ON before auto-demo.

## 14. Safety invariants (hold across all modes)

- Manual approval path is **never removed** (onboarding/testing/debug/validation).
- Auto adds **no new order path** — it reuses `approve → plan → promote-to-shadow`.
- Every gate is **fail-closed**; unknown/unset → Manual/no-execution.
- **Auto-shadow places no real order** (structural, three layers).
- Real order_send (auto-demo/live) stays **RED** behind timezone verification, Blueprint-06, and
  Nuno's explicit sign-off. No LLM/router self-arming.

# Post-Incident Review — Orphaned PLANNED Plan Concurrency Leak

**Incident status:** RESOLVED (2026-07-31). **Severity:** production trading halted (single account, demo).
**Independent of** Customer Zero / Phase A/B / Golden / Beta.
**Primary references:** `docs/INCIDENT_ORPHANED_PLANNED_PLAN.md`, PR #247 (`04c6656`).

---

## Executive Summary

**What happened.** The production account #1 (`nuno.amaral@live.com`, IS6 demo `1302561`) stopped opening
Telegram-copied trades. Telegram signals kept arriving, parsing, and planning correctly, but every signal
was rejected at the planning **concurrency gate** with `concurrent_limit_exceeded`. The gate counts only
plans in `PLANNED` status (`count_active`), and promotion-rejected plans were being left in `PLANNED`
forever — so they accumulated as "orphans" and permanently consumed concurrency slots. Ten orphans
accumulated over two weeks; the tenth saturated the per-account/symbol cap of 10, hard-blocking all new
signals.

**Customer impact.** Account #1 (a **demo** account) did not execute six Telegram signals (messages 159–164)
during the hard-block window. No financial loss (demo; rejected signals placed no orders and created no
positions). No other account was affected. No data was lost or corrupted.

**Duration.** Last successful trade **2026-07-30 19:03 UTC**; hard concurrency block from **2026-07-31
02:06 UTC**; first restored trade **2026-07-31 10:16 UTC** — a no-execution window of ~15h (of which the
concurrency-specific hard lock was ~8h).

**Resolution summary.** Root-caused read-only; fixed via three additive parts (recovery command, source-level
lifecycle correction, self-healing + monitoring) shipped through full engineering governance (PR #247, 2
adversarial reviews, 2056 tests green); deployed to production (backend + listener) under Sponsor gates; the
ten orphans reclaimed `PLANNED→VOIDED` (`count_active` 10→0) with zero unintended mutations; and recovery was
**live-proven** — the next genuine signal executed end-to-end to a real MT5 order + Trade + protection.

---

## Timeline (UTC)

| When | Event |
|---|---|
| 2026-07-15 12:03 | **First orphaned plan** — #19 created PLANNED; promotion rejected `account_exposure_exceeded`; left PLANNED. |
| 2026-07-16 → 07-21 | **Accumulation** — plans #28–32 (07-16), #83–85 (07-21) rejected on `daily_drawdown_hit`, each left PLANNED (9 orphans; 1 slot still free, so trading continued). |
| 2026-07-30 (through day) | Account #1 realised **−$2049.20** (53 closed trades) vs the **$2000** daily-drawdown cap. |
| 2026-07-30 19:03 | **Last successful trade** — plan #143 promoted + executed. |
| 2026-07-30 23:03 | **Triggering event** — plan #144 created PLANNED; promotion rejected `daily_drawdown_hit` (correct); left PLANNED → **10th orphan → gate saturated (10/10)**. |
| 2026-07-31 02:06 | **First production impact** — msg 159 rejected `concurrent_limit_exceeded`; msgs 159–164 all rejected. |
| 2026-07-31 (AM) | **Investigation** (read-only, Sponsor-directed) — localised the stall to the planning concurrency gate; exonerated Customer Zero / ADR-0021 / Golden / Beta. |
| 2026-07-31 | **Implementation** — PR #247 merged `04c6656` (design + implementation adversarial reviews; 2056 backend tests green). |
| 2026-07-31 09:32 / 09:35 | **Deployment** — backend recreated (`27dd516e`→`d5461b63`); wayond-listener rebuilt + recreated (`1a5ef571`→`731528ab`). |
| 2026-07-31 ~09:42 | **Reclaim** — `--apply` VOIDed the 10 orphans; `count_active` 10→0. |
| 2026-07-31 10:16 | **First successful live trade** — signal 165 → plan 145 PROMOTED → MT5 orders 230672–230674 → Trades 419–421 → breakeven. |
| 2026-07-31 ~10:17 | **Incident RESOLVED** (recovery live-proven). |

---

## Root Cause

- **Triggering event.** Plan #144's promotion was rejected by the daily-drawdown risk gate on 2026-07-30
  23:03 — a **correct** risk action (the account had lost $2049.20 that day, exceeding the $2000 cap). This
  produced the tenth orphan and tipped the gate to its cap.
- **Root cause (the defect).** A promotion-rejected — or otherwise never-promoted — `SignalExecutionPlan` is
  left **permanently in `PLANNED` status**. There is no "rejected" plan status: on a `PromotionRejected` the
  `auto_router` recorded a durable deferral audit and a `PromotionAuditEvent(PROMOTION_REJECTED)` but **never
  transitioned `plan.status`** (`auto_router._plan_and_promote_one`). Because `count_active` counts exactly
  `status=PLANNED` (`execution/models.py:809-813`) and the planning gate rejects at
  `count_active >= PLAN_MAX_CONCURRENT_GROUPS` (=10, `signal_planning.py:242`), **each orphan permanently
  consumed one concurrency slot.**
- **Why the gate saturated.** Ten orphans accumulated (2026-07-15 → 07-30). With one free slot until the
  tenth, trading limped along; the tenth (plan #144) removed the last slot → every subsequent signal was
  rejected `concurrent_limit_exceeded`.
- **Contributing factors.** (1) The cap of 10 made the defect **latent for two weeks** — it needed ten
  independent risk-rejections on the same account+symbol to surface. (2) A **monitoring blind spot**: the
  stuck-promotion health check explicitly *excluded* PLANNED plans carrying a `PROMOTION_REJECTED` audit
  (treating them as "normal drawdown rejections") and only scanned a recent lookback window — so exactly the
  leaking rows were invisible. (3) No concurrency-utilisation metric or saturation alert existed.

**Not causes (ruled out with evidence):** Customer Zero, ADR-0021, the Golden promotion, the Beta Agent, the
Telegram listener, the bridge, and MT5 — the routing/gate/listener/worker/bridge code was unchanged since
before 2026-07-15, and the same-day Golden work is a separate Windows subsystem that post-dated the stall.
The daily-drawdown gate itself behaved correctly; the fault was purely in the lifecycle *around*
`PromotionRejected`.

---

## Detection

- **How it was found.** A Sponsor-directed read-only investigation traced the pipeline stage-by-stage and
  found signals were rejected at the planning concurrency gate; a DB query showed
  `count_active(1,XAUUSD) = 10 = cap` with ten stale `PLANNED` plans dating back to 2026-07-15.
- **Why monitoring missed it earlier.** The one health check that inspects PLANNED plans
  (`detect_stuck_promotions`) deliberately excluded PLANNED plans with a `PROMOTION_REJECTED` audit and only
  looked at a recent window — precisely the accumulating orphans. There was no metric for gate utilisation
  or a saturation alert, so a slow, silent build-up to the cap produced no signal until it hard-blocked.
- **How the new monitoring addresses the gap.** `detect_saturated_concurrency_gates` raises a deduped
  WARN/CRITICAL alert as a tradeable account+symbol approaches (≥80%) or hits (100%) the cap — proven live
  during this incident (it fired a CRITICAL "Concurrency gate SATURATED — acct 1 XAUUSD (10/10 PLANNED)").
  The ops summary now carries a `concurrency` block (per-account/symbol PLANNED utilisation + orphan-age
  distribution), and the monitor-chain log surfaces reclaim/saturation counters.

---

## Repair (PR #247, `04c6656`)

- **Part A — Recovery tooling.** `manage.py reclaim_orphaned_planned_plans` — dry-run by default; `--apply`
  compare-and-sets `PLANNED→VOIDED` with a `PLAN_VOIDED` audit; account/symbol/source scoping; enforces the
  reclaim age > `SIGNAL_MAX_AGE_SECONDS`. Places no order.
- **Part B — Lifecycle correction (root fix).** `auto_router._void_rejected_plan` transitions a
  promotion-rejected plan `PLANNED→VOIDED` at the source (guarded to `PromotionRejected` only, compare-and-set,
  fail-open). Prevents future accumulation with no flag and without touching existing rows.
- **Part C — Monitoring + self-healing.** A monitor-chain reaper (gated `ORPHANED_PLANNED_RECLAIM_ENABLED`,
  default OFF, so deployment cannot auto-reclaim the backlog before a reviewed dry-run) + the saturation
  alert + the ops metrics.
- **Refinements (from adversarial review):** W1 reclaim age must exceed `SIGNAL_MAX_AGE_SECONDS`;
  W2 `PromotionRejected`-only guard; W3 self-heal mandatory-but-gated; W4 documented behaviour change
  (voided plans leave the daily-cap count and the ops "accepted" bucket — a correct loosening); W5 age by
  `created_at`.

**Implementation/deployment evidence.** 2 adversarial reviews (design + implementation) = no blockers; 21
focused tests + 2056 backend tests green; CI green on `61caed0`; merged `04c6656`. Deployed by placing the 6
touched files (sha-verified) after confirming prod was exactly pre-#247; backend image `d5461b63`; listener
image `731528ab` (FROM the new backend). See `docs/INCIDENT_ORPHANED_PLANNED_PLAN.md`.

---

## Validation (recovery evidence)

- **Reclaim:** `RECLAIMED 10` — the exact set {19,28,29,30,31,32,83,84,85,144}, all `PLANNED→VOIDED`
  (`hold_reason=orphaned_planned_reclaim`); 10 `PLAN_VOIDED` audits.
- **Active-plan count:** `count_active(1,XAUUSD)` **10 → 0**.
- **No unintended mutation:** 0 ExecutionJobs/Trades created; VOIDED 3→13 (+10 exactly), CLOSED 119 unchanged,
  plan_total 132 unchanged; no plan outside acct#1/XAUUSD changed; no config changed. Dry-run was proven
  deterministic + non-mutating twice before apply.
- **First genuine Telegram signal (msg 165, 2026-07-31 10:16):** planned (plan 145) → **PROMOTED** (did not
  stay PLANNED, no concurrency leak) → 3 `PLACE_ORDER` jobs SUCCESS → **real MT5 orders 230672/230673/230674**
  → **Trades 419/420/421** → **breakeven protection engaged** (SL→entry, verified). Gate returned to 0/10.

---

## Preventive Improvements

- **Lifecycle correction (Part B)** — promotion-rejected plans no longer leak concurrency slots.
- **Recovery tooling (Part A)** — a safe, auditable, dry-run-first reclaim command for any future backlog.
- **Self-healing reaper (Part C)** — monitor-chain reclaim (flag-gated) as a backstop for any orphan from any
  cause.
- **Saturation alert** — proactive WARN/CRITICAL before a gate blocks trading.
- **Operational metrics** — concurrency utilisation, PLANNED-per-account, orphan-age distribution, reject
  reasons, reclaim counters.
- **Governance** — controlled-migration posture (deploy → dry-run → separate apply authorisation) and
  full-env-capture recreate for the out-of-Git listener.

---

## Lessons Learned

**Engineering.**
1. A gate that counts a non-terminal state must have a guaranteed exit from that state — a "rejected" outcome
   that leaves state in the counted bucket is a slot leak. Every risk/promotion rejection must transition the
   plan to a terminal status.
2. Latent, slow-accumulating defects (bounded by a cap of 10) can hide for weeks; capacity gates need a
   *utilisation* signal, not just a binary "blocked" symptom.
3. Monitoring that *excludes* the "expected" rejections can blind you to their accumulation — an exclusion is
   a coverage decision that must be justified against the failure mode it hides.

**Operational.**
1. The production listener runs as a bare `docker run` (out-of-Git launch path) with its Telegram session +
   tokens inline in the container env only — recreating it safely required full-env capture. Such paths must
   be regularised (RULE 8: launch paths outside Git are a standing hazard).
2. Read-only-first investigation + evidence-gated deployment (deploy → dry-run → separate apply) kept a live
   trading recovery safe and reversible at every step.

---

## Follow-up Items (separate engineering tasks — not implemented here)

1. **Regularise the production listener deployment.** The `guvfx-wayond-listener` container is created outside
   normal compose governance (bare `docker run`, no compose labels, inline-only secrets). Bring it under the
   standard compose deployment model (a service with an env-file), so future rebuilds do not depend on
   full-env capture. *Separate task; do not implement now.*
2. **Review `MULTI_ACCOUNT_ROUTING_ENABLED`.** It was already ON before this incident, preserved unchanged by
   the deployment, did not contribute to the incident, and currently routes only to account #1. Determine
   whether the enabled state is intentional and whether documentation should be updated. *Documentation +
   architecture review only; do not modify the flag.*

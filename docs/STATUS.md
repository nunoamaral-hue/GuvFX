# GuvFX — Project Status

> Update this file **whenever** project state changes. This is a current-state
> snapshot; deeper operational detail lives in `docs/RUNBOOK.md` and the handoff
> docs.

## Execution workstream log

- **2026-07-01 — EXEC-E2b-R2: shadow-only worker claim mode (repo-only, no order).**
  Closes the blocker found at E2b-DEPLOY-D2 preflight: with the R1 code a
  dedicated shadow worker (`MT5_SHADOW_WORKER=1`) still claimed the executable
  `PLACE_TEST_ORDER`/`PLACE_ORDER` types (unconditional in `claim_worker_job`),
  so run persistently it could win a real order and route it to the live
  `order_send` path (→ real demo ticket), failing the D2 no-order gates. R2 makes
  shadow mode **shadow-only**: `claim_worker_job()` now branches — flag ON returns
  `claim_next_job("PLACE_ORDER_SHADOW")` and nothing else (no executable claims,
  no default SYNC); flag OFF keeps the exact pre-E2b 3-claim sequence. A dedicated
  shadow worker therefore **structurally cannot** place an order, and its poll
  rate drops to 1 claim/loop (~30/min at the default 2s sleep — well under the
  100/min throttle, no special sleep config needed). Tests rewritten: shadow mode
  claims only `PLACE_ORDER_SHADOW` / never the executable types / never default
  sync / single claim per loop; normal mode unchanged + short-circuit. 140
  execution+signal_intake tests green on local Postgres. No bridge change, no new
  migration. Worker + tests + docs only — **NO deployment**. Unblocks a re-run of
  E2b-DEPLOY-D2 (persistent shadow worker).

- **2026-07-01 — EXEC-E2b-R1: env-gate shadow-worker polling (repo-only, no order).**
  Fixes the E2b polling regression found during the E2b-DEPLOY-D1 dry-run: the
  worker's unconditional 4th `claim_next_job("PLACE_ORDER_SHADOW")` pushed its
  poll rate (~120/min) over the 100/min request throttle and the live worker
  looped on HTTP 429. The shadow claim is now **opt-in** behind `MT5_SHADOW_WORKER`
  (default OFF): `mt5_trade_ingest_worker` extracts the claim sequence into
  `claim_worker_job()`, which makes the `PLACE_ORDER_SHADOW` claim ONLY when the
  flag is set. The normal worker keeps its exact pre-E2b 3-claim sequence
  (`PLACE_TEST_ORDER` → `PLACE_ORDER` → default SYNC), so its request rate is
  unchanged and below the throttle. The next_job endpoint still independently
  gates shadow jobs on `worker_permissions.shadow_worker`. 9 new poll-gate tests
  (flag default-off / on, sequence unchanged in both modes, shadow claim position,
  short-circuit, env-flag truthy/falsey parsing); the E2b order_check-only /
  order_send-0× guarantees are untouched. 126 execution tests green on local
  Postgres. No new migration. Worker + tests + docs only — **NO deployment**; the
  dedicated shadow worker (flag ON) is deployed only by a separate, gated action.

- **2026-07-01 — EXEC-E2b: shadow worker + bridge mt5.order_check() dry-run (no order).**
  First MT5 execution rung — proves the full pipeline shadow job → worker → bridge
  → MT5 validation while guaranteeing `mt5.order_send()` is NEVER called. Worker
  (`backend/mt5_trade_ingest_worker.py`) gains `handle_shadow_job` + `agent_order_check`
  and a `PLACE_ORDER_SHADOW` claim; for `execution_mode=SHADOW` it POSTs
  `/mt5/order_check` (never the live `/mt5/order`), completes SUCCESS storing the
  validation (retcode/margin/latency — no ticket/deal/order id) or FAILED; LIVE and
  unknown modes fail closed. Bridge (`scripts/mt5_signal_bridge.py`) gains
  `shadow_order_check` + `/mt5/order_check` route: same demo validation
  (is_demo/trade_mode/symbol/lots) and the EXACT SAME MT5 request as
  `execute_demo_order`, then `mt5.order_check(request)` — never `order_send`.
  `execute_demo_order` is byte-for-byte unchanged (additive only). 15 tests
  (mocked MT5): order_check called once / order_send **0×**, shadow request ==
  live request, live path still calls order_send, demo enforcement preserved,
  invalid symbol / market-closed / non-demo / tick fail safely, worker routes
  SHADOW→order_check never live, LIVE/unknown fail closed, SUCCESS stores
  validation no ticket. 132 execution+signal_intake+strategies + governance green
  on local Postgres. No new migration. Backend/scripts only — **NO deployment**;
  no shadow worker runs until a WorkerIdentity is granted `shadow_worker` on the
  production box (a separate, gated operational action). E3 (real demo placement)
  remains gated.

- **2026-06-30 — EXEC-E2a: plan → suppressed, un-claimable shadow jobs (no order).**
  First rung creating real `ExecutionJob` records (under the recorded D17 sign-off),
  but suppressed and un-claimable. `execution.signal_promotion.promote_plan_to_shadow_jobs`
  promotes a PLANNED `SignalExecutionPlan` into one `PLACE_ORDER_SHADOW` job per leg
  (`execution_mode=SHADOW`), linking `ProposedOrderLeg.execution_job`. No order, no
  MT5/`order_send`/`order_check`/agent/network call, no executable PLACE_ORDER job
  (AST guard). Three suppression layers: the SHADOW flag, no deployed consumer
  requests the type, and a new **`next_job` endpoint guard** that serves shadow
  jobs only to a `worker_permissions.shadow_worker` caller (none exists). Added
  `ExecutionControl.signal_execution_mode` (SHADOW gate), `PROMOTED` statuses,
  `PromotionAuditEvent`, idempotency, re-validated gates, operator command
  `promote_plan_to_shadow`. 20 new tests; 142 execution+signal_intake+admin_ops+
  strategies + governance green on local Postgres. Backend only; no worker/bridge
  change, no deployment, no production access/migration. E2b (shadow worker +
  bridge dry-run on the production MT5 box) remains separately deployment-gated.
  Detail: `backend/execution/SHADOW_PROMOTION.md`.

- **2026-06-30 — EXEC-E1b-R2: fail-closed robustness cleanup (no order).**
  From the PR #48 review: `_signal_timestamp` now makes naive parsed timestamps
  timezone-aware (falls back to the aware `created_at`), so a naive Telegram
  date no longer raises `TypeError` during the staleness check; `_hold`/`_void`
  gained the same `IntegrityError`→existing-plan idempotency fallback as the
  PLANNED path; and invalid/NaN/Inf total-lot values now become a clean `HELD`
  (`volume_split_invalid`) instead of crashing. 11 new tests; 95
  execution+signal_intake + governance green on local Postgres. No schema change.
  Backend only; no production access/deploy/migration. E1b's no-order guarantee
  preserved (asserted).

- **2026-06-30 — EXEC-E1b: non-executable multi-leg demo execution plan (no order).**
  Added `execution.SignalExecutionPlan` + `ProposedOrderLeg` (non-executable —
  NOT ExecutionJobs, invisible to the worker claim path), `SignalSourceConfig`
  (per-source `auto_demo_execution_enabled`, default OFF), and `PlanAuditEvent`
  (append-only). Planner `execution.signal_planning.plan_demo_execution` reads an
  APPROVED `PendingSignalApproval`, carries `take_profits` through, splits into up
  to 3 legs (shared SL, one TP/leg) with a deterministic capped volume split,
  holds on missing SL/TP, voids on stale signal, and rejects on
  kill-switch/source-disabled/non-demo/symbol/per-group-cap — creating **no
  ExecutionJob, no order, no listener** (per-group caps, source+message
  idempotency, full signal→plan→leg audit). 27 new tests (incl. static no-order
  AST guard, `ExecutionJob.objects.count()` unchanged, worker-invisible); 84
  execution+signal_intake + governance all green on local Postgres. Operator
  entry: `manage.py plan_demo_execution`. Detail:
  `backend/execution/DEMO_EXECUTION_PLAN.md`. Backend only; no production
  access/deploy/migration. The EXECUTING rungs (E2 suppressed → E3 real demo)
  remain behind Nuno's recorded sign-off per the D17 governance gate.

- **2026-06-30 — EXEC-HARDEN-JOBS-R2: worker-action gating + clean kill-switch handling.**
  Gated the worker-protocol actions `next`(claim)/`complete` on
  `ExecutionJobViewSet` to validated worker credentials (or staff) via a new
  `IsWorkerToken` permission — ordinary authenticated users can no longer claim or
  complete jobs (closes the pre-existing claim-hijack). Translated
  `ExecutionKillSwitchEngaged` to a clean 503 on the `run_signal` and admin-retry
  endpoints and to a labelled clean skip in the H1/M5 schedulers, replacing
  unhandled 500s (no order is ever placed — the model guard fails closed first).
  Fixed the misleading `views.py` comment. 9 new tests + execution/signal_intake/
  admin_ops/strategies (84) + governance all green on local Postgres. No schema
  change. Backend only; no production access/deploy/migration.

- **2026-06-30 — EXEC-HARDEN-JOBS: lock down generic ExecutionJob creation.**
  Disabled the generic DRF write surface on `ExecutionJobViewSet`
  (`POST/PUT/PATCH/DELETE` → 405) so an ordinary authenticated user can no longer
  create or mutate an order-bearing job directly (pre-existing gap surfaced in the
  E1a review). `ExecutionJob`s now come only from sanctioned gated paths
  (strategy automation, `OpenTradeJobView`, `CreateDemoTradeJobView`, admin_ops
  retry). Order-defining serializer fields made read-only. Functional kill switch
  enforced at the **model layer** (`ExecutionJob.save()` blocks order-opening job
  types when `ExecutionControl.kill_switch_engaged` / `GUVFX_EXECUTION_DISABLED`),
  covering every creation path; `OpenTradeJobView`/demo endpoints fail closed with
  503; `CLOSE_TRADE` exempt (flattening). Single source of truth
  `order_creation_kill_reason`. 13 new tests + E1a/exec/strategies/governance all
  green on local Postgres. Removed 184 untracked iCloud ` 2.` duplicate strays that
  were breaking the migration graph (none git-tracked; `.gitignore` already lists
  the pattern). Backend only; no production access/deploy/migration.

- **2026-06-29 — EXEC-E1a: approval → ProposedSignalOrder bridge (no order).**
  Added `execution.ProposedSignalOrder` (non-executable candidate — NOT an
  `ExecutionJob`, structurally invisible to the worker claim path),
  `execution.ExecutionControl` (functional DB kill switch + signal-specific
  disable, replacing the MVP 501 stub), and `execution.ProposalAuditEvent`
  (append-only). Bridge `execution.signal_proposals.propose_order_from_approval`
  creates proposals only — places no order, queues no job, contacts no broker.
  Gates: approved-only, kill switch / env kill switch, demo-only, symbol
  allowlist, lot/daily/concurrent caps, one-per-approval. `/api/execution/kill-all/`
  is now functional (engages the DB switch; release is admin-only). Operator
  entry: `manage.py propose_signal_order`. 35 tests green on local Postgres
  (incl. `ExecutionJob.objects.count()` unchanged + static no-order AST guard);
  E0 ADR-009 boundary guard still green. Branch
  `feat/wayond-exec-e1a-proposed-orders` off `origin/main` (`49d5026`). Backend
  only; no production access, no deployment, no migration against production.
  Detail: `backend/execution/SIGNAL_PROPOSALS.md`,
  `docs/SECURITY_EXECUTION_MODEL.md` §1.4/§1.4a.

## Snapshot

- Date: 2026-06-28 (UTC)
- Canonical branch: `main` @ `148437ae8bc651f6eb818e15bd9a16cf9d3a993f`
- **Authority:** Notion is the source of truth for the full programme lifecycle
  (latest *GuvFX — Current State v0.52*). This file is the Git-side mirror and
  must be kept consistent with it. For the live data-acquisition frontier see
  [`docs/PROGRAMME_STATE.md`](PROGRAMME_STATE.md).
- Current governance merge: `c17b7b8` — PR #31 *Add governance convergence
  foundation* merged into `main`. This introduced the scoped Claude rules,
  authority/packet boundaries, the secret scanner + governance Make/CI gate, the
  Notion map, the evidence convention, and the active-packet pointer.
- Documented production routes: `https://guvfx.com` (frontend),
  `https://api.guvfx.com` (backend API), `https://guac.guvfx.com/guacamole/`
  (Guacamole MT5 desktop). These are the routes recorded in `docs/RUNBOOK.md`;
  route availability and live production health were **not probed** by
  GFX-PKT-004A or its R1 remediation.
- Research/data foundation: PR #32 and PR #33 are merged to `main`
  (`80ef2f8`), establishing the DuckDB research foundation and the versioned
  market-data contracts (GFX-PKT-005B / R1 / R2).
- **Synthetic market-data foundation (GFX-PKT-006C arc) — COMPLETE & MERGED.**
  006C + R1 + R2 + R3 + R4 + R4-R1 + **R4-R2** are all merged to `main`; the final
  R4-R2 (UTC-instant constructor/evidence reconciliation) merged via **PR #36**, so
  `main` is at `148437ae`. This delivered strict contracts, immutable raw landing
  with SHA-256/idempotency/quarantine, the `VERIFIED` timezone gate, synthetic M1
  bid-OHLC publication, one arbitrary-length-safe/immutable/unhashable UTC-instant
  primitive, and ordinary-quarantine provenance. It is **synthetic-only** — no real
  data, NAS, broker, agent acquisition or deployment lives in this repository.
- **LIVE PROGRAMME FRONTIER — real market-data acquisition (006D).** The active
  frontier is **NOT in this repository**. It runs in the dedicated private repo
  `nunoamaral-hue/guvfx-windows-history-agent` (`main` `46c81057…`; A0/A1/A2/A2-P1
  merged) plus a ladder of governed read-only probes executed over SSH/Tailscale
  against the Windows VPS MT5 terminal. All probes to date have PASSED: package
  import (P0/P1), terminal lifecycle (P2), session-dependent runtime accepted
  (H0/H1/ADR-DATA-017), source identity (P3), and history retrieval (P4: 6 EURUSD
  M1 rows). **First durable raw object (P5) — DONE (2026-06-28):** S1 provisioned the
  approved `GuvFXData` store and the first real GuvFX market-data object + provenance
  manifest are now published and SHA-256-verified there (immutable, content-addressed,
  idempotent). The next real gate is **broker-server timezone verification** before any
  normalisation or broad backfill. Full map: [`docs/PROGRAMME_STATE.md`](PROGRAMME_STATE.md).
- **Capability (Notion Capability Registry, v0.52):** 1 of 10 domains GREEN
  (*Trading* — production, live order path exists today); the other 9 AMBER. The
  *Market Data & Research Platform* domain is the weakest and gates strategy quality.

## Verified current state

Facts supported by code, Git history, or CI in this repository:

- Monorepo with a Django + DRF backend (`backend/`) and a Next.js frontend
  (`frontend/`); see `docs/ARCHITECTURE.md`.
- Backend local apps registered in `backend/guvfx_backend/settings.py`
  (`INSTALLED_APPS`): `users`, `core`, `trading`, `strategies`, `backtests`,
  `analytics`, `ai_helper`, `execution`, `hosting`, `mt5`, `wims`,
  `intelligence`.
- GuvFX/WIMS producer–consumer boundary is implemented: `intelligence` packages
  inputs into transient envelopes and delivers them; `wims` consumes via
  `ConsumptionContract`. WIMS never imports `intelligence` (ADR-009 boundary,
  documented in `backend/intelligence/README.md` and `backend/wims/README.md`).
- Auth is cookie-based JWT (`users.auth_cookie.CookieJWTAuthentication`) with
  DRF default permission `IsAuthenticated`; `USE_TZ = True`, `TIME_ZONE = 'UTC'`.
- Governance/evidence layer is present on `main` as of `c17b7b8` (PR #31):
  `.claude/rules/`, `scripts/check_no_secrets.py`, `tests/test_no_secrets.py`,
  `evidence/`, `packets/`, `make governance-check`.

## Active feature work

- **Flow A (`flow-a-shadow` branch)** — a shadow, execution-suppressed signal
  pipeline (`backend/flow_a/`: `signal_intake`, `candidate`, `evaluation`,
  `quality_gate`, `suppression`, `pipeline`, `replay/`, and the
  `run_flow_a_shadow` management command). It runs in shadow mode only and is
  **not** merged into `main` and **not** promoted to paper or live trading.
  Treat it as research/validation work bounded by its own branch and governance
  path.

## Known gaps and blockers

Current, evidenced items only:

- **Storage provisioned; first real object stored (S1 + P5 done 2026-06-28).** The
  approved `GuvFXData` root is live (validated by `scripts/check_data_root.py`) and
  the first immutable raw object + manifest are published & SHA-verified there.
  Backups: Phase-1 NAS-local (RAID) per sponsor decision; offsite deferred.
- **Broker-server timezone is UNVERIFIED** for the demo source (TradersWay-Demo) —
  **this is now the active gate.** MT5 bar times are broker-server time, not
  guaranteed UTC; no offset may be hardcoded and no normalised dataset may be
  published, and no broad backfill started, until this is evidenced (a Red probe).
- **MT5 runtime is desktop-session dependent** (autologon/kiosk console) per
  ADR-DATA-017; a true headless/service-managed model is unproven and deferred.
- **Live Trading path governance gap:** the GREEN *Trading* domain runs a real
  order path today (Windows bridge), governed by the legacy programme; Blueprint
  doc 06 requires reconciling it with the target execution architecture before any
  execution-layer packet — not yet done. Safety reference (how to stop it now,
  single points of failure, recovery): [`docs/LIVE_TRADING_RISK_WATCH.md`](LIVE_TRADING_RISK_WATCH.md).
- Local `make check` cannot complete on a machine without a `backend/.venv` and a
  reachable PostgreSQL (`127.0.0.1:5432`); backend Django tests need a running
  PostgreSQL. GitHub Actions is the approved full-integration gate.
- MT5 mouse input via Guacamole has been observed to be unreliable (clicks
  intermittently drop while keyboard navigation works); see
  `docs/KNOWN_ISSUES.md`.

## Last known green checks

Kept distinct: historical local evidence vs. current governance CI evidence.

- **Historical (2025-12-15):** Backend GitHub Actions CI (Django tests) and
  Frontend GitHub Actions CI (lint + build) reported green, with `make check`
  green locally at that time.
- **Current governance CI (2026-06-23):** GitHub Actions push run for merge
  `c17b7b8` (PR #31) — jobs `governance`, `backend`, and `frontend` all
  succeeded.

## Production operations

Production runs behind Traefik with Let's Encrypt TLS on a VPS; the
GuvFX backend/frontend/Postgres stack and the Guacamole + MT5 desktop stack are
operated separately. Do **not** duplicate the full procedure here — see
`docs/RUNBOOK.md` (sections "VPS Production (GuvFX)" and "RUNBOOK — MT5 Free
Desktop") for the authoritative restart, verification, and handoff-mount steps.
This document does not assert live-trading readiness; promotion to paper or live
follows the governance decision path, not status notes.

## Owners

- PM: Nuno Amaral
- Active coder: Nuno (current) → Clive (next)

# STATUS — MT5 Free Desktop (XRDP + VNC)

**Overall status:** ✅ STABLE / OPERATIONAL  
**Last verified:** 2025-12-18 (UTC)

### What is running
- XRDP listening on `:3389`
- xrdp-sesman listening on `:3350`
- Xvfb + Openbox running on `DISPLAY=:99` (VNC fallback)
- XRDP Xorg session allocated dynamically (e.g. `DISPLAY=:10`)
- MetaTrader 5 (Wine) auto-starts inside XRDP session

### Verified checks
- `terminal64.exe` running under user `mt5free`
- `wmctrl` lists:
  - `MetaTrader 5 - Netting`
  - `Login` window
- Guacamole RDP connection shows MT5 UI

### Persistence
- Wine prefix persisted via Docker volume: `/home/mt5free/.wine`
- Autostart script persisted via bind mount: `/home/mt5free/bin/autostart-rdp.sh`

### Notable guarantees
- No manual MT5 launch required
- No XRDP password re-entry after container restart
- Safe to rebuild container without losing MT5 state

## Incident Log

### 2026-03-17 — Traefik Stale Backend Routing (API Auth Failure)

**Classification:** Operational issue (routing layer) — NOT an architectural failure.

**Issue:** Intermittent API authentication failure due to inconsistent backend routing.

**Symptoms:**
- Browser login failure ("Failed to fetch")
- Intermittent 502 Bad Gateway responses
- CORS preflight failures
- Inconsistent API responses across requests

**Root Cause:** Traefik routing table contained multiple backend targets — one valid container IP and one stale (dead) container IP from a previous deployment. Requests routed to the stale IP returned 502 errors, causing an auth failure cascade.

**Affected Component:** `api.guvfx.com` → `guvfx-backend` service (Traefik routing layer only).

**Resolution:**
```bash
docker compose down --remove-orphans
docker compose up -d
```
This removed stale containers, rebuilt the Docker network, refreshed Traefik service discovery, and eliminated invalid backend targets.

**Validation:**
- CSRF endpoint: 10/10 success
- OPTIONS login preflight: 10/10 success
- Browser login: confirmed working
- API responses: stable

**Architecture Impact:** NONE
**Infrastructure Impact:** NONE
**Status:** RESOLVED

## Documentation Governance Mapping

This repository uses the following canonical mapping:

| Governance Reference | Canonical File |
|---|---|
| `GUVFX_IMPLEMENTATION_LOG.md` | `docs/STATUS.md` |
| `GUVFX_PLATFORM_STATE.md` | `docs/STATUS.md` (shared responsibility) |
| `GUVFX_TERMINAL_FARM_RUNBOOK.md` | `docs/RUNBOOK.md` |
| Incident / edge-case tracking | `docs/KNOWN_ISSUES.md` |

All references to `GUVFX_*` documents map to these files. The `docs/` directory is the single source of truth. Do not create duplicate documents with alternate naming or introduce parallel canonical structures.

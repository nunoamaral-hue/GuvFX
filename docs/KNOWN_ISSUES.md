# Known Issues / Sharp Edges

List active problems with reproduction steps and workarounds.

## Worker claim priority is MODIFY > CLOSE > PLACE > SYNC (packet lists PLACE before CLOSE) (2026-07-16)

- The ingest worker's single prioritized claim order (running code) is
  `MODIFY_POSITION, CLOSE_TRADE, PLACE_ORDER, PLACE_TEST_ORDER, SYNC_POSITIONS`. The
  TI-SIGNAL-EXECUTION-VALIDATION packet text expected `MODIFY, PLACE, CLOSE, SYNC`. The two **hard
  invariants hold**: MODIFY is first (protection MODIFYs are never starved) and SYNC is last (bookkeeping
  never blocks execution). Only the middle CLOSE-vs-PLACE order differs. CLOSE-before-PLACE (exit an
  existing position before entering a new one) is arguably the *safer* default, so it was **left unchanged**
  rather than silently reordering a live execution path. **Flagged for Nuno's decision** — reorder to match
  the packet, or ratify the current (safer) order.

## Plan-layer silent gaps are now alerted (stuck_promotion) (2026-07-16)

- `detect_stuck_promotions` (minute-chain `sweep_execution_health`) raises a deduped WARN
  (`stuck_promotion:plan:{id}`) when an auto_demo-source plan reaches PLANNED but gets NEITHER a PLACE_ORDER
  job NOR a PROMOTION_REJECTED reason within 300s — the plan-layer complement to the approval-layer
  `unplanned_tradeable_signal` alert. Normal `daily_drawdown_hit` rejections carry a PROMOTION_REJECTED audit
  and are excluded (0 false-positives, verified live). Alert-only, fail-open, auto-resolving. Tunable via
  `STUCK_PROMOTION_ALERT_SECONDS`.

## "MT5 bridge stall" was a self-inflicted 429 throttle storm — FIXED (2026-07-16)

- The intermittent stalls were the ingest worker **rate-limiting itself**: 5 `jobs/next/` calls per
  loop (~150/min) > the backend `user` throttle (100/min) → HTTP 429 → `claim_next_job` raised → the
  blanket `except` left the claimed job RUNNING (false "orphaned: worker gone") + tight-retried.
- FIXED: `next_job` prioritized `job_types` CSV → one claim/loop (~30/min); 429 → exponential backoff;
  idempotent jobs completed FAILED on error (never orphaned); `worker_throttle_storm` alert.
- Residual (documented, not a defect): the worker is still **single-threaded**, so a genuinely *hung*
  (not errored) agent call blocks the next claim for up to the ~15s HTTP timeout. Evidence shows this
  is bounded and protection MODIFYs still get through; a dedicated SYNC lane was NOT warranted by the
  evidence. The `agent_get` snapshot timeout is 15s; the be_sync lease is 60s.

## Protection latency is now durably instrumented; broker floor is the irreducible part (2026-07-16)

- `execution/protection_latency.py` + `Trade.close_ingested_at` give per-plan/leg segment latencies
  (A–H) from durable data; `/operations.tp_protection` surfaces them + the broker soft-deferral floor
  + an SLA status. **Missing datapoints show UNKNOWN, never zero.**
- Two broker-anchored segments (A broker-close→ingestion, H broker-close→verified) depend on the
  **assumed +3h broker offset** (`BROKER_UTC_OFFSET_HOURS`, **unverified** until the timezone probe);
  the offset-independent ingestion→verified segment is authoritative regardless.
- The broker stops/freeze soft-deferral (`sl_within_stops_level`) is the **irreducible** latency floor
  (empirical TP2_LOCKED ~243 s); it is correctly soft-deferred + retried, never clamped, never counted
  as a hard failure. Reducible system latency (cadence + ingestion stall) is what the watcher + short
  lease address.
- **SOAK-IN-PROGRESS:** aggregate 24/48/72h latency accrues on natural trades — no trade forced.

## TP2_LOCKED is now broker-PROVEN; residual latency has two floors (2026-07-16)

- **First natural TP2_LOCKED broker proof:** plan 33 leg 3, job **#405** verified SL 4028.92→4025.30
  (the TP2 price); leg 3 then closed at 4025.30 (+$144.80). The ladder is correct.
- The **adaptive watcher** cuts the *cadence* floor (~1 min → ~1 s in-window) and the **short
  protection-sync lease** cuts the *ingestion-stall* floor (~6 min → ~1 min). The **broker
  stops/freeze band** (`sl_within_stops_level`) remains an **irreducible** floor — a TP2-price SL
  sits inside the band until price moves off it (was ~4 min for plan 33); this is soft-deferred and
  retried, never clamped/forced.
- Intermittent **MT5 bridge/SYNC stalls** (SYNC #392/#406 ran 358/359s; PLACE_ORDER #386 354s) are
  the underlying cause of the ingestion stall. The short lease + watcher reclaim BOUND the impact and
  a deduped alert (`protection_sync_stall`) now surfaces it, but the bridge-side hang itself is a
  separate item worth diagnosing if it recurs.
- The watcher is deployed **dark** first (`TP_WATCHER_ENABLED=0`) then armed; the minute monitor
  chain remains the fallback, so disarming loses nothing.

## Daily-drawdown circuit-breaker re-scaled to $2,000 (2026-07-16)

- `RISK_MAX_DAILY_DRAWDOWN_ABS` was unset in prod → the **$100 default**. At ti_signals' 1.20-lot
  sizing one stop-out ≈ $500, so the breaker halted the strategy after the first losing signal each
  UTC day (plans 28–31 on 07-16 were all `PROMOTION_REJECTED: daily_drawdown_hit` after plan 27
  realised −$502.80). **Nuno approved re-scaling to $2,000** (~4 losing signals). Set durably in
  `telegram.env` (backend/worker) AND `wayond-listener.env` (promotion runs in the listener). The
  breaker is unchanged — only the threshold. NOTE the drawdown "day" uses **UTC** midnight while
  `Trade.close_time` is broker-server time (~UTC+3, stored as-if-UTC); this ~3h skew is a known,
  separate item (does not cause false blocks — a real intra-day loss still counts).

## TP2-always-wins — the ladder was unproven in prod until now (2026-07-16)

- The reported "leg 3 at breakeven after TP2" (plan 24) actually ran on the **pre-#131 breakeven-only
  code** (jobs 179/180 have `stage=None`); the incremental ladder merged ~70 min later. The new
  ladder is now proven by deterministic tests and hardened with `_supersede_pending_breakeven` (a
  still-PENDING breakeven is retired the moment a leg locks TP2). Residual physical floor: if TP1 and
  TP2 close in **different** sync cycles and price reverses within that ~60s window, leg 3 can still
  close at a breakeven SL before the TP2 lock lands — inherent to a poll-based ladder; the bridge
  refuse-widen backstop guarantees the *steady-state* SL is always the TP2 lock. Broker evidence of a
  TP2_LOCKED modify is still **EVIDENCE-PENDING** (awaits the first natural eligible close).

## TP incremental protection — armed but broker evidence still EVIDENCE-PENDING (2026-07-16)

- The ladder (`INITIAL→BREAKEVEN→TP2_LOCKED`) is DEPLOYED + ARMED for ti_signals (BREAKEVEN_ENABLED=1,
  incremental_protection_enabled=True), Wayond unchanged. Only the 2 old MODIFY jobs exist (179/180
  from plan #24); **no new eligible plan has closed TP1 since deploy**, so the two headline broker
  proofs (TP1→remaining SL at entry; TP2→TP3 SL at the TP2 price) remain **EVIDENCE-PENDING**. Not
  forced. Auto-captured on the first natural eligible close.

## Orphaned RUNNING PLACE_ORDER is reconciled, not re-run (2026-07-16)

- A worker recycle can strand a `PLACE_ORDER` job `RUNNING` with an expired lease. It is **never
  re-enqueued** (a place-order is not idempotent — re-running risks a duplicate broker order).
  `execution_health.reconcile_orphaned_place_orders` reconciles it against the broker: leg Trade
  exists → mark the job SUCCESS with the ticket; no trade → a deduped WARN `orphaned_place_order:job:{id}`
  for an operator (verify on the broker and place manually if genuinely absent). Surfaced on
  `/operations` as `execution_jobs.place_order.orphaned_running`.

## Soak-cron log directory is root-owned (2026-07-16, worked around)

- `/var/log/guvfx` is `root:root 755`, so the ubuntu-user hourly crons can only append to
  **pre-created** ubuntu-owned log files, not create new ones. The soak cron silently produced no
  snapshots for hours because `soak_report.log` did not exist (its `>>` redirect failed before the
  command ran). Worked around by pre-creating the file (`sudo touch + chown ubuntu:ubuntu`). Any NEW
  cron that writes a new log file under `/var/log/guvfx` must have its log pre-created the same way
  (or the dir made group-writable).

## Deploy parity: rebuild the wayond-listener on planning/model/migration changes (2026-07-15)

- The **`guvfx-wayond-listener`** is a SEPARATE container/image (backend image **+ telethon**, built via
  `deploy/wayond-listener/Dockerfile --build-arg BACKEND_IMAGE=<backend image>`). It runs the
  **synchronous auto-routing + planning + leg-creation** on signal acquisition — so it executes
  `auto_router` + `signal_planning` + the `execution.models` ORM.
- **A migration that adds a NOT-NULL column WITHOUT a DB-level default breaks it** if the backend
  migrates before the listener image is rebuilt: Django drops the DB default after back-fill, so a
  listener running a model that predates the column INSERTs without it → NOT-NULL violation. This
  caused the TI non-execution incident. **Always** (a) give new NOT-NULL columns a DB-level default,
  AND (b) rebuild the listener whenever backend planning/routing/models change.
- **Gotcha:** tagging the plain backend image as `guvfx-wayond-listener:latest` CRASHES the listener
  (`CommandError: Telethon not installed`) — it needs the telethon layer. Build it via the listener
  Dockerfile. Recreate: `docker run -d --name guvfx-wayond-listener --network guvfx-prod_default
  --restart unless-stopped --env-file /home/ubuntu/guvfx-prod/wayond-listener.env
  guvfx-wayond-listener:latest python manage.py run_wayond_listener --live --health-file /tmp/wayond_health`.

## Two pre-existing CRITICAL alerts remain OPEN (reliability core dormant) (2026-07-15)

- `RECOVERY_CIRCUIT:global` (stale, first seen **2026-07-07**) and `EXECUTION_PIPELINE:0:0`
  (first seen **2026-07-15 14:29**, before the TI-non-execution deploy). Neither is caused by that
  deploy. They stay OPEN because the reliability-core supervisor is dormant
  (`RELIABILITY_CORE_ENABLED=false`) so nothing auto-resolves them. Operator (PM) action to
  ack/clear + decide on enabling the core.

## Provider trade-management commands — deployed DARK, arming is Nuno-gated (2026-07-15)

- WS-E (PR #128) is **deployed but inert**. Follow-up commands are RECORDED (`ProviderCommand` rows,
  always-on, harmless) but NOT acted on. **Arming = Red**: set `PROVIDER_COMMANDS_ENABLED=1` AND
  `SignalSourceConfig(source="ti_signals").command_engine_enabled=True`. It is a new execution path
  that can close/cancel live (demo) positions from parsed Telegram; MODIFY/CLOSE are by design NOT
  kill-switch-blocked (risk-reducing), so the ONLY stops are those two gates + the demo-only bridge.
  Arm only in a controlled window under Nuno, with Wayond untouched (source-isolated).
- Residual NIT (bridge-backstopped, not a live risk): a MOVE_SL_PRICE judged against `plan.stop_loss`
  can enqueue a widen vs an already-engine-moved SL; the bridge `would_increase_risk` re-read refuses
  it (job FAILs), so no risk reaches the broker — but the command may read APPLIED. Documented.
- Deliberate safe-direction loss: "cancel it" / "ignore it" no longer classify as CANCEL ("ignore" is
  bidirectional). Unambiguous "cancel/void/disregard <noun>" still cancels.

## Incremental TP-protection ladder — armed, natural broker evidence pending (2026-07-15)

- The auto-breakeven step is now the full **incremental TP-protection ladder**
  (`INITIAL → BREAKEVEN → TP2_LOCKED`, monotonic, per-source). It is ARMED
  (`BREAKEVEN_ENABLED=1`) and TP2-lock is enabled for `ti_signals` only
  (`SignalSourceConfig.incremental_protection_enabled=True`); **Wayond stays at state-1 breakeven**
  (flag OFF, behaviour unchanged). Broker evidence (a `MODIFY_POSITION` `result.verified_sl` for each
  of the BREAKEVEN and TP2_LOCKED stages) is captured automatically on the first natural eligible
  plan — NOT forced. Until a natural TP1/TP2 close occurs the two headline claims read
  EVIDENCE-PENDING.
- **Broker stops/freeze-band deferral is expected, not a failure.** A TP2-lock SL equals the TP2
  price, which sits at live market right after TP2 closes; the bridge returns a `retryable`
  `sl_within_stops_level` and the sweep DEFERS (re-enqueues, no page) until price moves toward TP3.
  These show as `deferred_today` (not `failed_today`) on `/operations`; `breakeven[deferred=…]` in the
  monitor-chain log.

## Accepted risk — protection in-flight guard is not atomic (F4, 2026-07-15)

- `breakeven._protection_inflight` is a check-then-create, and `idempotency_key` is not a DB
  constraint. Two *concurrent* sweeps could both enqueue a MODIFY for the same (ticket, stage).
  **Why it is accepted:** the monitor chain is single-flight (one cron, sequential steps, ~sub-second
  sweep), and the bridge is idempotent — a duplicate modify hits the eps "unchanged" no-op or the
  `would_increase_risk` refusal, so no double risk reaches the broker. If the chain is ever run
  concurrently, add a `unique` constraint or `select_for_update`.
- **Latent twin (not fixed — out of scope):** the CLOSE_TRADE benign-no-op completion in
  `mt5_trade_ingest_worker.py` uses the same `{"ok": True, …, **result}` spread order that clobbers
  `ok` (fixed for MODIFY in this packet). CLOSE_TRADE is the disabled provider-command path; the
  self-contradictory stored result is cosmetic there. Fix alongside the next provider-command change.

## Reliability core dormant + stale circuit breaker (2026-07-15)

- **`RELIABILITY_CORE_ENABLED=false` on prod** — the `reliability_tick` supervisor (continuous
  component-health evaluation, alert delivery, auto-recovery) is DORMANT. `ComponentHealth` /
  `operations-summary` are still aggregated on demand, and app code paths still create `AlertEvent`s
  directly (e.g. auto-breakeven failure, undelivered-WIN), but there is **no continuous health/alert
  tick**. Enabling it turns on automated recovery actions — a deliberate Amber/Red decision (Nuno/PM),
  not flipped in this packet.
- **Stale `Recovery circuit breaker tripped` alert (CRITICAL, dedup `RECOVERY_CIRCUIT:global`)** —
  open since ~2026-07-07 ("5 recovery actions in 900s exceeded threshold; auto-recovery suppressed
  pending manual reset"). While the reliability core is dormant this is functionally inert, but it is
  why orphaned jobs were never auto-reclaimed. Needs a deliberate operator **manual reset** (not done
  here — PM owns lifecycle).
- **Orphaned RUNNING SYNC on worker recreate** — `docker compose up --force-recreate` on the ingest
  worker orphans any in-flight `SYNC_POSITIONS` job (RUNNING, lease expires, never completes); with
  auto-recovery suppressed these accumulate and read as `EXECUTION_PIPELINE DEGRADED`. Harmless to
  function (SYNC is idempotent; new SYNCs run), but should be cleaned. **Workaround:** force-fail
  lease-expired RUNNING SYNC jobs (`status=FAILED`). Cleared 6 such orphans on 2026-07-15.
- **Broker health `MT5_BROKER`/`MT5_TERMINAL` = UNKNOWN** and `latest_trade_age_s` negative — the
  UNKNOWN follows from the dormant core (no terminal probe); the negative age is the known broker-
  server-timezone offset (bar/deal times are broker-server time, ~2h ahead — see data-acquisition tz).

## Auto-breakeven — broker evidence pending first TP1 close (2026-07-15)

- Auto-breakeven (WS-B) is DEPLOYED + ARMED (`BREAKEVEN_ENABLED=1`). The pipeline is verified at the
  endpoint level (`/mt5/modify-position` returns `missing_fields` for a token'd probe → endpoint live)
  and by unit tests, but the **broker-side SL-move evidence** (`MODIFY_POSITION` job `result.verified_sl`)
  is captured only on the FIRST natural TP1 close — not force-tested (no stray demo orders, no premature
  live-position SL change). To watch: `MODIFY_POSITION` jobs + legs with `breakeven_applied_at` set, and
  the `breakeven[…]` counters in the monitor-chain log.

## Execution worker — shadow polling throttle (2026-07-01)

- **Fixed (EXEC-E2b-R1): unconditional shadow poll tripped the request throttle.**
  As shipped in E2b, `mt5_trade_ingest_worker` claimed four job types per loop
  (`PLACE_TEST_ORDER`, `PLACE_ORDER`, `PLACE_ORDER_SHADOW`, default sync). At the
  ~2 s loop cadence the fourth (shadow) claim pushed the poll rate to ~120/min,
  over the 100/min `GuvFXUserRateThrottle`, so the live worker looped on HTTP 429.
  Observed during the E2b-DEPLOY-D1 dry-run; mitigated then by reverting the worker
  script. **Fix:** the `PLACE_ORDER_SHADOW` claim is now opt-in behind the
  `MT5_SHADOW_WORKER` env flag (default OFF), so the normal worker keeps its
  pre-E2b 3-claim sequence. The next_job endpoint still independently requires
  `worker_permissions.shadow_worker`. Deployment of the dedicated shadow worker
  remains a separate, gated operational action.
- **Fixed (EXEC-E2b-R2): dedicated shadow worker is now shadow-only.**
  E2b-DEPLOY-D2 preflight found that with R1 a shadow worker (flag ON) still made
  the unconditional `PLACE_TEST_ORDER`/`PLACE_ORDER` claims, so run persistently
  alongside the live worker it could win a real order and route it to the live
  `order_send` path (→ real demo ticket), failing the D2 no-order gates. R2 makes
  `claim_worker_job()` branch: flag ON claims **only** `PLACE_ORDER_SHADOW` (no
  executable claims, no default sync — 1 claim/loop), so a shadow worker
  structurally cannot place an order; flag OFF is unchanged. Unblocks a re-run of
  the D2 persistent-shadow-worker deployment.

## Execution observability (2026-07-01)

- **`shadow_queue_depth` metric runs a COUNT per shadow claim.** `next_job` emits a
  `ExecutionJob.objects.filter(status=PENDING, job_type=PLACE_ORDER_SHADOW).count()`
  on every `PLACE_ORDER_SHADOW` claim (one extra query per claim, outside the atomic
  claim). It is fail-open, observational only (never consulted for an execution
  decision), gated to shadow claims, and cheap on the small shadow queue — fine for
  the pilot. If many shadow workers poll concurrently at scale, consider sampling the
  metric or moving it off the hot claim path. Not a behaviour/order risk.

## Backend migrations / tests (2026-06-29)

- **Pre-existing migration drift in `research` + `strategies`.** `manage.py
  makemigrations --check --dry-run` reports unmade migrations for
  `research.researchobservation` (id / quality_buckets field alters) and
  `strategies` (StrategyRuntimeEvent/State index renames). This predates EXEC-E1a
  (the `execution` app is clean) and was observed, not introduced. Out of scope to
  fix here; flag for a dedicated `chore:` migration-reconciliation packet.
- **Execution-app tests require PostgreSQL.** The trading apps carry Postgres-only
  RunSQL migrations, so the WIMS SQLite shim cannot run them. To run
  `execution`/`signal_intake` tests locally, point at a local Postgres `dev`
  database (a throwaway local fixture — never a real credential). CI already runs
  the full suite on Postgres.

## Programme / data-acquisition (2026-06-27)

- **Storage not provisioned — data workstream blocked.** `GUVFX_DATA_ROOT` /
  `GuvFXData` does not exist on the controller; GFX-PKT-006D-A2-P5 correctly stops
  at its storage gate. Resolved only by owner action GFX-PKT-006D-S1 (NAS creds).
- **Broker-server timezone — summer offset VERIFIED (UTC+3), winter re-probe pending.**
  GFX-PKT-006D-TZ-PROBE (2026-07-01, read-only) evidenced TradersWay-Demo server time
  = **UTC+3** (EEST) via a fresh EURUSD M1 bar vs NTP-synced UTC — see
  `docs/evidence/broker_timezone_evidence_v1.md`. **DST-dependent:** this is the
  summer offset only; likely UTC+2 in winter. Re-probe after the EU DST transition
  (late Oct 2026) for a year-round mapping. Downstream must READ the recorded offset,
  never hardcode; no winter entry exists yet, so do not rely on the mapping across a
  DST boundary.
- **MT5 runtime is desktop-session dependent.** `initialize()` succeeds only with
  the autologon/kiosk console session present (H1 confirmed a single logoff is
  re-created by autologon within seconds). Headless/service-managed model unproven
  (ADR-DATA-017). A console logoff is a live-impacting action (it disrupts the MT5
  terminal + signal bridge until on-logon tasks restore them — R0 verified recovery).
- **Read-only MT5 boundary is design/test-enforced only.** `order_send`/`login`
  live in the same package surface as `copy_rates_range`; the prohibition currently
  rests on adapter design + tests, not a verified CI/network control (backlog item E).
- **Live Trading path not reconciled with the target architecture.** The GREEN
  *Trading* domain places real orders today; Blueprint doc 06 requires reconciling
  it before any execution-layer packet (backlog item G).

## Example
- **Tests fail: permission denied to create database**
  - Symptom: `permission denied to create database`
  - Likely cause: DB user lacks CREATE DATABASE privilege
  - Workaround: grant permission or configure tests to use an existing DB
  - Next step: document exact local DB roles and update RUNBOOK

- **`pyenv: python: command not found` when running `make check`**
  - Symptom: backend-test previously failed because `python` shim wasn’t configured.
  - Fix: Makefile now invokes `backend/.venv/bin/python` when available so manual activation isn’t needed.
  - Remaining requirement: ensure `backend/.venv` exists (e.g., `python -m venv backend/.venv` + install deps).

- **Backend tests blocked: PostgreSQL connection to 127.0.0.1:5432 is denied**
  - Symptom: `make check` fails during the Django test setup with `psycopg2.OperationalError: connection to server ... Operation not permitted`.
  - Likely cause: PostgreSQL server is not running locally or sandbox prevents TCP connections on 127.0.0.1:5432.
  - Workaround: Start PostgreSQL (or allow TCP access) so Django can hit the database before rerunning `make check`.
  - Next step: ensure the database is reachable and then rerun `make check`.

- **MT5 mouse unreliable via Guacamole**
  - Symptom: Mouse clicks inside the MT5 UI served at `https://guac.guvfx.com/guacamole/` sometimes stop working while keyboard controls remain usable; the pointer often wakes up after opening the File menu or reconnecting.
  - Reproduction: Launch Guacamole, start the MT5 desktop, wait for the Login dialog, and repeatedly click fields/buttons—mouse input intermittently freezes even though typing and tabbing succeed.
  - Workarounds tried: restarting `guacd`/Guacamole containers, reconnecting the Guacamole tunnel, reopening the Login dialog, and using keyboard navigation (`Tab`/`Enter`) when the mouse is dead.
  - Next steps: stream Guacamole and `guacd` logs during a failure, inspect x11vnc/VNC cursor mode or scaling options, monitor `mt5free-desktop` output, and consider switching VNC server flags or evaluating alternate remote protocols if hits persist.

- **MT5 Free Desktop quirks**
  - **MT5 Login popup appears on first run**
    - Cause: Broker credentials not yet saved.
    - Resolution: Log in once via XRDP and tick “Save password”.
  - **`Login failed for display 0` messages in logs**
    - Cause: XRDP initial handshake occurs before a proper Xorg display is allocated.
    - Impact: None as long as the session eventually starts.
  - **VNC (:99) does not show MT5**
    - Cause: MT5 intentionally bound to the XRDP display (e.g. `:10`); VNC is kept as a fallback desktop.
  - **Multiple Wine processes on rebuild**
    - Cause: Old wineserver instances still running during restart.
    - Resolution: `autostart-rdp.sh` calls `wineserver -k` to terminate stale processes.
- **MT5 mouse input unreliable via Guacamole**
  - Symptom: Mouse clicks inside the MT5 client (served via `https://guac.guvfx.com/guacamole/`) can stop responding even though keyboard navigation continues to work; clicks briefly resume after opening the File menu or restarting the Guacamole/`guacd` stack.
  - Reproduction: Open the Guacamole MT5 desktop, wait until the automation brings up the Login dialog, and try clicking fields/buttons—mouse focus sometimes disappears until a menu hotkey or toggle reactivates it.
  - Workarounds tried: restarting the `guacd`/Guacamole containers, recreating the `mt5free-desktop` service, resetting the VNC resolution and scaling, and relying on keyboard navigation to complete flows.
  - Next steps: stream Guacamole and `guacd` logs while the issue is happening, adjust x11vnc/VNC parameters (cursor mode, forcing hardware/software scaling), inspect window manager focus handling, and consider swapping to an alternate VNC/RDP backend if the current server cannot deliver reliable mouse events.

- **Resolved: pyenv `python` not found**
  - Status: Makefile runs backend tests via `backend/.venv/bin/python`, so `make check` works without activation.
  - Requirement: keep `backend/.venv` in place (see `docs/RUNBOOK.md` for setup steps).

- **`make check` backend tests**
  - Status: runs `backend/.venv/bin/python manage.py test`, so no manual activation is needed.
  - Remaining requirement: ensure `backend/.venv` exists (see `docs/RUNBOOK.md` for how to prepare it).

- **Resolved: Traefik stale backend routing causing intermittent 502 / auth failures (2026-03-17)**
  - Symptom: Intermittent 502 Bad Gateway from `api.guvfx.com`, browser login failure ("Failed to fetch"), CORS preflight failures — with no backend application errors.
  - Root cause: Traefik routing table retained a stale container IP alongside the valid one after container recreation. Requests randomly routed to the dead container.
  - Resolution: `docker compose down --remove-orphans && docker compose up -d` from `/home/ubuntu/guvfx-prod`.
  - Operational rule added to `docs/RUNBOOK.md`: if intermittent 502s occur with no backend errors, suspect stale Traefik routing and run the above command before investigating application-level issues.
  - Status: RESOLVED — no architecture or infrastructure changes required.

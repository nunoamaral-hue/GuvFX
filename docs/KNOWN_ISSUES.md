# Known Issues / Sharp Edges

List active problems with reproduction steps and workarounds.

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

# FX ingestion schedule (Bank of England XUDLUSS)

Restores **automatic** GBP→USD FX freshness for the portfolio dashboard. Before this, `fetch_fx_boe` /
`fetch_fx_daily` were UNSCHEDULED in production, so observations only advanced on ad-hoc operator runs and the
store went stale (the confirmed P1). This adds the tracked, supervised schedule following the existing
`deploy/*` cron convention — not an untracked ad-hoc entry.

## Files
- `run_fx_boe_ingest.sh` — daily ingestion wrapper: `flock` single-instance (no overlap), `timeout` per attempt,
  bounded retries with backoff, structured UTC logs; append-only + idempotent (writes missing revisions only via
  the dedicated `fx_ingest` identity); never updates/deletes an observation, never orders, never touches FRED.
  Emits `FX_INGEST_ALERT` + non-zero exit on final failure.
- `run_fx_freshness_check.sh` — read-only monitor over `fx_ingestion_health` (priority-aware BOE→FED_H10).
  Quiet `OK` when HEALTHY and fresh; `FX_FRESHNESS_ALERT` + non-zero when STALE/EMPTY, when the health command
  fails, or when `age_days >= WARN_AGE` (default 5 — fires *before* the hard 7-day staleness refusal removes GBP
  conversion). A stalled ingestion makes `age_days` grow, so this also catches "effective date not advancing".
- `crontab.fx-ingestion` — the schedule (documented): BoE ingest `0 15 * * *` UTC; freshness `0 16` and `0 8` UTC.
- `install_fx_ingestion_cron.sh` — idempotent installer (`--remove` to uninstall), end-anchored markers.
- `verify_fx_ingestion.sh` — validates cron presence, executables, the cron-user log redirection boundary, flock
  contention, and the monitor, without triggering an ingestion.
- `lib_fx_log.sh` — shared helpers (`fx_ensure_log`, `fx_check_appendable`) that create the log files as the cron
  user and test the real `>>` redirection boundary; sourced by the installer and verifier.
- `tests/test_log_boundary.sh` — standalone regression test for that boundary (positive + negative controls; no
  sudo/cron/network). Run: `deploy/fx-ingestion/tests/test_log_boundary.sh`.

## Timezone
Host and container are **UTC**. London = GMT (UTC) in winter, BST (UTC+1) in summer. BoE publishes XUDLUSS
~midday London (T+1); **15:00 UTC** is safely after the window in both GMT (15:00 London) and BST (16:00 London)
year-round.

## Log files & the cron-user write boundary
The cron runs as the unprivileged `ubuntu` user and redirects with `>> /var/log/guvfx/<name>.log`. `/var/log/guvfx`
is root-owned `0755`, so that user can *append* to a log it already owns but cannot *create* a missing one — and a
missing redirect target makes `/bin/sh` fail to even start the scheduled command (silently, with `MAILTO` unset).
That was the confirmed 2026-10-28 reliability defect. The installer therefore pre-creates `fetch_fx_boe.log` and
`fx_freshness.log` owned by the cron user at least-privilege `0640` and refuses to enable the schedule unless both
are appendable; `verify_fx_ingestion.sh` re-tests that exact boundary. It never broadens the directory and never
touches unrelated logs.

## FRED (Fed H.10) is intentionally NOT scheduled
FRED currently times out from the production container and BoE is the governed primary
(`PORTFOLIO_FX_SOURCE_PRIORITY=BOE,FED_H10`). Schedule FRED only after its egress timeout is separately resolved.

## Alert delivery (remaining, reviewed follow-on)
The monitor emits actionable `FX_INGEST_ALERT` / `FX_FRESHNESS_ALERT` log lines and non-zero exit codes now.
Delivering those to the existing ops sink (`AGENT_ALERT_SINK=telegram`) needs a small reviewed monitor command
reusing that delivery path — no generic ops-alert command exists yet. Until then the alerts are log/exit-code
signals for log-based ops monitoring.

## Install / verify (production, from the host repo checkout)
```
/home/ubuntu/guvfx-app/deploy/fx-ingestion/install_fx_ingestion_cron.sh
/home/ubuntu/guvfx-app/deploy/fx-ingestion/verify_fx_ingestion.sh
```
Enabling the schedule does not trigger an immediate ingestion; the controlled one-off run already brought the
store current (latest BoE effective date = the most recent published).

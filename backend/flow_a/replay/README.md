# flow_a/replay — SCE Replay Harness (Shadow)

Runs the **real, unmodified** SCE engine on **offline** OHLC and routes any
resulting signal into Flow A suppression. No live data, no Windows agent, no
8788 token, no execution.

## Why a harness (not a committed pipeline yet)

The SCE engine stack currently lives only on the `strange-ptolemy` branch. This
harness imports it from a worktree path (`SCE_BACKEND_PATH`) so SCE can be
exercised against fixtures **before** the engine is landed on the Flow A base.
Nothing here modifies SCE or Flow A.

## Components

- `gen_fixtures.py` — deterministic OHLC fixture generator (zig-zag uptrend with
  strict fractal pivots). Fixture data only — **not** market data, **not**
  strategy logic.
- `fixtures/EURUSD_{H1,H4}.json` — standard GuvFX bar format
  (`time, open, high, low, close, tick_volume`).
- `adapter.py` — `SignalResult → flow_a.OpenTradeCandidate` (field mapping only).
- `run_sce_replay.py` — the harness: loads fixtures → injects them by
  monkeypatching `strategies.signal_engine.fetch_rates` → stubs the DB-touching
  risk/audit calls → calls the unmodified `evaluate_sce` → routes any BUY/SELL
  into `flow_a.suppression` → proves no network egress and no ExecutionJob.
- `tests.py` — deterministic routing tests (SignalResult → candidate →
  suppression) that run under `flow_a._shadow_settings` without the engine stack.

## Run

```bash
cd backend
# Harness (needs the engine stack; defaults to the strange-ptolemy worktree path):
SCE_BACKEND_PATH=/path/to/strange-ptolemy/backend \
  ../.venv/bin/python flow_a/replay/run_sce_replay.py

# Routing tests (no engine stack required):
DJANGO_SETTINGS_MODULE=flow_a._shadow_settings ../.venv/bin/python manage.py test flow_a.replay
```

## Safety (Shadow Mode)

- Agent env vars are scrubbed; `fetch_rates` is monkeypatched to fixtures — the
  Windows agent is never contacted; the 8788 token is never used.
- A socket guard asserts **zero** network connections during evaluation.
- `evaluate_sce` is called **directly** (returns a `SignalResult`); the
  job-creating dispatcher is never invoked → **no ExecutionJob** is created.
- Candidates flow through `flow_a.suppression`, which structurally cannot create
  a pollable execution job. EA remains the sole live decider.

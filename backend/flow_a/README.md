# flow_a — Flow A Shadow Delivery (Build Phase 1)

Flow A packages a Wayond signal into a **suppressed** OPEN_TRADE candidate.
Execution is structurally impossible from this app: it produces a logged/audited
candidate and stops. **The EA remains the sole live decider.**

```
Wayond Signal
  → Strategy Evaluation
  → Trade Quality Gate v0.1 (Draft / Shadow / NOT live-approved)
  → OPEN_TRADE Candidate          (only when the gate ACCEPTs)
  → Execution Suppressed          (logged/audited; never dispatched)
```

## Shadow Mode constraints (enforced)

- **Execution suppressed / no live trading.** Flow A imports nothing from
  `execution` and never constructs an `ExecutionJob`. No pollable
  `PENDING OPEN_TRADE` job is created, so the MT5 worker (which polls
  `/api/execution/jobs/next/` for `PENDING` jobs) cannot pick the candidate up.
  Verified structurally in `tests.py`.
- **Not deployed.** `flow_a` is intentionally **absent** from production
  `INSTALLED_APPS`, has **no models, no migrations, and no URL routes**. It runs
  only under the isolated `flow_a._shadow_settings` shim (SQLite).
- **EA remains sole live decider.** Nothing here decides or places a live trade.

## Components (no persistence models — ADR-009)

- `signal_intake.py` — reuses the unchanged `intelligence` producer (Phase 7A)
  to validate + wrap a Wayond signal into an immutable envelope.
- `evaluation.py` — Strategy Evaluation layer. Evaluates the signal against a
  *given* strategy mapping. **No market-data ingestion, no Strategy Selection
  engine** (both out of scope).
- `quality_gate.py` — Trade Quality Gate v0.1: `ACCEPT / REJECT / ESCALATE`,
  **default-reject on uncertainty**. Thresholds are recommended defaults, not
  live-approved policy.
- `candidate.py` — builds the immutable `OpenTradeCandidate` (field names mirror
  `execution.services.create_open_trade_job`'s payload for future, separately
  authorised wiring — but it is *not* an `ExecutionJob`).
- `suppression.py` — the execution boundary: logs/audits the candidate, refuses
  any Django model instance.
- `pipeline.py` — orchestrates the five stages; returns an immutable
  `ShadowRunResult` with `execution_suppressed=True`, `execution_job_created=False`.

## ADR compliance

- **ADR-009** (GuvFX creates intelligence; producer persists no models). Flow A
  persists no models; dependency direction is `flow_a → intelligence` (one-way).
- **ADR-012** (Trading Availability Single Source Of Truth). `can_trade` /
  trading availability may come **only** from `/api/reliability/trading-health/`.
  Flow A never recreates, derives, duplicates, or reinterprets it. In Phase 1
  shadow, availability is **not consulted** (`availability=None`). If an
  availability check is requested without an authoritative SSOT result, the gate
  **escalates** (`FlowAEscalation`) rather than inventing `can_trade`. The SSOT
  endpoint does not yet exist in this repository; availability gating is
  therefore deliberately not wired (see the build report's Open Questions).

## Demonstrate / test (isolated SQLite shim — production DB untouched)

```bash
cd backend
DJANGO_SETTINGS_MODULE=flow_a._shadow_settings python manage.py migrate
DJANGO_SETTINGS_MODULE=flow_a._shadow_settings python manage.py run_flow_a_shadow
DJANGO_SETTINGS_MODULE=flow_a._shadow_settings python manage.py test flow_a
```

`run_flow_a_shadow` reads a Wayond signal (default
`intelligence/fixtures/wayond_signal_sample.json`) and a strategy config
(default `flow_a/fixtures/strategy_sample.json`); override with
`--signal/--strategy` (inline JSON) or `--signal-fixture/--strategy-fixture`.

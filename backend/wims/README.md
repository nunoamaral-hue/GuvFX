# WIMS — Educational Content Flow (WP-1)

First operational WIMS MVP workflow. Logically separate Django app inside the
GuvFX backend (ADR-009 boundary): **WIMS owns Context, Content, Human Review and
Publishing.** No trading, MT5 or signal logic lives here.

## Pipelines

WP-1 (topic-sourced):
```
Educational Topic → Context → Content → Human Review → Publish
```

WP-2 (signal-sourced — Wayond Signal Flow):
```
Wayond Signal (external, not persisted)
  → Consumption Contract → Context → Content → Human Review → Publish
```

WP-3 (trade-result-sourced — Trade Result Flow):
```
Trade Result (external, not persisted)
  → Consumption Contract (source_type=TRADE_RESULT) → Context → Content
    → Human Review → Publish
```

All feed the **identical** Content → Review → Publish flow; WP-2/3 only extend
the *source* of a Context (a topic **or** a consumption contract). WP-3 adds no
new model/service/workflow — it adds a `TRADE_RESULT` source type to
`ConsumptionContract` with extra descriptive fields (`exit_price`,
`result_type`, `profit_loss`, `pips`, `close_time`, `commentary`, `tags`) and
reuses everything else. Every state-changing step writes an immutable
`AuditEvent` row in the same DB transaction, so the workflow is fully
reconstructable.

### Boundary (ADR-009)

WIMS **may persist**: Consumption Contracts, Context, Content, Review, Publish,
Audit. WIMS **must not persist**: Signals, Trades, Positions, Deals,
MT5/Broker/Execution objects. The `ConsumptionContract` records *received
intelligence* (descriptive metadata incl. symbol/direction/prices and, for
WP-3, trade outcome) — it is **not** a Signal/Trade/Position/Deal/Execution
object, WIMS never acts on it, and the WP-2/WP-3 demos + tests assert no
prohibited model types exist.

## Models (deliverables 1–7)

| Model              | Role                                   | Statuses |
| ------------------ | -------------------------------------- | -------- |
| `ConsumptionContract` | WP-2/3 — received intelligence (first WIMS-side object; `source_type` WAYOND/MANUAL/TRADE_RESULT) | RECEIVED / PROCESSED / ARCHIVED |
| `EducationalTopic` | D1 — source topic                      | DRAFT / ACTIVE / ARCHIVED |
| `Context`          | D2 — educational context from a topic  | DRAFT / READY_FOR_CONTENT / ARCHIVED |
| `Content`          | D3 — audience-facing content           | DRAFT / READY_FOR_REVIEW / APPROVED / REJECTED / PUBLISHED |
| `Review`           | D5 — mandatory human review record     | decision: APPROVE / REJECT |
| `Publish`          | D6 — manual publish record (simulated) | channel: TELEGRAM / X |
| `AuditEvent`       | D7 — append-only audit trail           | — |

`WorkflowState` (D4) is the operator-facing view derived in
`services.workflow_state_for_topic`: `AWAITING_CONTEXT`, `AWAITING_CONTENT`,
`AWAITING_REVIEW`, `AWAITING_PUBLISH`, `PUBLISHED`, `ARCHIVED`.

All transitions go through `wims/services.py` (enforced status rules + audit),
used uniformly by the admin actions and the DRF API.

## API

Mounted at `/api/wims/` (auth required):

- `topics/`, `contracts/`, `contexts/`, `contents/`, `audit/` (read-only)
- `POST contracts/` — manually create a Consumption Contract; accepts WP-3
  trade-result fields when `source_type=TRADE_RESULT` (WP-2 D5 / WP-3)
- `POST contracts/{id}/generate-context/` — body `{ "context_text": "..." }` (WP-2 D2)
- `POST contents/{id}/submit-for-review/`
- `POST contents/{id}/review/` — body `{ "decision": "APPROVE|REJECT", "notes": "" }`
- `POST contents/{id}/publish/` — body `{ "channel": "TELEGRAM|X" }`

## Admin

`/admin/` registers all models. `Content` has actions: submit-for-review,
approve, reject, publish→Telegram (simulated). `AuditEvent` is read-only.

## Demonstrate / test

The MVP demo and tests run against an **isolated SQLite database** so they need
no Postgres role (`wims/_demo_settings.py`, not used by the running app):

```bash
cd backend
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py migrate
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py wims_demo
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py test wims
```

`wims_demo` runs the WP-1 "What Is Risk Management?" flow; `wp2_demo` runs the
WP-2 "BUY XAUUSD" Wayond Signal Flow; `wp3_demo` runs the WP-3 "EURUSD WIN"
Trade Result Flow (reads a transient JSON payload — default fixture
`wims/fixtures/trade_result_eurusd_win.json`, or `--fixture` / `--payload`).
All print six evidence records and end in `PASS`. Flags (all): `--decision
REJECT` exercises the reject path, `--actor <user>` attributes to an existing
user, `--channel X` publishes to X.

```bash
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py wp2_demo
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py wp3_demo
```

In deployment, run the same command with the normal settings against Postgres
(it only uses the ORM):

```bash
python manage.py migrate wims
python manage.py wims_demo
```

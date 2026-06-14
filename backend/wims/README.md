# WIMS — Educational Content Flow (WP-1)

First operational WIMS MVP workflow. Logically separate Django app inside the
GuvFX backend (ADR-009 boundary): **WIMS owns Context, Content, Human Review and
Publishing.** No trading, MT5 or signal logic lives here.

## Pipeline

```
Educational Topic → Context → Content → Human Review → Publish
```

Every state-changing step writes an immutable `AuditEvent` row in the same DB
transaction, so the workflow is fully reconstructable.

## Models (deliverables 1–7)

| Model              | Role                                   | Statuses |
| ------------------ | -------------------------------------- | -------- |
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

- `topics/`, `contexts/`, `contents/`, `audit/` (read-only)
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

`wims_demo` runs "What Is Risk Management?" end-to-end and prints the six
evidence records (source, context, content, review, publish, audit). Flags:
`--decision REJECT` exercises the reject path; `--actor <user>` attributes to an
existing user; `--channel X` publishes to X.

In deployment, run the same command with the normal settings against Postgres
(it only uses the ORM):

```bash
python manage.py migrate wims
python manage.py wims_demo
```

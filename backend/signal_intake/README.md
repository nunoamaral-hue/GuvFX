# signal_intake — Wayond signal → PendingSignalApproval (EXEC-E0, SHADOW)

GuvFX-side, execution-adjacent **shadow** intake: turns a parsed Wayond Telegram
signal into a human-reviewable `PendingSignalApproval`. It is the execution-side
entry point for the Telegram signal, kept entirely separate from the WIMS content
path (ADR-009: a WIMS `ConsumptionContract` must never trigger an order).

## E0 scope — no order authority

- **No `ExecutionJob` is ever created here.** This app does not import `execution`
  (enforced by an AST + regex CI guard in `tests.py`).
- **Approving/rejecting changes status and writes an audit row — nothing more.**
- **No live Telegram listener** — input is file/fixture-based only.
- The signal → order bridge (E1+) is a separate, escalating, sponsor-gated packet.

## Two decoupled flows from one parser

```
Wayond Telegram ─┬─▶ CONTENT  : intelligence → WIMS ConsumptionContract → review → publish   [deployed]
                 └─▶ EXECUTION: signal_intake → PendingSignalApproval → human review          [this app, shadow]
```

Both reuse the deployed `intelligence.telegram_source` parser, but the flows never
cross: this app never touches the `ConsumptionContract`, and `wims`/`intelligence`
never import `execution`.

## Models

- `PendingSignalApproval` — source, `message_id` (dedup key, unique with source),
  symbol/direction/entry/SL/TP, raw payload, status
  (PENDING_APPROVAL/APPROVED/REJECTED/EXPIRED/QUARANTINED), reviewer/reviewed_at/notes.
- `SignalAuditEvent` — append-only: SIGNAL_RECEIVED / QUARANTINED / APPROVED /
  REJECTED / APPROVAL_DENIED.

## Services / command

- `services.intake_parsed` / `intake_message` — idempotent on `(source, message_id)`;
  unparseable → quarantined; creates no order.
- `services.approve` / `reject` — status + audit only. **E3-APPROVAL-RBAC:** both
  require the dedicated `signal_intake.review_signals` permission (fail-closed —
  plain staff/admin access is NOT sufficient; superusers qualify). A refused
  attempt raises `ReviewPermissionDenied` and writes a persisted APPROVAL_DENIED
  audit; the admin approve/reject actions are hidden from unauthorised staff.
- `manage.py grant_signal_reviewer <username_or_email> [--revoke]` — idempotent
  grant/revoke of the reviewer permission (operator entry point).
- `manage.py ingest_wayond_signals_for_approval [--file export.json]` — shadow batch
  intake (default fixture `signal_intake/fixtures/wayond_signals_sample.json`).

## Demonstrate / test

```bash
cd backend
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py migrate
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py ingest_wayond_signals_for_approval
DJANGO_SETTINGS_MODULE=wims._demo_settings python manage.py test signal_intake
```

## Next (gated)

E1: approval → `PLACE_ORDER` `ExecutionJob` on a **demo** account with the worker
suppressed (logs intended order, places none), behind the kill switch + demo/live
enforcement + risk/lot/symbol limits. E2 demo paper, E3 live — sponsor-gated.

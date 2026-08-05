# Broker-validation observability — support-grade timeline (WS-D)

**Goal:** no SSH should be required to understand a validation failure. Given a correlation id, a staff/admin
user can see WHERE in the pipeline a validation stopped, WHY, and roughly WHEN — without touching the Windows
host. Operator/admin only; **no customer exposure**. 2026-08-05.

## What is implemented (this PR)

- **Builder** — `backend/trading/validation_timeline.py:build_timeline(correlation_id)`. Read-only; **never
  raises**; assembles the timeline from durable records the backend already holds — it does **not** instrument
  the validation hot path and requires **no host access**:
  - the `BrokerAccountValidationAttempt` (authoritative terminal record: status / reason_code / is_demo /
    server / masked login / trigger / created_at),
  - the `CREDENTIAL_ACCESSED` `AuditEvent` for that account at/before the attempt (the real decrypt start
    marker),
  - the canonical 10-stage pipeline, with each stage's ✓ / ✕ / not-reached **derived** from the outcome
    `reason_code` (the WS-A classifier already localises the failure, so the reason tells us the furthest
    stage reached — a broker rejection means the broker was reached; `validation_ipc_unavailable` means the
    local IPC never came up and the broker was never reached; the terminal `persisted` / `browser_response`
    stages always complete because the result is recorded and returned regardless of outcome).
- **Staff API** — `GET /api/trading/validation-timeline/?correlation_id=<id>` **or** `?account_id=<id>` (latest
  attempt for the account) **or** `?attempt_id=<id>` (`ValidationTimelineView`, `IsAdminUser`;
  `resolve_correlation_id` resolves any of the three to a correlation id). Dark (404) whenever the
  broker-connectivity surface is dark; **400 only when NONE of the three is supplied**. Returns
  `{correlation_id, found, attempt_id, account_id, status, reason_code, is_demo, server, login_masked, trigger,
  started_at, finished_at, duration_ms, stages:[{key,operator_label,customer_label,state,reason}],
  customer_summary, operator_summary}`.
- **Secret-safe by construction** — only allow-listed, already-masked fields (masked login, non-secret server,
  reason code) are returned; never a password / ciphertext / host path / session id / pid. The customer
  summary never claims a broker outage for a host/agent failure and never leaks IPC / Session 0 / MT5 / error
  codes. Tests: `backend/trading/tests_validation_timeline.py` (builder derivation for success / host-IPC /
  broker-rejection; staff-only + dark + missing-arg + secret-safety).

## The 10-stage pipeline

`api_received → credential_decrypted → envelope_sealed → request_signed → agent_received → mt5_launched →
broker_login → broker_response → persisted → browser_response`

Each stage carries an **operator label** (precise) and a **customer-safe label** (generic), an outcome state
(`ok` / `failed` / `not_reached`), and — on the failing stage — the reason code. Example for the #13 failure
(`validation_ipc_unavailable`): stages `api_received…agent_received` = ok, **`mt5_launched` = failed**,
`broker_login`/`broker_response` = not_reached, `persisted`/`browser_response` = ok; `operator_summary` names
the furthest stage + the reason; `customer_summary` = *"couldn't start the secure broker-validation session;
your details weren't rejected — try again later."*

## Fidelity boundary (honest)

Real per-stage **timestamps/durations** exist for the coarse markers the backend owns (decrypt start →
attempt persisted). The **fine-grained agent-internal timings** (terminal-launch → GUI → IPC → broker-TCP →
authorisation) live only in the agent's on-host diagnostic artefact
(`…\validation-diagnostics\<corr>.diag.json`). Surfacing those per-stage timings without SSH requires the
**agent to forward its already-computed, sanitised operator summary** in the VALIDATE_LOGIN response — i.e.
add `stage_reached` / `first_failing_stage` / `last_error_code`-class fields to the agent's response
allow-list (`lib.mgmt_agent_core._sanitise`) and have the backend persist them. That is an **additive agent +
host change (gated, not in this packet's scope — "no host modification")**; the builder here is designed to
enrich automatically once those fields are present. Until then the derived stages + the WS-A reason code
already localise a failure to the correct region (host/agent vs broker vs credential) **without SSH**.

## Staff timeline UI (Phase-3 — BUILT)

**Operations → Validation Timeline** (`src/app/(app)/admin/operations/validation-timeline/page.tsx`,
`useAdminRole`-gated) — searchable by **Correlation ID / Account ID / Validation Attempt ID** (the backend
`resolve_correlation_id` resolves any of the three; the endpoint accepts all three query params). It renders
`ValidationTimelinePanel`: the stage rail with ✓ / ✕ / ○ per stage, the customer-safe label + operator label +
reason, the customer-safe summary and the operator summary, plus the correlation / attempt / account / server /
masked-login / duration header. No customer exposure; the whole route is admin-gated and the backend enforces
`IsAdminUser` + darkness.

The customer-facing **validation history** table (`ValidationHistoryTable`) is redesigned for support: status
icon, time, concise outcome, customer-safe summary — with a **correlation-id column shown ONLY to staff**
(`staff` prop). Customers never see the correlation id or any raw reason code / operator diagnostic.

**WS-B fidelity note:** the builder also corroborates the persisted marker with the committed VALIDATION
`OperationalEvent` (WP5.2 projection) for the correlation id — read-only, no host access. Fine-grained
agent-internal per-stage timings still require the (gated) agent-forward change described above.

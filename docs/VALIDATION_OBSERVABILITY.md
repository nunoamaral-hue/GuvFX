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

## Per-stage fidelity audit (Phase-4 WS-B — derived vs directly evidenced)

Be explicit about which stages are **directly evidenced** by a durable record and which are **derived** from
the outcome `reason_code`. Almost all per-stage ✓/✕ are **derived** (a dictionary lookup on the classifier's
reason via `_REASON_FURTHEST_OK`), NOT independently observed — so the timeline is exactly as honest as the
reason code, and no more. This is by design (no host access, no hot-path instrumentation), and it localises a
failure to the correct **region**; it does not pinpoint a node.

| Stage | Evidence source | Derived or direct? | Confidence |
|---|---|---|---|
| api_received | The attempt row exists (build only runs if it does) → the request WAS processed. State (ok/failed) derived from reason. | **Derived** (existence weakly corroborates) | Medium |
| credential_decrypted | `CREDENTIAL_ACCESSED` `AuditEvent` — a **direct** decrypt record — but used only for `started_at`/duration, not to set the stage state. | **Derived** state; **direct** timing | Medium–High |
| envelope_sealed | none | **Derived** | Medium |
| request_signed | none | **Derived** | Medium |
| agent_received | none (transport-ambiguous reasons stop here conservatively) | **Derived** | Medium |
| mt5_launched | none | **Derived** | Medium |
| broker_login | none | **Derived** | Medium |
| broker_response | none | **Derived** | Medium |
| persisted | The `BrokerAccountValidationAttempt` row **is** the persistence; corroborated by the committed WP5.2 VALIDATION `OperationalEvent`. | **Direct** | High |
| browser_response | The backend **returned** the response; there is **no** signal the browser rendered it (a client that dropped after persist never saw it). | **Assumed ok** (weakest) | Low — see label wording |

Consequences honestly stated: (a) `browser_response` is always ✓ and its customer label is deliberately
"Returned the result to you" (what the backend can evidence), **not** "Showed you the result" (which it
cannot). (b) Several distinct host/agent reasons (`validation_ipc_unavailable`, `validation_busy`,
`mt5_unavailable`, `runtime_unavailable`, `isolation_check_failed`, `could_not_verify`) all localise to the
same `mt5_launched` region — the **reason code shown on the failing stage** is what distinguishes them, not
the rail position.

## Known limitation — historical (pre-WS-A) attempts (Phase-4 S6)

The per-stage state is derived from the **persisted** `reason_code`, and `reason_code` is immutable raw
evidence (`.claude/rules/data.md` — never rewritten in place). Attempts recorded **before** the WS-A
classifier fix persisted `server_unavailable` for what were actually **local Session-0 IPC** failures
(`broker_tcp_observed=false`, e.g. #12/#13). For those historical rows the timeline will faithfully render
`server_unavailable`'s mapping — "Contacted your broker" ✓ / "Your broker responded" ✕ — which is **wrong for
that attempt** but is an accurate rendering of a now-known-defective stored reason. This is a **data
artefact, not a code defect**, and must NOT be "fixed" by rewriting immutable reason codes. **Going forward**
the agent emits `validation_ipc_unavailable` for local IPC failures, which the timeline renders correctly
(host/agent region, broker not reached). When triaging a **pre-2026-08-05** attempt whose reason is
`server_unavailable`, cross-check the agent diagnostic artefact's `broker_tcp_observed` before believing the
"broker reached" rail.

# Validation Failure Triage — locus diagnosis without SSH (Phase 4, WS-D)

**Goal.** Given ANY broker-validation failure and only a **Correlation ID / Account ID / Validation Attempt
ID**, a support engineer determines **WHERE** in the pipeline it stopped —

```
Browser → Backend → Agent → MT5 (local IPC) → Broker → Persistence → Customer result
```

— **without SSH to the Windows host**. This complements the situation-based
[support-playbook.md](support-playbook.md): the playbook answers "what do I tell the customer for situation
X"; this doc answers "where did it break". Read-only; the Validation Timeline is staff/admin-gated.

## The tool

**Operations → Validation Timeline** (`/admin/operations/validation-timeline`, staff only). Search by
Correlation / Account (latest attempt) / Attempt ID. It renders the 10-stage rail with, per stage, ✓ done /
✕ failed / ○ not-reached, the customer-safe label, the operator label, and — on the failing stage — the
reason code. Plus the customer summary, the operator summary, server, masked login, and duration. Backend:
`ValidationTimelineView` (`IsAdminUser`; 404 when the surface is dark).

## Step 1 — open the timeline

Search by whichever id you have. If **not found**, the id is wrong, or the broker-connectivity surface is
dark for this environment (the endpoint 404s) — confirm the flag state, not a broker fault.

## Step 2 — read the failing stage → locus

The **first ✕ stage** localises the failure. The reason code confirms it. Map:

| First ✕ stage | Locus | Meaning | Representative reasons |
|---|---|---|---|
| `api_received` | **Backend (pre-flight)** | The request could not even be started — no broker server on record. | `broker_server_missing` |
| `credential_decrypted` | **Backend** | No saved credential to prepare. | `credential_missing` |
| `envelope_sealed` | **Backend (config)** | Signing/sealing not provisioned — the request was never signed or sent. | `validation_unconfigured` |
| `request_signed` | **Backend↔Agent transport** | A black-box transport outcome that cannot confirm the agent was reached. | `login_timeout`, `bridge_unavailable`, `validation_runner_unavailable`, `validation_runner_timeout` |
| `mt5_launched` | **Agent / MT5 local IPC (validation HOST)** | The agent WAS reached; the local MT5 Python↔terminal IPC never came up (or the agent's delegator was busy). **The broker was NEVER contacted.** | `validation_ipc_unavailable`, `validation_busy`, `credential_unsealable`, `mt5_unavailable`, `runtime_unavailable`, `isolation_check_failed`, `validation_baseline_dirty`, `could_not_verify` |
| `broker_response` | **Broker** | The broker WAS reached and returned a verdict/condition. | `invalid_password`, `invalid_login`, `account_disabled`, `server_not_found`, `classification_mismatch`, `server_unavailable` |
| *(no ✕; all validation stages ✓ but result not healthy)* | **Post-probe (Agent)** | The login ran; a step AFTER the broker response failed. | `diagnostic_capture_failed`, `credential_scrub_unverified` |
| *(all ✓, healthy)* | **Success** | Connected and classified. | `demo_ok`, `live_detected` |

`persisted` and `browser_response` are **always ✓** — the outcome is recorded and returned regardless of the
login result. If a customer says "I saw nothing", that is a browser/network issue on their side, not a
pipeline stage (the timeline proves the result was persisted).

## Step 3 — the critical distinction (the defect this programme fixed)

`mt5_launched` ✕ vs `broker_response` ✕ is the distinction that matters most:

- **`mt5_launched` ✕ (e.g. `validation_ipc_unavailable`)** — a **validation-HOST** condition. The broker was
  never contacted. **Do NOT tell the customer the broker is down, and do NOT tell them their details are
  wrong.** Correct message: the secure validation session couldn't start; their details weren't rejected;
  try again later. (This is exactly the IS6 case — the broker was proven operational.)
- **`broker_response` ✕ with `server_unavailable`** — the broker WAS reached and reported unavailable. Only
  here is "broker temporarily unavailable" the honest message.

The operator summary states the furthest stage reached and the first failing stage in one sentence — read it
to confirm.

## Step 4 — act (defer to the playbook for the customer message)

Once the locus is known, use the matching [support-playbook.md](support-playbook.md) workflow for the
customer wording, permitted actions, and escalation:

- Backend pre-flight (`broker_server_missing` / `credential_missing`) → playbook #1 / #7 (customer supplies
  broker identity / credential).
- Backend config (`validation_unconfigured`) → playbook #3 + escalate to Engineering (arming/config).
- Agent/host IPC (`validation_ipc_unavailable`, `validation_busy`, ...) → playbook #3 (validation
  temporarily unavailable — **no credential re-entry**); escalate to Operator for the host if persistent.
- Broker verdict (`invalid_password` / `invalid_login` / `account_disabled`) → playbook #4 (re-check
  credentials); (`server_unavailable`) → playbook #3 (broker-side, advise retry).
- `isolation_check_failed` / `impl_integrity_mismatch` → **platform fault**, escalate to Engineering.

## Step 5 — corroborate (optional)

- The **operational timeline** (Operations → the account's events) shows the committed `VALIDATION`
  projection for the same correlation id — a second, independent confirmation the result was recorded.
- The **validation history** on the account (staff view) shows the correlation-id column and the sequence of
  attempts, so you can see whether this is a one-off or a repeating host condition (informs the reliability
  picture — see [VALIDATION_RELIABILITY_EVIDENCE_MATRIX.md](../../VALIDATION_RELIABILITY_EVIDENCE_MATRIX.md)).

## Fidelity boundary (be honest with yourself)

Per-stage ✓/✕ is **derived** from the outcome reason code (the classifier already localises the failure); it
is not an independent per-stage instrument. Fine-grained agent-internal *timings* (launch→GUI→IPC→broker-TCP)
live only in the on-host diagnostic artefact and are not surfaced without SSH (a gated agent-forward change,
out of scope). What the timeline gives you **without SSH** is the correct **region** of the failure — which
is enough for every triage decision above. See
[VALIDATION_OBSERVABILITY.md](../../VALIDATION_OBSERVABILITY.md) for the fidelity contract and
[VALIDATION_RELIABILITY_EVIDENCE_MATRIX.md](../../VALIDATION_RELIABILITY_EVIDENCE_MATRIX.md) §"Fidelity".

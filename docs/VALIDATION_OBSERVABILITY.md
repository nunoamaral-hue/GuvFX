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

## Transport timeout taxonomy (2026-08-05)

A timeout can occur at several layers, and each has a DIFFERENT meaning. The layers, and which timeout /
reason belongs to each:

```
Browser
  │  (browser → backend HTTP; a failure HERE is client/network — shown as a network error, never persisted)
  ▼
Backend  ── requests.post(timeout=(connect=10s, read=175s for VALIDATE_LOGIN))
  │        ├─ CONNECT timeout (~10s): TCP session never opened → request NEVER SENT
  │        │      → ManagementChannelUnreachable → reason = validation_agent_unreachable
  │        └─ READ timeout (~175s): connection accepted, no response body in time (op status UNKNOWN)
  │               → ManagementChannelTimeout     → reason = validation_agent_timeout
  ▼
Validation agent  (single-flight lock; delegates to the GUI-capable runner)
  │        ├─ another check holds the lock            → validation_busy
  │        ├─ runner task won't launch                → validation_runner_unavailable
  │        └─ runner launched, no result in time      → validation_runner_timeout
  ▼
MT5  ── initialize(login,server,timeout≤120s): local IPC + terminal + broker connect + authorise
  │        ├─ local Python↔terminal IPC never up (-10004 / -10005) → validation_ipc_unavailable
  │        └─ MT5 reported a login-phase timeout                   → login_timeout  ◀ the ONLY legit login_timeout
  ▼
Broker
           ├─ reached, credential rejected  → invalid_password / invalid_login / account_disabled / server_not_found
           └─ reached, server unavailable   → server_unavailable
```

| Layer | Timeout | Reason code | Evidence of a login attempt? |
|---|---|---|---|
| Browser → Backend | client/network | (network error — not a validation reason) | no |
| Backend → Agent (connect) | ~10s (`CONNECT_TIMEOUT`) | `validation_agent_unreachable` | **no — request never sent** |
| Backend → Agent (read) | ~175s (VALIDATE_LOGIN read budget) | `validation_agent_timeout` | no — MT5 status unknown |
| Agent → MT5 (login) | ~120s (MT5 login window) | `login_timeout` (MT5-reported) | **yes — MT5 reached the login phase** |
| MT5 → Broker | broker-side | `server_unavailable` | yes — broker reached |

**Root cause of the reclassification (2026-08-05).** Account #13's `login_timeout` (correlation
`validate-acct-13-d4079267879e`, elapsed ≈10.0s) was a **backend→agent CONNECT timeout** to a down agent
port — the request never left the backend, MT5 was never invoked, the broker was never contacted. The
backend collapsed `requests.ConnectTimeout` and `requests.ReadTimeout` into one `ManagementChannelTimeout` →
`login_timeout`. They are now split: connect → `validation_agent_unreachable`, read → `validation_agent_timeout`,
and `login_timeout` is reserved for a genuine MT5-reported login-phase timeout.

**Known limitation — historical (pre-fix) `login_timeout` rows.** `reason_code` is immutable raw evidence
(`.claude/rules/data.md`), so pre-fix rows that persisted a *transport connect timeout* as `login_timeout`
(including account #13's `validate-acct-13-d4079267879e`) are NOT rewritten. Under the go-forward timeline
mapping (`login_timeout` → `mt5_launched`) those historical rows now render as if MT5 was launched — wrong for
that attempt, which was a connect timeout. This is a data artefact of the old classifier, not a code defect;
**going forward** a connect timeout persists `validation_agent_unreachable` (MT5 never implied). When triaging a
**pre-2026-08-05** `login_timeout`, check the elapsed: ~10 s (≈ the connect budget) is the old connect-timeout
mislabel, not an MT5 login timeout.

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

## In-process isolation diagnostic capture (2026-08-06)

There are exactly **two** validation execution modes, selected in `agent._build_login_validator`:

- **task-launched runner** — used when `BETA_AGENT_VALIDATION_TASK_NAME` is set; the runner
  (`validation_runner.run_once`) persists the per-correlation `<id>.diag.json` artefact.
- **in-process handler** — used when `validation_task_name` is **unset**; `LoginValidationHandler.validate`
  runs directly inside the supervised agent service process.

**Production currently runs the in-process mode** (`validation_task_name` unset). The earlier runner-only
isolation instrumentation therefore produced **no artefact** for a browser validation that failed
`isolation_check_failed`, because the runner never executed — see the deployment finding of 2026-08-06.

**What changed (in-process-capture packet):** the in-process handler now persists the **same** structured
isolation artefact on an `isolation_check_failed`, using the **one shared** evidence schema
(`validation_diagnostics.write_isolation_diagnostic` → `build_evidence`/`write_evidence`, same nested
`isolation` allow-list). The artefact carries `execution_mode: "in_process"`, the agent `process_id`
(session id is `null` — the agent is win32-free), `stage_reached: "AGENT_RECEIVED"`,
`first_failing_stage: "ISOLATION"`, the full `isolation` report (effective + canonical paths, the combined
forbidden roots, the matched root, and the five rule booleans), and the `config_source` provenance (env vs
default) for every path input. `config_source` is **shared** by both modes (one source of truth), so schema
drift is test-detectable.

**Invariants (unchanged):** this is **instrumentation only** — it does not fix or weaken the isolation
contract, does not change execution-model selection, and does not change the customer reason code
(`isolation_check_failed`), wording, or retryability. Diagnostic evidence remains **host-side** only; the
customer response stays `{ok, reason_code, is_demo}` and carries no host path. A diagnostic-write fault is
**fail-open**: the fail-closed result is unchanged and a secret-safe `diagnostic_capture_failed` operator log
line is emitted (correlation id + component + stage + fixed error class — never a path or exception text).
The staff timeline may note that host-side diagnostic evidence exists for the correlation id; the host path
is **not** forwarded to the backend in this change.

**Next authorised browser test is evidence-gathering only** — a single `Test connection` on the disposable
demo account, after this instrumentation is deployed, will write one `<id>.diag.json` recording the exact
failing isolation rule. It does not remediate the isolation configuration.

## In-process terminal materialisation (2026-08-07, P0)

The captured artefact above proved the exact blocker: `isolation.sub_reason = validation_terminal_missing`
— every path/containment rule passed but the validation terminal executable
(`…\validation-5833\terminal\terminal64.exe`) was **absent at validation time**. The isolation policy and
configuration are correct; the certified baseline was simply not present when the in-process validator ran
(the runner restores its baseline *after* each probe; nothing restored it on the in-process path).

**Fix:** the in-process validator now **materialises/restores** the isolated validation terminal from the
certified precompiled baseline **before** the isolation gate, under the single-flight lock, reusing the
runner's own mirror primitive (`validation_runner.mirror_validation_baseline` → `_mirror_os`). Order is now
**prepare → assert isolation → launch MT5** (never MT5 before isolation). The step:

- refuses to copy into a non-isolated destination or **from** a forbidden/golden/live/slot source (both are
  path-contract-checked; ONE rule source of truth via `_assert_isolated_dir`);
- reuses the source-validated, reparse-safe, deletes-extras mirror (an invalid/empty source never wipes the
  terminal dir — fail-closed);
- **never raises and never weakens the isolation gate** — a non-`restored` outcome leaves the terminal
  as-is and the authoritative gate still reports the exact rule;
- records a fixed, secret-safe `prepare_result` label in the isolation artefact, so any residual isolation
  failure shows whether the baseline was restored.

It does **not** change the execution mode (`validation_task_name` stays unset), the isolation rules, or the
customer contract. Restoring the baseline before every validation also removes any prior run's credential
artefact/logs. Once the terminal is present, the in-process MT5 launch is expected to advance to the
Session-0 GUI/IPC stage (the historical `-10004` IPC blocker) — that later stage is a **separate** follow-up.

### `prepare_result` label vocabulary

The label is fixed and secret-safe (paths/credentials never appear):

| label | meaning |
|-------|---------|
| `restored` | baseline mirrored in **and** `terminal64.exe` confirmed present at the destination |
| `precompiled_unconfigured` | no precompiled dir configured → materialisation is a no-op (see efficacy note) |
| `path_contract_unmet` | destination **or** source is not an isolated path; the isolation gate reports the exact rule |
| `no_source` / `invalid_source` | the precompiled source is missing or lacks `terminal64.exe` |
| `mirror_incomplete` | the mirror reported restored but `terminal64.exe` did **not** actually reach the destination (a swallowed per-file copy fault or a locked destination) — we decline to overclaim `restored`; the gate re-checks presence and still fails closed |
| `mirror_failed` / `prepare_unavailable` | the mirror raised, or the preparation step itself faulted (e.g. a lazy import failure); degraded so `validate` never raises and still runs the isolation gate + writes exactly one artefact |

### Deployment-config requirement (efficacy)

Materialisation is only **effective** when `BETA_AGENT_VALIDATION_PRECOMPILED_DIR` is set and the precompiled
baseline is placed so it satisfies the in-process path contract, i.e. it must be:

- an **absolute** path **beneath** the configured `BETA_AGENT_VALIDATION_ROOT` (default `C:\GuvFX\beta\validation`),
  e.g. `C:\GuvFX\beta\validation\_precompiled`;
- **disjoint** from every forbidden root (`…\slots`, both `…\golden` locations, `…\accounts`) — the guard
  deliberately refuses a golden/live/slot source, so the certified baseline must be its **own** copy under the
  validation root, never a pointer at the golden image.

If the source is unset or fails the contract, preparation no-ops (`precompiled_unconfigured` / `path_contract_unmet`)
and the original `validation_terminal_missing` blocker is **not** remediated — visible in the artefact via
`prepare_result`. The deployment phase must configure this and confirm a `restored` label before certifying.

### Known limitation — post-probe credential hygiene (in-process path)

The runner path performs deterministic post-probe **terminate + credential-scrub + removal-verify**
(`validation_runner.run_once`). The in-process path relies solely on **restore-before** (the next validation's
mirror deletes the prior run's artefacts) plus `mt5.shutdown()`. That closes the common case, but if a probe
leaves a **lingering** terminal that holds `accounts.dat` open, the next mirror's delete pass cannot remove it,
so a prior run's credential artefact can survive until the lock clears. (The `mirror_incomplete` label keys on
`terminal64.exe` presence, not on `accounts.dat`, so it does **not** by itself detect a surviving credential
file — it only prevents a false `restored` when the executable itself failed to materialise.) Bringing the
in-process path to full parity with the runner's post-probe terminate+scrub+verify is a **separate,
host-certified follow-up** (it terminates processes on the live host — outside this "materialise **before** the
isolation check" packet). Currently latent: the active Session-0 `-10004` IPC blocker fails the login before
any broker `accounts.dat` is persisted.

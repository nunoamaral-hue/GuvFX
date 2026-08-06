# Validation Reliability — Evidence Matrix (single source of truth)

**Purpose (Phase 4, WS-E).** One consolidated matrix of what is **known**, the **evidence** each fact
rests on, the **confidence**, what remains **unknown**, and the **next evidence** that would resolve it.
This is the authoritative reference for the reliability question; the investigation narrative
([VALIDATION_IPC_RELIABILITY_INVESTIGATION.md](VALIDATION_IPC_RELIABILITY_INVESTIGATION.md)) and the
observability doc ([VALIDATION_OBSERVABILITY.md](VALIDATION_OBSERVABILITY.md)) defer to it.

**Status:** DIAGNOSIS ONLY. No infrastructure was mutated. Every row is either a directly-observed record
or is explicitly marked as an unknown. No inference is presented as fact.

## Evidence sources (referenced by the matrix)

- **DIAG** — agent diagnostic artefacts on the validation host:
  `C:\GuvFX\beta\agent-state\validation-diagnostics\<correlation>.diag.json`
  (fields: `last_error_code`, `last_error_reason`, `initialize_result`, `mt5_package_version`,
  `terminal_info_present`, `broker_tcp_observed`, `stage_reached`, `first_failing_stage`, `elapsed_ms`,
  session ids, `stray_termination`, `baseline_restore_result`).
- **MANUAL** — the operator's manual MT5 login + trade on IS6Technologies-Demo (account 1302561).
- **ATTEMPT** — the durable append-only `BrokerAccountValidationAttempt` rows (`status`, `reason_code`).
- **CODE** — the merged repository (classifier + taxonomy + timeline).

## Matrix

| # | Known fact | Evidence | Confidence | Unknown | Next evidence required |
|---|------------|----------|------------|---------|------------------------|
| 1 | The broker server IS6Technologies-Demo is operational and reachable from the host. | MANUAL: a human connected, received live prices, placed and closed a trade. | **HIGH** — direct human observation. | Whether the broker is *continuously* available (not just at that instant). | Not required for the reliability question — one positive is sufficient to reject "broker unavailable". |
| 2 | The validation agent launched an MT5 terminal for attempt #7 (a process existed and reached `DATA_PATH_READY`). | DIAG: `stage_reached=TERMINAL_LAUNCHED`, `terminal_pid` present, `journal_milestones=["DATA_PATH_READY"]`. | **HIGH** — durable artefact. | The exact sub-second launch→GUI→IPC sequence (not captured). | Agent-forwarded per-stage timings (a gated agent+host change, out of scope). |
| 3 | The MetaTrader5 **Python↔terminal local IPC** never came up. | DIAG: `last_error_code=-10004`, `last_error_reason="No IPC connection"`, `initialize_result=false`, `mt5_package_version=(0,0,'')`, `terminal_info_present=false`, `first_failing_stage=GUI_READY`. | **HIGH** — direct artefact; four independent fields agree. | Why the GUI/IPC failed to initialise in this instance (mechanism). | Controlled credential-free IPC probe (WS-G plan) with per-stage capture. |
| 4 | The broker was **never contacted** during the failing attempts. | DIAG: `broker_tcp_observed=false` AND fact 3 (the package never reached a terminal, so no login was ever sent). | **HIGH** — direct artefact + logical necessity (no IPC ⇒ no login call). | — | — |
| 5 | `server_unavailable` was a **classification defect**, not a broker outage. | CODE: the old agent rule mapped `-10004`→`server_unavailable`; facts 1+3+4 prove the broker was fine and never reached. | **HIGH** — evidence + code inspection. | — | — |
| 6 | The correct reason is `validation_ipc_unavailable` (local host/session IPC), reserved distinctly from `server_unavailable` (broker-reached). | CODE: `deploy/beta-agent/validate_login.py:classify_init_error`; `broker_login_validation._TAXONOMY`; tests `tests_validate_login_agent.py`. | **HIGH** — merged + tested. | — | — |
| 7 | Session 0 **can** succeed on the same harness. | ATTEMPT/DIAG: Customer Zero #12 (login 1302575) reached `CLASSIFIED` with a real build (5833), `initialize_result=true`, in ~13 s. | **HIGH** — durable record of a clean success. | Under what conditions success vs failure occurs. | The controlled test (fact 10). |
| 8 | Session 0 **can** fail with `-10004`. | DIAG: attempts #7 and its retry both `-10004`. | **HIGH** — two durable records. | Whether failure is deterministic under any condition (load/time/prior-run). | The controlled test (fact 10). |
| 9 | A durable `VALIDATED` state is preserved across non-authoritative (host/transient) outcomes. | CODE: `broker_connectivity.run_broker_validation` + `_NON_AUTHORITATIVE_REASONS`; tests `tests_broker_connectivity.py`. | **HIGH** — merged + tested. | — | — |
| 10 | **The Session-0 IPC success/failure RATE is unmeasured.** | Only 3 data points exist (1 success, 2 failures). No controlled series was run. | **HIGH** (that it is unknown) | The actual reliability rate; whether isolation (interactive/GUI session) changes it. | The WS-G controlled reliability test: ≥20 consecutive credential-free IPC probes per arm (Session-0-idle vs interactive), repeated after a reboot, on a **disposable** workspace. |
| 11 | Concurrent interactive/operator activity *may* aggravate Session-0 readiness (hypothesis only). | Read-only `quser` showed active sessions ~17:28; the #13 failures were ~17:18/17:21 — **near but not proven concurrent**. | **LOW** — correlation, not causation; timing not aligned. | Whether interactive activity was concurrent *during* the failures. | The controlled test's concurrency sub-test. |

## Transport-layer finding (2026-08-05) — a `login_timeout` that never reached MT5

| # | Known fact | Evidence | Confidence | Unknown | Next evidence required |
|---|------------|----------|------------|---------|------------------------|
| 12 | Account #13's latest `login_timeout` (correlation `validate-acct-13-d4079267879e`) failed at the **backend→agent TCP connect**, ~10.0s. MT5 was never invoked; the broker was never contacted. | DB attempt id 10 persisted 21:10:08.602; `CREDENTIAL_ACCESSED` audit 21:09:58.577 → elapsed **10.02s** = the `CONNECT_TIMEOUT=10`. Reproduced live: a socket connect from `guvfx-backend` to the configured agent `100.79.101.19:8791` **failed with TimeoutError in 10.02s**. | **HIGH** — DB + audit + reproduced connect. | — | — |
| 13 | The proximate operational cause is a **port/agent-availability mismatch**: the backend's `BETA_AGENT_BASE_URL` targets `:8791`, which is not listening; `:8788` is open. | Read-only port probe from the backend: `:8788` OPEN (0.03s), `:8787` + `:8791` TimeoutError. | **HIGH** — reproduced. | Whether `:8791` should be corrected or the agent restarted (an operator/config decision, out of repo scope). | Operator action on the host (NOT part of this repository packet). |

**Code defect (now fixed in the repository, DARK):** the backend collapsed `requests.ConnectTimeout` and
`requests.ReadTimeout` into one `ManagementChannelTimeout` → `login_timeout`. A connect timeout is positive
evidence **no login occurred**. Split into `validation_agent_unreachable` (connect) / `validation_agent_timeout`
(read); `login_timeout` is reserved for a genuine MT5-reported login-phase timeout. See the **Transport timeout
taxonomy** in [VALIDATION_OBSERVABILITY.md](VALIDATION_OBSERVABILITY.md).

## What this matrix rules IN and OUT

- **Ruled OUT (HIGH confidence):** "the broker is unavailable" (facts 1,4); "the customer's credentials are
  wrong" (fact 4 — no login was ever attempted).
- **Ruled IN (HIGH confidence):** a **local validation-host / Session-0 IPC readiness** failure (facts 2,3).
- **Still OPEN (the only open question):** the **rate/determinism** of that failure (fact 10) — which is
  exactly what the controlled test measures, and the sole thing that can move the recommendation off
  Option C.

## Consequence for the recommendation

No row supports Option A (accept the host) or Option B (dedicated VM) **today**: fact 10 says the rate is
unmeasured, and facts 7+8 show the host both succeeds and fails. Recommending either architecture now would
be inference, not evidence. **Option C stands** until the controlled test (fact 10 → next evidence) runs.
See [VALIDATION_IPC_RELIABILITY_INVESTIGATION.md](VALIDATION_IPC_RELIABILITY_INVESTIGATION.md) §6–7 for the
executable plan and threshold.

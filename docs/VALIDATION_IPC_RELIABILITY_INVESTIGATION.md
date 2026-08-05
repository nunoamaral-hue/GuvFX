# Validation-host MT5 IPC reliability — investigation & controlled test plan

**Status:** DIAGNOSIS ONLY. No infrastructure was mutated; no host was accessed to produce this document
beyond read-only diagnostic-artefact reads already captured. Phase-3 structure (2026-08-05): observed facts,
evidence, hypotheses, unknowns, and recommendations are kept **strictly separate**. Every hypothesis and
every recommendation cites its evidence. The document states clearly **where the evidence ends**.

---

## 1. Observed facts (not interpretation)

- F1. The operator manually logged into IS6Technologies-Demo (account 1302561) in MetaTrader 5 and placed +
  closed a trade successfully.
- F2. Beta account #13 (login 1302587) failed two consecutive credentialed `VALIDATE_LOGIN` attempts (browser
  Test connection + Retry).
- F3. Customer Zero #12 (login 1302575) succeeded a `VALIDATE_LOGIN` earlier the same day (`demo_ok`).
- F4. The validation runner and the validation MT5 terminal ran in **Session 0** on the shared Windows host
  for all three attempts.

## 2. Evidence (the durable records the facts rest on)

Agent diagnostic artefacts on the host: `C:\GuvFX\beta\agent-state\validation-diagnostics\<correlation>.diag.json`.

| field | #13 #7 (`a252c05b0463`) | #13 retry (`df493ffcfc0c`) | #12 SUCCESS (`7e3eca08465d`) |
|---|---|---|---|
| `last_error_code` | -10004 | -10004 | null |
| `last_error_reason` | "No IPC connection" | "No IPC connection" | "" |
| `initialize_result` | false | false | true |
| `mt5_package_version` | `(0, 0, '')` | `(0, 0, '')` | `(500, 5833, '25 Apr 2026')` |
| `terminal_info_present` | false | false | true |
| `account_info_present` | false | false | true |
| `stage_reached` | TERMINAL_LAUNCHED | TERMINAL_LAUNCHED | CLASSIFIED |
| `first_failing_stage` | GUI_READY | GUI_READY | null |
| `journal_milestones` | `["DATA_PATH_READY"]` | `["DATA_PATH_READY"]` | `[]` |
| `broker_tcp_observed` | false | false | false |
| `elapsed_ms` | 135819 | 134021 | 13322 |
| `terminal_session_id` / `runner_session_id` | 0 / 0 | 0 / 0 | 0 / 0 |
| `terminal_pid` | 14184 | 9704 | 15956 |
| `gui_mdi_failed` | false | false | false |
| `stray_termination` (force-killed) | killed [14184] | killed [9704] | killed [15956] |
| `baseline_restore_result` | restored | restored | restored |
| `reason_code` | server_unavailable* | server_unavailable* | demo_ok |

*mis-classified at capture time; corrected to `validation_ipc_unavailable` in the WS-A remediation. Note
`gui_mdi_failed=false` while `first_failing_stage=GUI_READY`: the runner journalled `DATA_PATH_READY` but no
GUI/MDI-created **or** MDI-failed milestone — the terminal hung before either, consistent with H1 (GUI/IPC
never initialised) rather than an explicit MDI-create failure.

- E1 (→F1): the broker server accepted a live login and order — it is operational and reachable from the host.
- E2 (→F2): `-10004 "No IPC connection"` is the MetaTrader5 **Python↔terminal local IPC** failing; `broker_tcp_observed=false`
  and `mt5_package_version=(0,0,'')` prove the broker was never contacted and the package never reached a terminal.
- E3 (→F3): #12 reached `CLASSIFIED` with a real terminal build (5833) and `initialize_result=true` — the same
  harness, same session type, produced a clean 13 s success.
- E4 (→F4): `runner_session_id=0` and `terminal_session_id=0` on every artefact.

## 3. Hypotheses (each cites evidence; ranked)

- H1 — **Intermittent Session-0 GUI/window-station → IPC readiness (STRONGEST).** Evidence: E2+E3+E4 — the
  terminal launched (a pid exists, reached `DATA_PATH_READY`) but never created its GUI/IPC (`first_failing_stage=GUI_READY`,
  no `GUI_MAIN_WINDOW_CREATED`/`IPC_PIPE_READY`), the Python package never connected, and the identical
  Session-0 harness *succeeded* for #12. This is the documented ADR-0027 Session-0 MT5 behaviour.
- H2 — **Concurrent interactive/operator activity aggravating Session-0 readiness (PLAUSIBLE).** Evidence:
  read-only `quser` showed active interactive sessions on the host, and the #13 failures fell in the same time
  window as operator activity. *Weaker: the manual MT5 was confirmed only ~10 min after the failures — not
  during them* (see Unknowns U2).
- H3 — **Stale/hung terminal from the prior run impairing the next (POSSIBLE).** Evidence: each failing
  artefact shows `stray_termination` force-killed a terminal that survived `mt5.shutdown()`. Mitigated by the
  dirty-baseline guard + `baseline_restore_result=restored`, so a *persisted* contaminant is unlikely.
- H4 — **Build/package mismatch (WEAK).** `terminal_build=null` for #13 is a *symptom* of E2 (IPC never up so
  no build could be read), not a demonstrated cause; #12 read build 5833 from the same baseline.

## 4. Unknowns (where the evidence ends)

- U1. The **rate** of Session-0 IPC success vs failure is unmeasured. Three data points (1 success, 2 failures)
  are not a reliability rate.
- U2. Whether an interactive session/MT5 was **concurrently running during** the #13 failures (17:18 / 17:21)
  is not established — only that one existed ~17:28 (H2 is therefore not confirmable from current evidence).
- U3. The **per-stage agent-internal timings** (terminal-launch → GUI → IPC → broker-TCP) are not in the
  backend; they live only in the host artefact, and the fine sub-second sequence is not captured.
- U4. Whether the failure is deterministic under any specific condition (load, time-of-day, prior run) is
  unknown — by definition, until the controlled test (§6) runs.

## 5. Recommendation (cites evidence)

**OPTION C — evidence insufficient.** Evidence: E3 proves Session 0 *can* succeed; E2 proves it *can* fail;
U1 states the rate is unmeasured. Replacing the architecture (a dedicated validation VM, Option B) on three
data points would violate the no-assumption rule.

- **Option A** (existing host, bounded remediation): open, unproven.
- **Option B** (dedicated interactive validation session/VM): **do NOT recommend yet.** Recommend B **only if**
  the controlled reliability test (§6) **fails** the §7 threshold — i.e. Session 0 cannot meet the
  consecutive-success bar even when isolated.
- **Option C** (current): the honest status; the next step is the controlled test, **not** an infrastructure
  decision. Only its result can move the recommendation off C.

---

## 6. Controlled reliability test plan (executable; NOT executed here)

Runs only after this plan is approved, on a **disposable** validation workspace. No Customer Zero (#12), no
live account (#1), no orders, no host modification beyond running the probe in the disposable workspace.

**Procedure**
1. **Credential-free startup/IPC probes FIRST.** `mt5.initialize(path=…, portable=True)` with no
   login/password/server (or a deliberately bogus login), asserting only local IPC readiness. This isolates
   IPC readiness from any broker/credential concern.
2. **One attempt at a time,** serialized via the existing single-flight lock; never overlap probes.
3. **Deterministic cleanup between runs:** terminate the terminal (path-guarded), verify no `terminal64.exe`
   remains for the workspace, restore the precompiled baseline, assert a clean baseline fingerprint before the
   next run.
4. **Two arms:** Arm A = Session 0, host otherwise idle; Arm B = the runner in a GUI-capable interactive
   session (auto-logon console). Compare IPC-success rate and `elapsed_ms` distributions (tests H1).
5. **Concurrency sub-test (H2/H3):** with Arm B as baseline, run one probe while an interactive MT5 is
   deliberately open, to measure interference.

**Pass criteria (per arm):** every probe has `initialize()==true`, non-zero `mt5_package_version`,
`terminal_info_present==true`, `last_error_code==null`, and `elapsed_ms` far below the login-timeout window.

**Fail criteria (per arm):** any `-10004`/"No IPC connection", any run whose `elapsed_ms` approaches the
timeout window (~120 s+), any `mt5_package_version==(0,0,'')`, or any stale-process / baseline-restore failure.

**Evidence required (every run):** a diagnostic artefact with `initialize_result`, `mt5_package_version`,
`last_error_code/reason`, `stage_reached`, `first_failing_stage`, `elapsed_ms`, session ids, cleanup +
baseline-restore results. Runs are numbered; the full sequence is retained (successes AND failures).

**Sample size:** ≥ 20 consecutive probes per arm; repeat the full ≥20 run **after at least one host reboot**.

**Abort conditions:** stop immediately (and do NOT continue the sequence) on: any Customer-Zero / live-account
/ production-terminal process appearing as a termination target; a baseline-restore failure that the
dirty-baseline guard does not catch; or any sign the probe touched a non-disposable path.

**Recovery procedure:** on abort or any dirty state — terminate only path-guarded disposable-workspace
terminals, restore the precompiled baseline, verify the fingerprint, and DO NOT run the next probe until the
workspace is baseline-clean. If recovery cannot be verified, stop and escalate (no further probes).

## 7. Success threshold (accept the existing host only if ALL hold)

- **≥ 20 consecutive** clean IPC probes (per Pass criteria), **zero** `-10004`, in Arm A (Session 0, idle host).
- **Zero** stale-process / cleanup / baseline-restore failures across the sequence.
- Bounded, stable `elapsed_ms` (no run approaching the timeout window).
- Clear isolation + rollback evidence each run (the guard never trips on the next run).
- The threshold re-met **after a host reboot** (readiness survives a cold start).

Only if Arm A clears this threshold is **Option A** viable. If Arm A fails it but Arm B clears it, that is the
evidence that would justify **Option B**. Until the test runs, the recommendation remains **Option C**.

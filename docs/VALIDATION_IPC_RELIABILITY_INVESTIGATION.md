# Validation-host MT5 IPC reliability — investigation & controlled test plan (WS-B)

**Status:** DIAGNOSIS ONLY. No infrastructure was mutated. This document is the evidence review, ranked
hypotheses, and a controlled test plan for review before any host change. 2026-08-05.

## 1. What failed

Two consecutive credentialed `VALIDATE_LOGIN` attempts for beta account #13 (browser Test connection +
Retry) failed. Primary evidence — the agent's durable diagnostic artefacts on the Windows host
(`C:\GuvFX\beta\agent-state\validation-diagnostics\<correlation>.diag.json`):

| field | #13 attempt #7 (`a252c05b0463`) | #13 retry (`df493ffcfc0c`) | **#12 SUCCESS (`7e3eca08465d`)** |
|---|---|---|---|
| `last_error_code` | **-10004** | **-10004** | `null` |
| `last_error_reason` | **"No IPC connection"** | **"No IPC connection"** | `""` |
| `initialize_result` | false | false | **true** |
| `mt5_package_version` | **`(0, 0, '')`** | **`(0, 0, '')`** | **`(500, 5833, '25 Apr 2026')`** |
| `terminal_info_present` | false | false | **true** |
| `account_info_present` | false | false | **true** |
| `stage_reached` | TERMINAL_LAUNCHED | TERMINAL_LAUNCHED | **CLASSIFIED** |
| `first_failing_stage` | GUI_READY | GUI_READY | `null` |
| `journal_milestones` | `["DATA_PATH_READY"]` | `["DATA_PATH_READY"]` | `[]` (fast success) |
| `broker_tcp_observed` | false | false | false |
| `gui_mdi_failed` | false | false | false |
| `elapsed_ms` | 135819 (~136 s) | 134021 | **13322 (~13 s)** |
| `terminal_pid` / `session` | 14184 / **0** | 9704 / **0** | 15956 / **0** |
| `runner_session_id` | **0** | **0** | **0** |
| `stray_termination` | killed [14184] | killed [9704] | killed [15956] |
| `baseline_restore_result` | restored | restored | restored |
| `reason_code` | server_unavailable* | server_unavailable* | demo_ok |

*misclassified — corrected in WS-A to `validation_ipc_unavailable`.

MT5 error **-10004 "No IPC connection"** is the MetaTrader5 Python package failing to establish its **local**
IPC channel to **its own terminal process** — *before* any broker login. Corroboration: `mt5_package_version`
is `(0,0,'')` (the package never talked to a terminal), no terminal/account info, no broker TCP, and the run
consumed the **full ~136 s** login-timeout window then failed. The terminal process *did* launch (a pid
exists, reached `DATA_PATH_READY`) but never created its GUI main window / IPC pipe, and had to be
force-killed (`mt5.shutdown()` alone did not exit it).

## 2. Why #12 reached CLASSIFIED while #13 reached only DATA_PATH_READY

The **same** task-launched runner path, the **same Session 0**, produced a clean 13 s success for #12 at
13:37 and a 136 s `-10004` failure for #13 (twice) at 17:18 / 17:21. The differentiator is **whether
`mt5.initialize()` establishes the local Python↔terminal IPC**: for #12 it did (real package version
`500/5833`, `initialize_result=true`); for #13 it did not (`(0,0,'')`, "No IPC connection"). Both ran in
Session 0. **Session 0 can work but is not reliably working** — the failure is intermittent, not
deterministic, and is independent of the IS6 broker (which the operator separately proved is up by trading
on it manually). The account login (1302587 vs 1302575) is irrelevant: `-10004` occurs at IPC establishment,
*before* credentials are used.

Host session facts (read-only, `quser` at investigation time): the runner + terminal run in **Session 0**
(the non-interactive services session) while interactive sessions exist (console session 1; RDP session 3).
MT5's terminal needs a GUI-capable window station / message pump to bring up its IPC pipe; Session 0 provides
this unreliably (documented ADR-0027 root cause: MT5 GUI/MDI creation is unreliable when not in a GUI-capable
window station).

## 3. IPC-failure hypotheses, ranked by evidence

1. **H1 — Intermittent Session-0 GUI/window-station readiness (STRONGEST).** The terminal launches but hangs
   before GUI/IPC-ready (`DATA_PATH_READY` only, no `GUI_MAIN_WINDOW_CREATED`/`IPC_PIPE_READY`), the Python
   package never connects (`(0,0,'')`), and the run burns the full timeout then force-kills a hung terminal.
   Directly matches the documented Session-0 MT5 behaviour; reproduced twice; contradicted by no evidence.
   *Evidence: the three artefacts above; `runner_session_id=0`/`terminal_session_id=0`; `first_failing_stage=GUI_READY`.*
2. **H2 — Concurrent interactive/operator activity on the host aggravating Session-0 readiness (PLAUSIBLE,
   timing-adjacent).** The #13 failures (17:18, 17:21) coincided with the operator's active app sessions and a
   manual interactive MT5 login on the host (screenshot ~17:28). An interactive session competing for GUI /
   window-station resources could tip the intermittent Session-0 IPC into failure. *Evidence: timing overlap;
   an active RDP session present. NOT conclusive — the manual MT5 is confirmed at 17:28, after the failures.*
3. **H3 — Stale/hung terminal from the prior run impairing the next (POSSIBLE, partly mitigated).** Each
   failing run left a terminal that survived `mt5.shutdown()` and was force-killed; a lingering terminal or
   held IPC namespace could impair a subsequent launch. Mitigated by the dirty-baseline guard + verified
   baseline restore (`baseline_restore_result=restored`), so a *persisted* contaminant is unlikely, but a
   transient in-flight overlap is not excluded. *Evidence: `stray_termination` killed a terminal on every
   failing run.*
4. **H4 — Build/package mismatch (WEAK).** `terminal_build=null` for #13 is a **symptom** (IPC never came up,
   so no build could be read), not a demonstrated cause; #12 read `build 5833` from the same baseline.
5. **H5 — Closely-spaced attempts / concurrency (WEAK for the IPC failure itself; PROVEN for the #12 status
   flip).** The #13 attempts were ~3 min apart (not tight). Separately, the ~136 s hangs *widened* the
   single-flight window and directly caused the #12 `validation_busy` collision (see WS-C).

## 4. Controlled test plan (diagnosis, NOT customer clicks)

To be run only after this plan is reviewed and explicitly approved, on a **disposable** validation workspace.
No Customer Zero (#12) mutation, no live account #1 mutation, no orders.

1. **Credential-free startup/IPC probes FIRST.** Launch the isolated validation terminal and call
   `mt5.initialize(path=…, portable=True)` with **no login/password/server** (or a deliberately bogus login),
   asserting only whether the **local IPC** comes up (`initialize()` return + `mt5.version()` non-zero +
   `terminal_info()` present). This isolates the IPC-readiness question from any broker/credential concern.
   Capture an evidence artefact for every run (reuse the existing diagnostics writer).
2. **One attempt at a time.** Serialize via the existing single-flight lock; never overlap probes. Record
   `runner_session_id` / `terminal_session_id` for each.
3. **Deterministic cleanup between runs.** After each probe: terminate the terminal (path-guarded), verify no
   `terminal64.exe` remains for the workspace, restore the precompiled baseline, and assert a clean baseline
   fingerprint before the next run. Fail the run if a terminal survives or the baseline is dirty.
4. **Two arms, same workspace:**
   - **Arm A — Session 0 only** (current path): N consecutive credential-free IPC probes with the host
     otherwise idle (no interactive MT5, no RDP-driven MT5).
   - **Arm B — interactive session**: the runner launched inside a GUI-capable interactive session
     (auto-logon console session / dedicated interactive account), N consecutive probes.
   Compare IPC-establishment success rate and `elapsed_ms` distributions between arms. This directly tests H1
   (Session 0 unreliable) and, by contrast, whether an interactive session is reliable.
5. **Concurrency sub-test (H2/H3):** with Arm B established as the baseline, run one probe while an interactive
   MT5 is deliberately open, to measure interference.
6. **Evidence artefact for every run** (already emitted): `initialize_result`, `mt5_package_version`,
   `last_error_code/reason`, `stage_reached`, `first_failing_stage`, `elapsed_ms`, session ids, cleanup +
   baseline-restore results.

## 5. Reliability threshold (accept the existing host only if ALL hold)

- **≥ 20 consecutive** credential-free IPC probes with `initialize()` success + non-zero `mt5_package_version`
  + `terminal_info_present`, **zero** `-10004`/"No IPC connection".
- **Zero** stale-process or cleanup/baseline-restore failures across the run.
- A bounded, stable `elapsed_ms` (no runs approaching the login-timeout window — a ~136 s run is a failure
  signature, not a slow success).
- Clear isolation + rollback evidence (each run leaves the workspace baseline-clean; the guard never trips on
  the next run).
- The same threshold re-met **after** at least one host reboot (readiness must survive a cold start).

## 6. Recommendation (evidence-based, pending the controlled test)

On the evidence to date (H1 strongest; Session 0 demonstrably intermittent; the documented ADR-0027 root
cause is that MT5 needs a GUI-capable window station), the leading recommendation is **B — a dedicated
interactive validation session/VM is required** for reliable IPC, i.e. run the validation runner in an
auto-logon interactive console session (or a dedicated validation VM), not Session 0. **A — the existing host
made reliable with bounded remediation** remains open *iff* Arm A of the controlled test clears the §5
threshold; the current evidence (2/2 recent failures, intermittent success) does not yet support A. **C —
insufficient evidence** is the honest status for the *quantified* reliability rate until the controlled test
runs. No host change is proposed here; the controlled test (credential-free, disposable, one-at-a-time) is the
next gated step and must be approved before execution.

# WS-D — Session-0 MT5 GUI limitation: research analysis (2026-07-24)

Read-only analysis of the MT5 evidence captured during the Phase-8 viability launch (beta slot 1, runtime
`1f1b4b83…`, terminal64.exe pid 8200, `TASK_LOGON_PASSWORD` as `guvfx_b_slot1`, **Session 0, no interactive
desktop**). No new launch was performed; the WS-D *experiments* remain gated on WS-C (slot release), which is
gated on ADR 0014 (RELEASE) approval. This covers WS-D research points 1–3.

## Source
`C:\GuvFX\beta\slots\1\terminal\logs\20260724.log` (23 lines) + `…\logs\metaeditor.log` (compiler).

## What WORKS under Session 0 (non-interactive TASK_LOGON_PASSWORD)
| Evidence line | Meaning |
|---|---|
| `MetaTrader 5 x64 build 6036 started for MetaQuotes Ltd.` | The terminal process starts and initialises. |
| `Windows Server 2025 build 26100 on KVM, 8 x AMD EPYC-Milan, … GMT+0` | Environment enumeration works. |
| **`MCP started on 127.0.0.1:22346`** | The terminal's **internal IPC/control server is up** — the endpoint the MT5 Python API / bridge connect to. Not desktop-dependent. |
| `Compiler full recompilation has been started` … **`finished: 131 file(s) compiled`** | The **MQL5 compiler/engine runs to completion** in Session 0. |
| `updating …\MQL5 folder; 453 files updated` | File management works; writes contained to the slot. |

## What FAILS under Session 0
| Evidence line | Meaning |
|---|---|
| `Window MDI create failed … error 0` (repeated) | The **MDI child-window container** cannot be created — it needs an interactive window station/desktop. |
| `Window MDI unhook failed …` | Same root cause (window hook on a non-interactive station). |
| `Document create frame from resource 131 failed` / `load frame from 131 resource failed` | The chart frame GUI resource cannot be instantiated. |
| `Document create new frame '…\PROFILES\CHARTS\DEFAULT\CHART0{2,3,4}.CHR' failed` | The saved **charts cannot be restored** (no MDI to host them). |

`MQL5\Logs\` has **no expert/script log** — no EA or script was run during the trial, so EA-on-chart
execution is **untested**, not proven-failed.

## Conclusions (WS-D points 2–3)
- The failure is **specific to chart restoration + MDI window creation**, i.e. the visible GUI. It is **not**
  fatal to: the terminal process, the environment/detection, the MQL5 compiler/engine, or the internal IPC
  control server (`127.0.0.1:22346`).
- It is **inherent to Session 0** — a batch-logon (`TASK_LOGON_PASSWORD`) identity runs on a non-interactive
  window station with no desktop, and MDI child-window creation requires one. This is the classic Windows
  Session-0 GUI restriction, not an MT5 defect.
- **Does MT5 require a window station/desktop for charts?** Yes — for chart/MDI windows. **No** — for the
  terminal core, the MQL5 compiler, or the IPC/control surface.

## Implication for the execution model (to be PROVEN by the gated WS-E trial)
- An execution path that **does not need a visible chart** — the MT5 Python API / the bridge (both speak to
  the `127.0.0.1:22346` control server), or a chartless mechanism — is a *credible* path to trading function
  under Session 0. The compiler and IPC server both being up support this.
- An **EA-on-chart** model is likely blocked (no chart to attach to). Unproven either way here.
- Configuration-only mitigations to evaluate under WS-D experiments (post WS-C): launch with an **empty /
  no-restore chart profile** (removes the `CHART*.CHR` restore failures, though the MDI-create failure may
  persist), and confirm whether the Python-API/bridge path is unaffected by the MDI failures.

## Preliminary classification (subject to the WS-E functional trial)
**VIABLE WITH NON-TRADING CONSTRAINTS (provisional).** Process + engine + IPC are up under Session 0; the
chart GUI is not, and trading function (auth → market data → chartless MQL5 execution) is **not yet proven**.
A definitive `FULLY VIABLE` / `VIABLE WITH NON-TRADING CONSTRAINTS` / `NOT VIABLE IN SESSION 0` verdict
requires the WS-E disposable-demo trial, which is gated on WS-C (slot release) and thus on ADR 0014 approval.

## Not covered / limitations
No new launch was run (experiments gated on WS-C). EA/script execution untested. No account, no market-data
connection attempted. The `MCP` server being *up* does not by itself prove the Python API can drive a trade
in this session — that is the WS-E question.

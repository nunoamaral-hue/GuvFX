# B3P-2 — MT5 Disposable-Demo Functional Validation — Design (WS-A)

- Date: 2026-07-25
- Status: **Design (pre-host).** No host action until this is settled.
- Scope authority: the "GUVFX — MT5 DISPOSABLE-DEMO FUNCTIONAL VALIDATION" packet. **No order of any kind
  (market/limit/stop/pending, real or demo) is authorised by this packet.** Demo order placement is a separate
  sponsor decision taken only if the non-trading trial succeeds.

## 1. Question this validation answers

Does a disposable-demo MT5 runtime, launched through the approved GuvFX per-slot lifecycle, provide the functions
required for *automated* trading — **despite the recorded chart/MDI GUI limitation** — up to but **excluding**
order execution?

### 1a. Session model — measured, not assumed (supersedes a stale finding)

The B3P per-slot lifecycle launches the runtime via the per-slot batch-logon scheduled task, so the terminal
lands in **Session 0** (the agent service is also Session 0; the operator's production terminal is Session 3).
An earlier feasibility note (`BETA_HEADLESS_WSA_FEASIBILITY.md`, 2026-07-20, golden **5833**) recorded that a
portable MT5 in Session 0 "starts but does not persist — exits within ~10–30 s", and the accepted co-host
Option A therefore targeted an interactive **Session 1**.

**That Session-0 finding no longer holds on the current mechanism.** Fresh measurement (2026-07-25, golden
**6036**, launched via the ADR-0016 wrapper through the signed lifecycle): the Session-0 beta terminal
**persisted ~5 minutes, healthy** (172→118 MB, 18 threads) and its **MQL5 compiler completed** ("full
recompilation has been finished: 131 file(s) compiled"). The **chart/MDI GUI errors are present** as documented
("MDI create failed", "create new frame CHART0x.CHR failed"), but they did **not** kill the terminal. So the
current per-slot Session-0 launch is process-viable; whether an EA can attach and run *given* the chart failures
is the open L2 question this validation answers. (This resolves the review's HIGH design finding by measurement:
the doc no longer claims an unproven Session-0 persistence — it cites a current control.)

## 2. Viability layers (kept strictly distinct)

Process survival is **not** functional success. Each layer is judged on its own evidence:

| # | Layer | What proves it | Needs a demo account? |
|---|-------|----------------|-----------------------|
| L1 | Terminal process viability | one contained beta terminal64 in Session 0, **persists** (≥5 min measured 2026-07-25), MQL5 compiler completes | no (measured — §1a) |
| L2 | EA runtime viability | EA compiles, attaches, `OnInit` returns `INIT_SUCCEEDED`, `OnTimer` fires repeatedly, clean `OnDeinit` | **no** (timer is account-independent) |
| L3 | Broker connectivity | terminal reaches the demo server; `TerminalInfoInteger(TERMINAL_CONNECTED)`; account info readable | **yes** |
| L4 | Market-data viability | symbol selectable; bid/ask received; `OnTick` fires; tick timestamps advance | **yes** (and market open) |
| L5 | Chart-dependent functionality | whether MDI/CHART failures block attachment/indicators/visual objects | partial |
| L6 | Trade-execution viability | **out of scope — not tested, not authorised** | n/a |

**The decomposition matters:** L1–L2 (and L5's "does the chart failure block the EA?") are answerable
**account-free**. L3–L4 require the Nuno-provisioned disposable demo login. This lets the account-free layers be
proven first, de-risking the EA runtime independently of any credential.

## 3. Success / failure criteria (per the packet's classification)

- **A — Fully functional for non-interactive automation:** L1 ✓, L2 ✓, L3 ✓, L4 ✓, and L5 chart errors do **not**
  prevent required automation.
- **B — Functional with strategy constraints:** L1 ✓, L2 ✓, L3 ✓, L4 ✓, but chart objects/visual indicators
  fail → only chart-independent (timer/tick-logic) EAs are supported; templates/graphical strategies unsuitable.
- **C — Process viable, functionally insufficient:** L1 ✓ but a required layer fails (EA cannot init, no market
  data, login cannot complete, or chart dependency blocks automation).
- **D — Not viable:** terminal crashes, restart loop, unsafe containment, or any production impact.

A definitive A/B verdict **requires** the account phase (L3/L4). The account-free pass can reach at most a
provisional "L1–L2 pass; L3–L4 pending" and will classify no higher than the evidence supports.

## 4. Evidence sources

- **Agent (signed, `NT SERVICE\GuvFXBetaAgent`):** NEGOTIATE/MATERIALISE/VERIFY/START/STOP/TOMBSTONE/RELEASE
  JSON (occupancy, pid, session, containment).
- **Diagnostic EA log:** a sanitised file written **inside the slot** (`MQL5\Files\` of the runtime) — the
  authoritative L2–L4 record (see WS-B fields). Redacted identifiers only.
- **Terminal logs:** `logs\*.log` and `MQL5\Logs\*.log` inside the slot — build, connection, MDI/CHART errors.
- **Independent host observation:** `Get-Process`/WMI for pid/path/session/handles/memory; directory scans for
  containment; golden digest; tombstone inventory. **Read-only, admin-side.**
- **No screenshots of credentials, no account numbers in full, no passwords anywhere.**

## 5. Observation duration

Bounded, ≥ 15 minutes when the selected market is open (per packet). Account-free L2 trial: a few minutes is
enough to prove repeated `OnTimer` firings + stability. L3/L4 (account phase): the full ≥15-minute window, timed
to an open market (e.g. a major FX pair during London/NY session; a 24/5 symbol avoids weekend closure).

## 6. Diagnostic EA behaviour (full spec → WS-B)

A minimal EA that **never trades**. On a fixed `OnTimer` cadence it appends one sanitised heartbeat line to its
slot-contained log with the fields the packet enumerates (build, login-present bool, trade mode, demo/real
class, server-if-safe, connection state, symbol, symbol-available, bid/ask, first-tick ts, tick count, timer
count, terminal/EA trade-permission, AutoTrading, a genuine market-open signal (last-quote freshness) beside the
symbol trade mode, last MQL5 error, chart id/windows, FS write path, heartbeat ts). It **fails closed** — logs a
fatal marker and halts — if the account classifies as anything other than demo, re-asserted **every heartbeat
(OnTimer), not only at OnInit**, so a mid-run login to a non-demo account also latches closed. It contains **no**
`OrderSend`, `CTrade` buy/sell, pending-order path, indirect control transfer
(`iCustom`/`ChartApplyTemplate`/`IndicatorCreate`/`EventChartCustom`), external network call, DLL import, or
credential print. A static test (mutation-verified) rejects the EA if any of those appears, if the login is
valued, or if a non-server account string is read.

## 7. Account & symbol prerequisites (→ WS-C, Nuno-gated)

- **One disposable demo account** on a broker whose demo offers the target symbol, provable as demo, no real
  funds, credentials unique to this validation, discardable, never reused from production.
- **Credentials never enter chat/Git/logs/evidence/screenshots.** Nuno provisions the login into the slot config
  locally; the model never sees the password. Exact secret-safe operator instructions are returned in WS-C.
- **The startup `guvfx_startup.ini` protects integrity, not confidentiality.** A config `terminal64` reads is
  readable by in-slot code (both run as the slot identity), so a login placed in it is not secret from the
  tenant (ADR-0016 amendment). This is acceptable ONLY because the account is a **disposable demo** owned by the
  tester; a production/live credential must never go in a startup config. The account-free L2 trial uses a config
  with **no** credentials (Expert + Symbol only).
- **Symbol:** one liquid symbol the demo server offers and that is open during the observation window.

## 8. Containment checks

Every phase: all writes remain inside the assigned slot; golden image digest unchanged; slots not selected
unchanged; production MT5 (pid/path/session) unchanged; bridge (pid/port 8788) unchanged; exactly one beta
terminal64; the beta runtime's **observed owner SID is the expected slot identity** (`guvfx_b_slot<n>`), never
the operator/Administrator; no unexpected child process persists; no new port exposure; no credential in any
evidence artefact.

## 9. Cleanup & rollback

Native signed shutdown only: STOP → VERIFY ABSENT → TOMBSTONE → RELEASE → Available. Account for the known
Restart-Manager handle-release delay: a TOMBSTONE `cleanup_precheck_failed` may be retried after a bounded wait
**only** when it is attributable to lingering runtime handles AND the runtime process is independently proven
ABSENT (VERIFY running:false); any other unmet cleanup proof (a still-present process, a generation/ownership
mismatch) is a STOP, not a wait. No manual runtime deletion. Remove all scratch/credential artefacts; verify 0
residue. Slots end all-Available; tasks remain Enabled + triggerless.

## 10. STOP conditions (verbatim intent)

Stop immediately, preserve evidence, on: account not conclusively demo; any credential in output/logs; any
order/trade attempted; >1 beta MT5 process; production MT5 changes; bridge changes; unexpected port exposure;
runtime writes outside its slot; golden image changes; another slot changes; EA contains/reaches a trade path;
service observation unavailable; native STOP fails; cleanup requires manual deletion; runtime cannot be released
cleanly; any live-account identifier discovered; **any slot-integrity / (slot, generation) monotonicity /
ownership-marker disagreement (SlotIntegrityError / slot quarantined) at any stage**; **the runtime is observed
in a session other than the expected one, or does not persist (an unexpected early exit — the §1a control),
which would confound the functional result**.

## 11. Sequence

1. WS-A design (this) → WS-B diagnostic EA + static no-trade test (pipeline) → WS-D merge (no secrets).
2. WS-E host pre-flight (read-only) → WS-F fresh runtime → **account-free L2 trial** (compile/attach/OnInit/
   OnTimer headless; characterise the chart/MDI failure's effect on the EA).
3. **Nuno gate (WS-C/WS-G):** disposable demo account provisioned secret-safely → demo login → L3/L4 observation.
4. WS-I native shutdown + repository closure + final classification.

Between (2) and (3) the model returns with results + exact credential-safe instructions; it does not create
accounts or handle credentials, and places **no order**.

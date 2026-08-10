# LEGACY_RETIREMENT_PLAN — Customer Zero (account id=1 / broker 1302561 / IS6Technologies-Demo)

> **Status:** PLAN ONLY. Nothing disabled, removed, or reconfigured. Execution DARK
> (`HOSTED_MT5_EXECUTION_ENABLED` unset). Retires the *legacy* Administrator interactive MT5 executor for
> TradingAccount **id=1** (broker **1302561**, **IS6Technologies-Demo**, demo) in favour of the hosted
> portable runtime under non-admin **`guvfx_u_1`** at `C:\GuvFX\accounts\1\terminal`, delivered as a
> Guacamole RemoteApp. Facts are tagged **VERIFIED** (repo) vs **ASSUMED** (host-observation/memory —
> confirm on host before acting).

> Scope note: distinct from the *earlier* "Customer Zero" (prod user #16 / account #12 = 1302575, the
> headless AccountRuntime/beta-provisioner path in `docs/CUSTOMER_ZERO_EVIDENCE_MATRIX.md`). This plan is
> the **live production** account **id=1 / 1302561**.

## 1. Inventory — legacy execution stack

| # | Component | What it is | Status |
|---|-----------|-----------|--------|
| L1 | Legacy MT5 executor | `terminal64.exe` from `C:\Program Files\IS6 Technologies MT5 Terminal\`, as **Administrator**, **console session 1**; pre-logged into 1302561; GuvFX only *attaches* (`mt5.initialize(path=)`), never `mt5.login()`. | VERIFIED (host-obs docs: SINGLE_SESSION_INVARIANT, host-cert; memory pid 3972) |
| L2 | Signal-execution bridge | `scripts/mt5_signal_bridge.py` on-host as `C:\GuvFX\mt5_signal_bridge.py`; HTTP :8788 `/mt5/order`,`/mt5/snapshots/rates`; hosted vs legacy via `MT5_HOSTED_EXECUTION`. | VERIFIED (code) |
| L3 | Bridge tokens/auth | Inbound `GUVFX_AGENT_TOKEN` (no fallback, fail-closed); bridge→backend `GUVFX_WORKER_TOKEN` (legacy) or `GUVFX_WORKER_ID`+`SECRET` (hosted). :8788 != beta agent :8791. | VERIFIED (code) |
| L4 | Autostart launch | `C:\GuvFX\guvfx_autostart.bat` at logon; RX-3A guard idempotent. **Caveat:** repo guard targets an *older* portable path `C:\GuvFX\terminals\account_001\instance\...`, NOT the IS6 Program Files terminal — live launch config re-pointed outside Git (RULE 8). | VERIFIED (repo artefact); live batch = ASSUMED |
| L5 | Scheduled tasks / watchdog | Logon task re-runs autostart; bridge watchdog self-heals. Names `GuvFX_Autostart/LaunchMT5/SignalBridge/BridgeWatchdog` from 58-day memory. | ASSUMED (names); mechanism VERIFIED (docs) |
| L6 | Auto-logon | Host boots → autologon single console session (Session 1, Administrator) → logon task starts terminal+bridge. | VERIFIED (docs); registry specifics ASSUMED |
| L7 | Trade-ingest (Linux) | `guvfx-mt5-trade-ingest-worker` claims `PLACE_ORDER` jobs, POSTs :8788 via `GUVFX_WINDOWS_AGENT_BASE_URL/TOKEN`; `SYNC_POSITIONS`→`Trade`; cron `run_h1/h4/m5_scheduler`. | VERIFIED |
| L8 | Signal-copy strategies on acct#1 | `wayond` (asn#7, Telegram) + `ti_signals` (asn#8), AUTO_DEMO/LIVE; ti_signals 0.40/leg; TP-protection/auto-breakeven enqueue MODIFY/CLOSE via same bridge. | ASSUMED (DB/handoff/memory) |
| L9 | Wayond listener | `guvfx-wayond-listener` container, acquisition-only into `signal_intake`, provider UN-ARMED (E3 RED), never orders. | VERIFIED |
| L10 | ti_signals source | `backend/intelligence/ti_signals_source.py`, `telegram_source.py`. | VERIFIED (exist) |
| L11 | Legacy Guacamole VNC | `trading/terminal-access` → `GuacamoleVncAdapter` → `Mt5Instance.rdp_host`; conn family `mt5-terminal` (vs hosted `mt5-workspace-<uuid>`). Dead `10.50.0.2`; live `100.79.101.19:3389`. Viewer, not order path. | VERIFIED |
| L12 | Profile/account data | Legacy `config\accounts.dat` (1302561 login) + `bases\` history in the IS6 install. Never promote to golden (RULE 10). | VERIFIED (existence); state ASSUMED |
| L13 | Bridge log | `mt5_signal_bridge.log` on-host. | VERIFIED |
| L14 | Recovery dependency | Recovery = reboot host → autologon → logon task → terminal auto-logs-in from `accounts.dat` → bridge binds :8788. Killing the terminal loses the in-memory login (documented wrong move). | VERIFIED (docs) |

## 2. Dependencies

All live order execution for account #1 flows: Linux cron schedulers → create `PLACE_ORDER` → ingest worker
POSTs :8788 → legacy bridge attaches legacy Admin terminal → `order_send` on 1302561 → `SYNC_POSITIONS` →
`Trade`. `ti_signals`/`wayond` land through this exact path (ASSUMED). If legacy terminal/bridge stop,
autonomous trading for #1 silently halts (jobs FAIL — safe, no fills). Documented SPOF.

## 3. Contention — the 1302561 dual-login problem (the crux)

A broker account cannot be authenticated from two terminals at once. The legacy Admin terminal holds 1302561
continuously; the hosted portable runtime requires the *customer* to log 1302561 in inside the RemoteApp
(GuvFX never holds the password). Host probe 2026-08-10 observed the hosted portable journal
`authorization on IS6Technologies-Demo failed (Invalid account)` while the legacy terminal (pid 3972) held
1302561 — leading hypothesis **concurrent-login collision**. **Consequence: logging 1302561 OUT of the legacy
terminal is a PREREQUISITE for the hosted runtime to authenticate**, not post-migration cleanup. The
single-session fix (`fSingleSessionPerUser=1`) addressed *duplicate hosted* sessions — this legacy↔hosted
contention is separate and its clean positive control (hosted login after legacy logout, flushed `authorized`
in the portable journal) is still pending a Sponsor action.

## 4. Migration steps (ordered, non-destructive)

- **M0** Baseline & freeze (read-only): running legacy terminal, bridge :8788, ingest worker, asn#7/#8; PIDs,
  ports 8788/8791, container ids, `Trade`/`ExecutionJob` counts.
- **M1** Prove the hosted delivery path opens (owner-authenticated connect → `guvfx_u_1` portable runtime,
  `rdp_host=100.79.101.19`, single-session applied). No broker login yet.
- **M2** Stage the hosted-mode bridge for `guvfx_u_1` DARK: `MT5_HOSTED_EXECUTION=1`, `MT5_GUARDED_ATTACH=1`,
  `MT5_REQUIRE_IDENTITY_PIN=1`, `MT5_ALLOW_LIVE` unset, own `GUVFX_WORKER_ID/SECRET` (never legacy token).
  Supported service/task mechanism only (RULE 1), parse first (RULE 9). `HOSTED_MT5_EXECUTION_ENABLED` OFF.
- **M3** Backend hosted-workspace state for #1 (observation on): advance toward `EXECUTION_READY`; obtain
  customer ACK `workspace_confirmed_at`. Order authority stays with the live bridge gate.
- **M4** The contention swap (Sponsor-gated, one window): (1) log 1302561 OUT of the legacy terminal; (2)
  Sponsor logs 1302561 IN inside the hosted RemoteApp; (3) observe flushed `authorized` + 1302561 + balance
  in the *portable* journal (the outstanding RULE-11 positive control).
- **M5** Execution certification on the hosted path (demo, Nuno-manual): enable `HOSTED_MT5_EXECUTION_ENABLED`
  in the isolated cert scope, arm the workspace, Nuno performs ONE tiny demo PLACE+CLOSE; prove pre-send
  `verify_execution_binding` / `verify_mutation_identity`, wrong-account rejection, no-duplicate. Claude never
  places/closes/modifies.
- **M6** Cutover (only on green M5): leave the flag on for #1, re-point asn#7/#8 `PLACE_ORDER` claims to the
  hosted node-aware worker. Legacy bridge/terminal **stopped but retained** as rollback.
- **M7** Soak on the hosted runtime with legacy dormant-but-present.

## 5. Rollback

Reversible at every step before M6. Fastest: unset `HOSTED_MT5_EXECUTION_ENABLED` → gate returns
`RW_EXECUTION_FEATURE_DISABLED`; single-session `fSingleSessionPerUser` 1→0. Restore legacy: log 1302561 OUT
of hosted, back IN on legacy (or reboot → autologon self-heal, L14); re-point signal flow back. Nothing legacy
was deleted (L1–L7, L11 retained), so rollback = "swap the login back", never "rebuild a deleted component".

## 6. Cutover trigger

Authorized ONLY when M5 produces a success marker on a **demo** trade: `HOSTED_MT5_EXECUTION_ENABLED` provably
gates (OFF denies via `RW_EXECUTION_FEATURE_DISABLED`, `backend/execution/readiness.py:124-125`); layered
arming satisfied for #1; host-cert/capstone gates pass; Nuno's demo PLACE+CLOSE succeeds with correct binding,
wrong-account rejection, no-duplicate; prod blast radius = ZERO. Proposed marker
`EE_HOST_CERTIFIED — HOSTED_DEMO_EXECUTION_PROVEN`. Arming **real/live** orders is a separate Red authorization
after demo certification. A working legacy path is never retired on a passing test suite alone.

## 7. Deletion candidates vs must-remain

**Eventually disable/remove (only AFTER §6 cutover certified + soaked):** D1 legacy MT5 launch line +
logon task; D2 legacy-mode bridge :8788; D3 legacy watchdog; D4 legacy Guacamole VNC connection (+ dead
`10.50.0.2` refs); D5 archived bridge log.

**Must REMAIN until execution certification succeeds (do NOT touch):** R1 legacy Admin IS6 terminal + its
`accounts.dat`/`bases\` (the only live executor + rollback anchor; never a golden image, RULE 10); R2 bridge
:8788 + autostart/watchdog/auto-logon; R3 trade-ingest worker + cron schedulers (legacy routing) until M6;
R4 assignments asn#7/#8 (pausing them is the kill switch, not deletion); R5 Wayond listener + ti_signals
source (executor-independent).

## 8. VERIFIED vs ASSUMED (overall)

VERIFIED from repo: `mt5_signal_bridge.py` (PLACE_ORDER, :8788, `GUVFX_AGENT_TOKEN` no-fallback, legacy vs
hosted worker headers, attach-only, fail-closed hosted startup, identity-pin/binding gates); `rx3a_autostart_guard.ps1`;
ADR-0033/0034, host-cert/SINGLE_SESSION/DELIVERY docs, `readiness.py`, `flags.py`; legacy Guac adapter exists;
Linux ingest chain + SPOF; Wayond listener acquisition-only. ASSUMED (host/memory — verify on host): the live
legacy executor is the IS6 Program Files terminal in Admin session 1 holding 1302561; scheduled-task names;
asn#7/#8 binding + LIVE state; the 1302561 contention hypothesis + "legacy logout is prerequisite" (positive
control still pending Sponsor). **Notable discrepancy to reconcile before host action:** repo autostart
artefact points at an older portable path vs host-observed IS6 Program Files terminal — confirm the actual live
launch mechanism on the host (RULE 8).

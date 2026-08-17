# AJ#6.3 — Shape-3 capability-recovery certification: RESULT

**Date:** 2026-08-17 · **Subject:** support@ / TradingAccount 24 / `guvfx_u_24` on Node 2 (`guvfx-beta-node-1`).
**Merged code:** `main` @ `53aa774`. **Verdict:** **DEPLOY = PASS · SAFETY ARCHITECTURE = PASS ·
SHAPE-3 RECOVERY HAPPY-PATH = FAIL (real defect found).** Recovery edge **disarmed (DARK)** after the finding.

## 1. What deployed (all DARK-safe, verified)

- **Backend** rebuilt from `53aa774`, migrations `0008` (`execution_authorized_at`) + `0009`
  (`capability_recovery_*`) applied (additive `AddField`; existing rows NULL/0). Rollback image
  `guvfx-prod-guvfx-backend:rollback-preAJ63` (= `63ac3694`).
- **Frontend** built `GIT_COMMIT=53aa774`, `NEXT_PUBLIC_*` capability flags **omitted → DARK**; live
  `build-info gitCommit=53aa774`, both flags `false`; routes `/`, `/register`, `/onboarding/hosted`,
  `/accounts`, `/strategies/marketplace` all `200`; the verbatim *"Enable automated trading when you want
  GuvFX to begin executing your enabled strategies."* copy present in served bundle `bbb508ceee2fc22c.js`.
  Rollback image `rollback-preAJ63` (= `51df4fd3`).
- **Host** `RELAUNCH_TERMINAL` capability staged byte-identical to merged `main`
  (`Relaunch-GuvfxTerminal.ps1`, `primitive_runner.py`, `host_agent_dispatch.py`, `host_protocol.py`);
  RULE-9 ParseFile gate `PASS` (12/12 scripts, 0 failures); `py_compile` exit 0; executor restarted via
  `Restart-Service` (RULE 1) → `listening bind=100.79.101.19:8790`; runner `CONTRACT` wired
  `relaunch_terminal → Relaunch-GuvfxTerminal.ps1`; reserved-id floor `{1}`; manifest parity 12/12. Host
  rollback backups `*.preAJ63.bak` retained.

## 2. HARD GATE before arming (ADR-0047 live) — PASS

Live read-only proof on the deployed backend: `AUTOARM_candidates=0` (acct 24 not a candidate);
`READY_but_UNAUTHORIZED_rows=0`; `ANY_authorized=0`; `ANY_execution_enabled=0`; pure authz gate fires
(`workspace_execution_not_authorized`); running image contains all three gates (arm ×2, readiness ×2, auto-arm
authz filter ×1) + `capability_recovery.py` + migrations `0008`/`0009`. Reaching `EXECUTION_READY` **cannot**
auto-arm.

## 3. Shape-3 certification on account 24 — FAIL (LiveUpdate hijack)

Armed `HOSTED_CAPABILITY_RECOVERY_ENABLED=1`; the every-minute cron fired the edge at **11:07–11:08 UTC**
(cron log: `capability_recovery: reasserted config + relaunched account=24 (attempt 0)`; DB `rec_count=1`,
`rec_at=11:08:06`).

**What worked:** the runner correctly selected acct 24 as the sole candidate (CONNECTED + matched +
`trade_allowed=False`, CZ excluded); `apply_autotrading_config` re-asserted `common.ini [Experts]
AllowLiveTrading=1 / Enabled=1` (was `Enabled=0` — the AJ#6.2 revert); `relaunch_terminal` registered the
per-account close/relaunch tasks and dispatched them.

**What broke — the defect.** Ground truth from `C:\GuvFX\accounts\24\terminal\logs\20260817.log`:

```
07:18:52  '1302587': trading has been enabled - hedging mode      <- connected + trading, algo OFF (the target state)
11:08:11  Terminal exit with code 0                               <- graceful close worked
11:08:11  '1302587': disconnected from IS6Technologies-Demo
11:08:12  LiveUpdate start "...\liveupdate\terminal64.exe" /update /path:"C:\GuvFX\accounts\24\terminal" /portable
11:08:13  Terminal exit with code 0                               <- the UPDATER ran and exited; NO trading terminal
```

Launching `terminal64.exe /portable` with a **pending MetaTrader LiveUpdate** starts the **updater**
(`\liveupdate\terminal64.exe /update`), not the trading terminal. Net effect: the recovery **closed a healthy,
connected, trading-enabled terminal and did not bring it back** — regressing acct 24 from observable
(`obs applied=1`, `trade_allowed=False`) to `terminal_not_running` (`obs applied=0`). The observation-freshness
gate then removed the now-unobservable workspace from the candidate set, so **no runaway** — `rec_count` capped
at 1 (loop-safety held). The only `terminal64.exe` for `guvfx_u_24` is now the LiveUpdate remnant (pid 10600,
cmd `...\liveupdate\... /update`), not a trading terminal.

**Root cause:** `Relaunch-GuvfxTerminal.ps1` is **not LiveUpdate-safe**. Relaunch must detect a pending
LiveUpdate and complete it (or launch by a path that bypasses `/update`), then verify the *trading* terminal
came back connected — never treat a `liveupdate\terminal64.exe /update` process as a recovered terminal.

## 4. Safety architecture held throughout — PASS

- `execution_authorized_at = NULL`, `execution_enabled = False`, `ARMED = False`, `auto_arm_suppressed = True`
  for acct 24 the entire time. **Zero** `ExecutionJob`, **zero** `StrategyAssignment`, **zero** orders.
- Globally: `any_authorized = 0`, `any_armed = 0`.
- **Customer Zero + account 18 byte-identical**: CZ Golden `STRUCTURAL_SHA256 =
  b57182b4bc0295350bda810705267be85a3df682d60097ddd818629ba5609e61` (AFTER == BEFORE). CZ terminal (pid 7812,
  session 3) and acct 18 (pid 11768, session 4) untouched.
- The `RELAUNCH_TERMINAL` primitive only ever acted on `guvfx_u_24`'s own terminal; CZ refused at four layers.

## 5. Actions taken to make prod safe

- **Disarmed** `HOSTED_CAPABILITY_RECOVERY_ENABLED` — `beta.env` restored **byte-identical** to the pre-arm
  backup (`beta.env.preAJ63.bak`); armed copy kept as `beta.env.aj63-armed-attempt`; backend recreated; flag
  now reads `False`. The edge is DARK; it will not fire again.
- All other AJ#6.3 code stays deployed and DARK (authorization endpoint, RELAUNCH primitive, read-model
  fields). No customer authorization occurred; the "Enable automated trading" control is not shown for acct 24
  (correct — it is not `EXECUTION_READY`).

## 6. Consequence to disclose

Account 24's demo trading terminal is **down** (LiveUpdate remnant only) as a direct result of the armed
recovery relaunch. No orders, no customer impact (support@ is our test account), CZ unaffected. Restoring it is
a support@ **RemoteApp reconnect** (relaunches MT5; the LiveUpdate having run, the next launch should be the
trading terminal). **Do not re-arm capability recovery until the LiveUpdate-safe fix ships.**

## 7. Recommended fix (next packet)

Make the Shape-3 relaunch LiveUpdate-safe: before the relaunch, detect a pending LiveUpdate (there is prior
art in `apply_liveupdate_containment`), complete or contain it, then launch the trading terminal and **verify**
the reappeared process is the trading terminal (cmd line has **no** `\liveupdate\`, `/update`) and reaches
`trade_allowed=True` before reporting success. Add a fail-closed guard: if the reappeared terminal is the
updater, report `relaunch_hit_liveupdate` (don't count it as recovered) and leave `trade_allowed` untouched.

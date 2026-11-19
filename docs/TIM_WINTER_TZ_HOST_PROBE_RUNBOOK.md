# Runbook — TIM winter broker-timezone host probe

**Tool:** `scripts/tim_winter_tz_probe.py`
**Packet:** GFX-TIM-WINTER-TIMEZONE-HOST-PROBE-009
**Status:** repository tooling only — NOT installed, NOT scheduled, NOT deployed, NOT run by any agent. A human operator runs it on the bridge host under a separate Sponsor GO.
**Outcome (2026-11-19):** the isolated on-host observation (ADR-0121 path, via the dormant `account_001` slot — the `:8788` terminal is a protected live Taurex-LIVE session) was run by a human operator and **PASSED**. Winter is now **`TIM_WINTER_TIMEZONE_VERIFIED`** (`TradersWay-Demo = UTC+2 / 7200 s`, winter-season- and single-observation-bound). Evidence: `docs/evidence/broker_timezone_evidence_v2_winter.md`; canonical value-blind record `tests/fixtures/tim_winter_tz/winter_tz_observation_2026-11-19.json`.

## Purpose

Complete the winter broker-server timezone observation that could not be done from the control node (see `docs/evidence/broker_timezone_evidence_v2_winter.md`). The read must happen **on the Windows MT5 bridge host**, where `GUVFX_AGENT_TOKEN` is already provisioned and the bridge answers on loopback, **without any agent handling the credential**.

## Governance boundary

This tool introduces no new authority and no new network surface. It implements the already-governed, read-only timezone-evidence method of `docs/evidence/broker_timezone_evidence_v1.md` under the producer boundary of **ADR-0070 / ADR-0076**; no new ADR is required. It does **not** weaken the bridge authentication boundary and adds **no** unauthenticated endpoint. It has no trading, order, history-export, file-copy, or NAS capability and cannot modify MT5 or broker state.

## Security contract (enforced in code + unit-tested)

- Reads `GUVFX_AGENT_TOKEN` **only in-process** via `os.getenv`; never echoes, serializes, hashes, logs, or places it in argv or a child-process command line. There are no child processes. There is **no `--token` argument**.
- Calls **loopback only** (`127.0.0.1` / `localhost` / `::1`); any other target is refused.
- Fails closed on: token unavailable, endpoint unreachable, 401/403, malformed payload, missing/ambiguous trade mode, non-DEMO account, wrong server, fewer/more than three completed bars, open/incomplete bar, independent UTC unavailable, inconsistent offsets, DST-boundary ambiguity.
- Emits **value-blind** JSON only (allow-listed keys): DEMO classification, approved server label, three completed-bar server epochs, independent UTC epochs, per-sample offsets and residuals, consistency verdict, correlation id, observation time. Never login, account number, token, balance, equity, currency, positions, orders, or message bodies.

## Method

1. Confirm DEMO via `GET /mt5/snapshots/account` (`trade_mode == 0`) and that the connected server equals the approved `--expect-server` label.
2. `GET /mt5/snapshots/rates?symbol=EURUSD&timeframe=M1&count=4`; the currently-forming bar (`data[-1]`) is **excluded**, leaving exactly three **completed** M1 bars. Reject unless they are at 60-second cadence and the forming bar is contiguous.
3. Capture an independent UTC timestamp (host NTP-synced clock) tightly around each read.
4. Offset = round((latest-completed-bar-end − independent-UTC) / 3600) × 3600, whole hours; residual (freshness/skew) must be within 180 s; offset within [−12h, +14h].
5. Repeat for `--samples` independent observations (default 3); **all must agree** or the result is `CONFLICT`.
6. If the observation is within one hour of a EU DST transition, the result is `INCONCLUSIVE` (DST ambiguity).

## Preconditions (operator confirms)

- The bridge host clock is NTP-synced.
- The market is open and it is after the DST transition for the season being verified (winter: after the late-October transition).
- The terminal on the bridge is connected to the approved TradersWay-Demo producer instance.

## Operator steps (Sponsor-gated; run on the bridge host)

Dry-run / preflight (safe; no read, no credential access) — this is the default:

```
python scripts\tim_winter_tz_probe.py --expect-server TradersWay-Demo
```

Real read (only under a bounded Sponsor GO, on the bridge host):

```
set TIM_WINTER_TZ_PROBE_ON_HOST=1
python scripts\tim_winter_tz_probe.py --expect-server TradersWay-Demo --execute
```

Return **only** the printed value-blind JSON. Do not copy the token, any account number, or any balance/position/order data. The offset is then recorded as a v2 (winter) `broker_timezone_evidence` entry.

## Opaque launcher (GFX-TIM-WINTER-TZ-OPAQUE-LAUNCHER-011)

`deploy/tim-winter-tz/run_winter_tz_probe.bat` is the reviewed operator launcher. It
exists because `GUVFX_AGENT_TOKEN` is not a standing environment variable — the bridge
injects it into its own process only at launch by `call`-ing
`C:\GuvFX\secrets\bridge.tokens.bat`, so a plain SSH/RDP session never has it (see
`docs/evidence/broker_timezone_evidence_v2_winter.md`). The launcher reuses that **same
governed loader** to populate its own process environment **opaquely**: it never reads or
prints the secrets file and never types, pastes, echoes, copies, persists, or reconstructs
the token. It accepts no arguments, uses fixed paths only, hard-codes the DEMO server, and
fails closed. The real execution is still Sponsor-gated and run by a human on the bridge
host — no agent handles the token.

**Fixed paths the launcher uses (installed, not repo):**

- secrets loader (pre-existing, governed, ACL SYSTEM+Administrators): `C:\GuvFX\secrets\bridge.tokens.bat`
- installed probe: `C:\GuvFX\TIM\winter-tz-probe\tim_winter_tz_probe.py`

### 1. Stage (one-time, Sponsor-gated; Administrator console on the bridge host)

```
mkdir C:\GuvFX\TIM\winter-tz-probe
copy <repo>\scripts\tim_winter_tz_probe.py        C:\GuvFX\TIM\winter-tz-probe\tim_winter_tz_probe.py
copy <repo>\deploy\tim-winter-tz\run_winter_tz_probe.bat C:\GuvFX\TIM\winter-tz-probe\run_winter_tz_probe.bat
```

### 2. Verify the staged bytes against the repository (value-blind)

```
powershell -NoProfile -Command "Get-FileHash -Algorithm SHA256 C:\GuvFX\TIM\winter-tz-probe\tim_winter_tz_probe.py, C:\GuvFX\TIM\winter-tz-probe\run_winter_tz_probe.bat | Format-List"
```

Confirm each hash equals the `sha256:` value recorded for that file in
`evidence/manifests/GFX-EVD-TIM-WINTER-TZ-OPAQUE-LAUNCHER-011.json`. If any hash differs,
**stop** — do not run.

### 3. Execute exactly once (Administrator; the ACL on the loader requires it)

```
C:\GuvFX\TIM\winter-tz-probe\run_winter_tz_probe.bat
```

The launcher loads the token opaquely, sets the on-host confirmation internally, and runs
the probe once. Return **only** the printed value-blind JSON (DEMO classification, approved
server label, three completed-bar epochs, independent UTC epochs, per-sample offsets and
residuals, consistency verdict, correlation id, observation time). Copy no token, account
number, balance, position, or order data. Launcher exit codes: `10` argument supplied,
`11` loader absent, `12` probe absent, `13` python unavailable, `14` token unavailable
after the loader; otherwise the probe's own exit code.

### 4. Rollback

The launcher writes nothing persistent. To remove the staged tooling:

```
rmdir /s /q C:\GuvFX\TIM\winter-tz-probe
```

No task, service, registry, environment variable, or bridge state is changed by staging,
running, or rolling back.

## Isolated observation path (GFX-TIM-ISOLATED-WINTER-TZ-PATH-013 / ADR-0121)

Use this path when the default `:8788` bridge is bound to a **live** terminal (e.g. a
`Taurex-LIVE` guarded session) and must not be touched. It stands up a separate, ephemeral,
loopback-only, read-only bridge against the dormant `C:\GuvFX\terminals\account_001\instance`
TradersWay-Demo slot on fixed port **`127.0.0.1:8977`**, and never touches `:8788`, the
Program-Files terminal, or any live tenant.

### 1. Stage (one-time, Sponsor-gated; Administrator console on the bridge host)

Copy the reviewed files into the TIM staging directory (the probe may already be staged):

```
copy <repo>\scripts\mt5_signal_bridge.py                 C:\GuvFX\TIM\winter-tz-probe\mt5_signal_bridge.py
copy <repo>\scripts\tim_isolated_bridge_ctl.py           C:\GuvFX\TIM\winter-tz-probe\tim_isolated_bridge_ctl.py
copy <repo>\scripts\tim_winter_tz_probe.py              C:\GuvFX\TIM\winter-tz-probe\tim_winter_tz_probe.py
copy <repo>\deploy\tim-winter-tz\start_tim_isolated_bridge.bat      C:\GuvFX\TIM\winter-tz-probe\start_tim_isolated_bridge.bat
copy <repo>\deploy\tim-winter-tz\run_winter_tz_probe_isolated.bat   C:\GuvFX\TIM\winter-tz-probe\run_winter_tz_probe_isolated.bat
copy <repo>\deploy\tim-winter-tz\stop_tim_isolated_bridge.bat       C:\GuvFX\TIM\winter-tz-probe\stop_tim_isolated_bridge.bat
```

Verify every staged file's SHA-256 against
`evidence/manifests/GFX-EVD-TIM-ISOLATED-WINTER-TZ-PATH-013.json`:

```
powershell -NoProfile -Command "Get-FileHash -Algorithm SHA256 C:\GuvFX\TIM\winter-tz-probe\mt5_signal_bridge.py, C:\GuvFX\TIM\winter-tz-probe\tim_isolated_bridge_ctl.py, C:\GuvFX\TIM\winter-tz-probe\start_tim_isolated_bridge.bat, C:\GuvFX\TIM\winter-tz-probe\run_winter_tz_probe_isolated.bat, C:\GuvFX\TIM\winter-tz-probe\stop_tim_isolated_bridge.bat | Format-List"
```

If any hash differs, **stop**.

### 2. Confirm the demo slot (owner)

Ensure the `account_001` terminal is logged into a valid **TradersWay-Demo** DEMO account (relog /
refresh the demo via the terminal GUI if it expired). Leaving it running logged-in is fine — the
start launcher will attach to it without owning it.

### 3. Start, observe once, stop (Administrator)

```
C:\GuvFX\TIM\winter-tz-probe\start_tim_isolated_bridge.bat
C:\GuvFX\TIM\winter-tz-probe\run_winter_tz_probe_isolated.bat
C:\GuvFX\TIM\winter-tz-probe\stop_tim_isolated_bridge.bat
```

The start launcher proves the isolated bridge binds **only** `127.0.0.1:8977` and that `:8788` is
unchanged, then emits value-blind JSON. Return **only** the probe's value-blind JSON from the run
step. The stop launcher terminates **only** the bridge/terminal PIDs it created (each after a
live-identity re-check) and confirms `:8788` is untouched. Launcher exit codes: `10` argument /
`11` loader absent / `12` controller or probe absent / `13` python unavailable / `14` token
unavailable.

### 4. Rollback / teardown

`stop_tim_isolated_bridge.bat` is the teardown. If the start partially failed it rolls back the
newly created resources itself. To remove the staged tooling afterwards:
`rmdir /s /q C:\GuvFX\TIM\winter-tz-probe`. `:8788`, the Program-Files terminal, and every live
tenant are never started, stopped, relogged, repointed, or mutated.

## What this runbook does NOT authorise

No host deployment of the tool as a service; no scheduled task; no bridge restart; no order/trade; no NAS mount; no dataset publication; no signing key; no GO #2. The real read is a one-shot, operator-run, read-only observation under its own Sponsor GO.

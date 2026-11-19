# Broker-Server Timezone Evidence v2 — winter (VERIFIED 2026-11-19; earlier attempts retained)

**Packet:** GFX-TIM-WINTER-TIMEZONE-VERIFICATION-008 (attempt) → GFX-TIM-WINTER-TIMEZONE-HOST-PROBE-009 (mechanism) → GFX-TIM-ISOLATED-WINTER-TZ-PATH-IMPLEMENTATION-013 (isolated path, ADR-0121) → GFX-TIM-WINTER-TZ-EVIDENCE-RECONCILIATION-013 (verified observation reconciled into Git)
**Probe date:** 2026-11-18 (control-node attempt, NOT_VERIFIED) → **2026-11-19 (isolated on-host observation, VERIFIED)**
**Authority:** Sponsor-approved, strictly read-only broker-timezone observation; HARD STOP includes **no credential handling** and **no host deployment / real broker read**
**Current verdict (2026-11-19):** **`TIM_WINTER_TIMEZONE_VERIFIED`** — `TradersWay-Demo = UTC+2 / 7200 s` (winter-season- and single-observation-bound), established by the isolated on-host observation recorded in the next section. This **supersedes** the earlier `TIM_WINTER_TIMEZONE_NOT_VERIFIED` verdict of the 2026-11-18 control-node attempt, which is **retained (not erased)** in the sections below.

## VERIFIED winter observation — 2026-11-19 (isolated path; supersedes the NOT_VERIFIED attempts below)

A human operator logged the dormant `account_001` TradersWay-Demo slot into a valid DEMO over RDP and ran the reviewed, merged isolated launchers (`deploy/tim-winter-tz/`, ADR-0121, main `c649c550`) in that **one interactive session**. The isolated, **loopback-only** bridge read three completed M1 bars once; the bridge was then torn down. The original `127.0.0.1:8788` terminal proved to be a **live `Taurex-LIVE` / `trade_mode=2`** guarded session, so it was **never** touched — a dedicated isolated `account_001` path was used instead. The result was validated against the probe contract and the host independently re-confirmed clean. The canonical value-blind record is `tests/fixtures/market_data/tim/winter_tz_observation_2026-11-19.json`.

### Value-blind evidence (observation, `tim-winter-tz-probe/1.0`)

- **Observation time:** 2026-11-19T13:57:03Z (independent NTP-synced UTC).
- **Classification:** `DEMO`; **assessment:** `VERIFIED`; **server:** `TradersWay-Demo`.
- **Completed M1 bars (server epochs):** `1795103640`, `1795103700`, `1795103760` — **exactly three**; 60 s cadence; the forming bar is excluded.
- **Independent UTC epochs:** `1795096621`, `1795096622`, `1795096623`.
- **Implied offsets:** `[7200, 7200, 7200]` s; **residuals:** `[1, 2, 3]` s (within the ±180 s freshness/skew bound).
- **Consistency:** `consistent: true`, `offset_seconds: 7200`, `samples: 3`; **offset_hours:** `2`.
- **Correlation id:** `tim-winter-tz-1795096623`.
- **No** login, account number, balance, position, order, or token value appears in the evidence.

### Isolation and teardown proof

- **Start:** loopback-only bind `127.0.0.1:8977`; protected `:8788` (production Taurex-LIVE) **unchanged**; the already-running demo terminal was **attached, not owned** (`terminal_created_by_us: false`).
- **Stop:** isolated bridge stopped; temporary listener gone; protected `:8788` **unchanged**.
- **Independent post-check (read-only):** port `8977` free; **no** TIM controller / probe / bridge process; controller state file absent; protected `:8788` still a **single listener** owned by **PID 10272** (the exact control captured before the observation).

## What the 2026-11-18 attempt established (value-blind)

- **Observation time:** 2026-11-18T10:17:41Z (control-node, NTP-synced UTC).
- **Independent UTC source:** control-node system clock, NTP-synced — **available** (this was *not* the blocker).
- **Demo server identity:** **NOT re-established.** The bridge exposes `account_login / account_server / trade_mode` only inside the **authenticated** `/mt5/snapshots/account` payload; the unauthenticated surface does not expose identity. Confirming "DEMO not LIVE" from broker truth therefore requires the credentialed call.
- **Sample count:** **0 bars.** The authenticated `/mt5/snapshots/rates` endpoint was **not** called — doing so requires `GUVFX_AGENT_TOKEN`, i.e. credential handling, which packet -008 forbids.
- **Observed offset:** none. **Agreement result:** N/A (0 samples).
- **Identifiers:** no login, account number, order id, or token value was requested, handled, displayed, or stored.

## Method and controls (read-only, no credential sent)

- Post-DST timing confirmed: 2026-11-18 is after the 2026-10-25 EU DST transition, FX market open — so the October time-gating is cleared.
- **CONTROL 1 (reachability):** TCP connect to the Windows MT5 host bridge port from the control node → **CLOSED / UNREACHABLE**; an unauthenticated HTTP GET to `/mt5/snapshots/rates` returned status `000` (no response). (The 2026-10-24 probe, which reached the port and received `401`, ran from the bridge host itself, not the control node.)
- **CONTROL 2 (auth wall, from source):** `scripts/mt5_signal_bridge.py` — the OHLC endpoint auth is `GUVFX_AGENT_TOKEN` with **no fallback**; the bridge "will not start unauthenticated." Reading bars is impossible without the token.

## Why the 2026-11-18 control-node attempt could NOT verify the winter offset (historical — superseded 2026-11-19)

*Historical: this section explains the 2026-11-18 control-node attempt only. It was **superseded on 2026-11-19** by the VERIFIED isolated observation recorded above; the current verdict is `TIM_WINTER_TIMEZONE_VERIFIED`.*

Three independent blockers applied to that control-node attempt, each sufficient on its own:

1. **Bar source unreachable** from the authorised read context (bridge port closed from the control node).
2. **Only bar path is credentialed** (`GUVFX_AGENT_TOKEN`); reading it from the control node is credential handling, barred by packet -008.
3. **DEMO-not-LIVE reconfirmation** requires that same authenticated payload (`trade_mode`); it cannot be done credential-free (the v2 pre-transition evidence recorded this gap, and it persists).

There is **no credential-free substitute**: a third-party public EURUSD feed carries its vendor's clock, not the approved broker's server time, so it cannot establish *this* broker's winter offset.

## Resolution mechanism (repo-only; run on 2026-11-19 via the isolated path — see the VERIFIED section above)

The correct completion path is to run the read **locally on the bridge host**, where the token is already provisioned and the bridge is reachable on loopback, **without any agent handling the credential**. Packet -009 adds exactly that mechanism as repository tooling:

- `scripts/tim_winter_tz_probe.py` — a host-local operator wrapper that reads `GUVFX_AGENT_TOKEN` **only in-process**, calls the **loopback** bridge, classifies DEMO fail-closed, selects exactly three **completed** M1 bars (the forming bar is excluded), captures an independent UTC per sample, computes the whole-hour offset, requires the samples to agree, and emits **value-blind** JSON (no login/token/balance/position/order). It defaults to **dry-run**; a real read requires `--execute` **and** an on-host confirmation variable, and is gated behind a separate Sponsor GO.
- `tests/test_tim_winter_tz_probe.py` — synthetic-only unit tests (DEMO/LIVE/ambiguous, token isolation, loopback-only, 401/403, malformed payload, bar-count/open-bar/cadence, inconsistent offsets, independent-UTC-unavailable, DST ambiguity, value-blind output, no-mutation capability, and a summer UTC+3 positive control).

This wrapper is **not installed, not scheduled, not deployed, and not run by any agent**. It performs no broker read in this packet.

## Status of the mapping (updated 2026-11-19)

- Summer **UTC+3 (EEST)** remains VERIFIED **for its observed season only** (`broker_timezone_evidence_v1.md`, 2026-07-01). Unchanged by this update.
- Winter **UTC+2** is now **VERIFIED for its observed season only** by the 2026-11-19 isolated observation above (`TradersWay-Demo = UTC+2 / 7200 s`, single-observation-bound).
- The winter UTC+2 and summer UTC+3 results are **two independent single-season observations** that happen to match the standard EET/EEST pattern; the year-round switching rule itself is **not** evidenced by these two samples. **Do not** generalise either seasonal observation into an unconditional year-round rule, normalise across the DST boundary from a single sample, or run a broad backfill. Each season's release binding uses its own `timezone_evidence`; a winter release binding now has a VERIFIED winter observation to cite.

## Safety boundary observed

No order placement/modification/cancellation; no credential requested/handled/displayed/stored; no login or account change; no history export; no dataset publication; no NAS mount; no signing key; no `GUVFX_DATA_ROOT` change; no GO #2 or first release; no host deployment and no real broker read; no production-data write. Only unauthenticated reachability controls were sent, from the control node.

## Bounded next action — COMPLETED 2026-11-19

This action was **completed on 2026-11-19**. Because the original `127.0.0.1:8788` terminal proved to be a live `Taurex-LIVE` / `trade_mode=2` session, the run used the dedicated **isolated** `account_001` path (ADR-0121) rather than the `:8788` bridge: a human operator logged `account_001` into TradersWay-Demo over RDP and ran the reviewed isolated launchers, which returned a passing value-blind JSON result (DEMO classification, approved server label, three completed-bar epochs, independent UTC epochs, per-sample offsets, consistency verdict, correlation id, observation time). The offset is recorded in the **VERIFIED winter observation** section above; winter is now `TIM_WINTER_TIMEZONE_VERIFIED` (`UTC+2 / 7200 s`, season- and observation-bound).

## Launch-context root cause and opaque launcher (GFX-TIM-WINTER-TZ-TOKEN-CONTEXT-DIAGNOSIS-010 / GFX-TIM-WINTER-TZ-OPAQUE-LAUNCHER-011)

A first on-host `--execute` attempt (2026-11-18) **failed closed before any network call**: `GUVFX_AGENT_TOKEN` was absent from the operator's SSH PowerShell process and the Machine environment, so the probe refused (no broker read, no DEMO classification, no bars, no credential handled). Value-blind diagnosis (repo evidence only; secrets file contents **not** read — `docs/SECRET_INVENTORY.md`, `backend/terminal_provisioning/windows/rx3a_autostart_guard.ps1`, `deploy/node2-order-bridge/start_node2_bridge.bat`): the token is **not a persistent Machine/User variable**. It lives in the governed, ACL-restricted loader `C:\GuvFX\secrets\bridge.tokens.bat` (SYSTEM+Administrators) and is injected into the bridge process **only at launch**, when `C:\GuvFX\guvfx_autostart.bat` `call`s that loader before starting `terminal64.exe /portable` and `python mt5_signal_bridge.py` on `:8788`. An interactive session never inherits it.

Resolution (repo-only, this packet): `deploy/tim-winter-tz/run_winter_tz_probe.bat` — a reviewed, ASCII-only launcher that reuses the **same governed loader** to populate its own process environment **opaquely** (no typing/pasting/echoing/copying/persisting/reconstructing of the value; the secrets file is never read or printed), verifies token presence value-blindly, sets the on-host confirmation internally, and runs the installed probe `C:\GuvFX\TIM\winter-tz-probe\tim_winter_tz_probe.py --execute --expect-server TradersWay-Demo`. It accepts no arguments, uses fixed paths, hard-codes the DEMO server, fails closed (codes 10–14), propagates the probe exit code, and has no trading / order / withdrawal / NAS / dataset / persistence / task / service / process-control capability. Synthetic-only tests: `tests/test_tim_winter_tz_launcher.py` (wired into `make governance-check` and the CI governance job); real runtime behaviour proved by a windows-latest synthetic gate using a neutral synthetic token and a stub probe. Staging, hash verification, one-shot execution, value-blind JSON return, and rollback are in `docs/TIM_WINTER_TZ_HOST_PROBE_RUNBOOK.md`. The launcher was **not installed, not staged, not run** in that packet; winter stayed **`TIM_WINTER_TIMEZONE_NOT_VERIFIED`** at that time. **(Superseded 2026-11-19:** a Sponsor-gated on-host run via the isolated `account_001` path returned a passing value-blind result — see the **VERIFIED winter observation** section above; the current verdict is `TIM_WINTER_TIMEZONE_VERIFIED`.)

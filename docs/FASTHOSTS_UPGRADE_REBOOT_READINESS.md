# FASTHOSTS_UPGRADE_REBOOT_READINESS — cold-boot unattended-recovery certification

**Date:** 2026-09-30 · **Packet:** REBOOT-RECOVERY P0–P5 + Sponsor-authorized deployment · **Reviewer:** pending-Sponsor
**Codebase:** all of P0–P5 merged to `main` (P0 `4da49d5`, P1 `216142a`, P2 `92114f1`, P3 `2ab679b`, P4-prep `2ab7d65`, P4-a `cfa25e3`, P4-b `b41cd1c`, P4-c `caff68d`, P5 `244f1ba`). Prod at `f2c41ca`.

## VERDICT: **`FASTHOSTS_UPGRADE_REBOOT_READY` (deployment + subsystem) = GREEN — but CONDITIONAL on a fresh, authoritative, immediately-pre-reboot flat-estate gate. This is NOT permission to reboot at the present moment.**

The three deployment blockers are **CLEARED** and probe-only recovery is **production-certified**. Every subsystem-readiness gate is GREEN. **However, the Fasthosts reboot is NOT authorized while the live estate has open exposure.** `FASTHOSTS_UPGRADE_REBOOT_READY` here certifies that the recovery *system* is deployment-ready — it does **not** authorize a reboot now. The reboot requires a fresh, authoritative **immediately-pre-reboot exposure gate** (§5A) to pass **at that moment**, plus enabling `HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED` immediately before, plus explicit Sponsor reboot authorization. The estate is a live trading system and is **not flat now** (25/35/36 hold open positions — normal strategy activity). **We wait for natural strategy management to leave the estate flat; trades are never closed/altered/manufactured to obtain the window.** Self-connect remains OFF; nothing was rebooted.

---

## 1. Deployment executed (2026-09-30, under Sponsor authorization)

| Step | Action | Result |
|---|---|---|
| 1 | Deploy P4/P5 backend to prod **DARK** (both flags OFF), migrate | ✅ image rebuilt; migration 0014 applied zero-downtime (one-off on new image, old backend serving) then `guvfx-backend` recreated `--no-deps --force-recreate` (never `--remove-orphans`); reconciler flags now present + OFF; 25/35/36 untouched; all other containers undisturbed. |
| 2 | Deploy PROBE_SESSION to the host, safe ordering | ✅ `Probe-GuvfxSession.ps1` staged FIRST (checksum-matched + **PARSE_OK** under Win PS 5.1); then `host_protocol.py`/`host_agent_dispatch.py`/`primitive_runner.py` deployed (checksums matched); dry-run **VERIFY_SCRIPTS_OK + contract parity True** BEFORE restart; `GuvFXHostedExecutor` restarted (only that service) → Running. **Real read-only probes: 25→DISCONNECTED (sid 7), 35→DISCONNECTED (sid 14), 36→DISCONNECTED (sid 15)** — states correspond to reality (terminals running in disconnected RDS sessions). |
| 3 | Install the P5 reconciler schedule | ✅ `*/5 * * * * … run_hosted_session_reconciler` cron installed (log pre-created `ubuntu:ubuntu`); DARK no-op verified while the flag was OFF ("disabled … no-op"). Cadence `*/5` documented in §3. |
| 4 | **No** experimental prod-guacd/self-connect | ✅ none performed. Read-only prod Guacamole compat only: base HTTP 200, `/api/languages` HTTP 200 (REST reachable) — no token minted, no session created. |
| 5 | Enable **`HOSTED_SESSION_RECONCILER_ENABLED` only** + certify probe-only | ✅ enabled in `hosted-executor.env` (backup `…preSESSIONRECON`; `ARM_SELFCONNECT` deliberately absent), backend recreated. **Probe-only cert (2 idempotent cycles):** `recon_enabled=True, arm=False`; 25/35/36 (fresh) → `skipped_healthy`; one genuinely-stale workspace → correctly classified **DISCONNECTED** via real PROBE_SESSION and **`skipped_not_armed`** (decided-reconnect, **zero self-connect, zero decryption, no claim burned**); `established=0 reconnected=0 errors=0`; nothing created/altered; 25/35/36 `session_recovery_count=0`. |

## 2. Fresh gates (2026-09-30, read-only)

- **Bridge-health — GREEN:** `8788`/`8789`/`8804`/`8805` all LISTENING; 6 `terminal64`; 11 `guvfx_u_*` sessions.
- **Identity / strategy / magic / sizing — GREEN (untouched):** 25 magic 1000000010 (asn 10); 35 magic 1000000016 (asn 16, leg_sizing 6); 36 magic 1000000019 (asn 19, leg_sizing 9); all persistent_workspace demo active. `session_recovery_count=0` for all three (recovery never touched them).
- **Recovery-readiness — GREEN:** deployed + probe-only production-certified; arming machinery proven (host executor armed + resolves, GUAC configured, `delivery_ready`, `workspace_node` present + node-agree).
- **Flat-estate — WAIT (reboot-time gate):** currently NOT flat — 25=3, 35=3, 36=2 open positions (live strategy trading; fluctuates). The reboot must occur in a flat window (0 open, 0 active plans).

## 3. Recovery sequence / timing / backoff / concurrency / failure states / audit / rollback

Recovery sequence, timing (cadence `*/5`; staggering `MAX_ESTABLISH_PER_CYCLE=4`; cooldown `RECOVERY_COOLDOWN_S=300`; `MAX_RECOVERY_ATTEMPTS=3`), per-tenant isolation, failure states (`UNKNOWN`/`ok:false`/transport → fail-closed; establish fail → bounded retry → CRITICAL alert; `node_divergence` fail-closed), audit (per-attempt `OperationalEvent` `workspace.session_recovery` secret-free + `AlertEvent` + `operations_summary._session_recovery_block`), and disable/rollback (two independent flags OFF) are as previously specified. **Cadence `*/5` rationale:** after boot a session is ABSENT → stale within 300 s → the next `*/5` tick establishes it; 25/35/36 (3 candidates < cap 4) recover in ~one pass (≈5–10 min); the future 20-account estate = ⌈20/4⌉ = 5 passes ≈ 25 min worst case. Tunable via the cron cadence + `MAX_ESTABLISH_PER_CYCLE`.

## 4. Arming-prerequisite status — ALL MET

| Prerequisite | Status |
|---|---|
| P4-c/P5 governed tests + adversarial certification | ✅ met |
| Arming machinery in prod (executor armed + resolves; GUAC; `delivery_ready`; `workspace_node`+node-agree) | ✅ met (fresh) |
| `Probe-GuvfxSession.ps1` host ParseFile (RULE 9/11) | ✅ met (staged, PARSE_OK) |
| `accounts.dat` broker auto-reconnect (25/35/36) | ✅ met |
| **Backend P4/P5 deployed to prod** | ✅ **cleared** (deployed DARK) |
| **PROBE_SESSION host-op deployed** (`verify_scripts`/parity green; real probes accurate) | ✅ **cleared** |
| **Session-reconciler cron scheduled** | ✅ **cleared** (`*/5`, DARK-verified) |
| **Probe-only production certification** | ✅ **met** (2 idempotent cycles; zero self-connect/decryption; nothing altered) |
| End-to-end probe→establish→session against real prod guacd | ⏳ at the **supervised reboot** (its purpose; no prod spike; feasibility = 12/12 harness) |

## 5A. Authoritative immediately-pre-reboot exposure gate (must PASS at reboot time)

A fresh gate re-run **at the moment of the reboot** — not at cert time. It must show, for 25/35/36:
- **Zero broker positions/exposure** — BROKER-authoritative (live per-tenant bridge `/mt5/positions` snapshot), NOT the ingested GuvFX `Trade` rows. **Reconcile stale GuvFX records against live broker state** (a stale open `Trade` whose broker position is closed is reconciled, never treated as authoritative exposure).
- **Zero pending / RUNNING execution jobs** (`ExecutionJob` in a non-terminal state for these accounts).
- **No active management plans requiring host management** (`SignalExecutionPlan` PLANNED/PROMOTED; TP-protection ladders; any in-flight MODIFY/close orchestration).
- **Reconfirm invariants:** bridges LISTENING (8788/8789/8804/8805); host executor armed + resolves; PROBE_SESSION deployed (verify_scripts/parity green); reconciler schedule present; Guacamole reachable (read-only); identity/server/magic/sizing unchanged (magic 1000000010/16/19); recovery flags = reconciler ON, self-connect OFF.

**The window is obtained by WAITING for natural strategy management to flatten the estate — trades are never closed, altered, or manufactured to force it.** Only when this gate genuinely passes does the reboot proceed.

## 5. Exact final flag-change + supervised reboot procedure (Sponsor-executed; STOP is here)

1. **Wait for the §5A gate to pass genuinely** (natural flat estate; do not force).
2. **Arm self-connect immediately before the reboot:** add `HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED=1` to `/home/ubuntu/guvfx-prod/hosted-executor.env`, then `cd /home/ubuntu/guvfx-prod && docker compose up -d --no-deps --force-recreate guvfx-backend` (never `--remove-orphans`). Verify `hosted_session_reconciler_arm_selfconnect_enabled()==True` (reconciler flag is already ON). Target config now = reconciler ON + self-connect ON.
3. **Fasthosts performs the cold-boot/upgrade.** Do **NOT** manually open 25/35/36.
4. **Automatic recovery (the certification event):** Administrator autologon restores CZ + per-tenant bridges; per-tenant `/portable` terminals do not auto-recover → sessions ABSENT → stale within 300 s → the `*/5` reconciler (ON + armed) probes → ABSENT → establishes via guacd self-connect (≤4/pass) → `/portable` auto-launches → observer → capability_recovery → auto_arm (freshness-gated). **This is the first production self-connect.**
5. **Fallback = manual RemoteApp ONLY** on a defined failure/timeout (3 attempts exhausted → CRITICAL `MT5_TERMINAL` alert, or the account not recovered within ~2 recovery cycles). Do not pre-empt automatic recovery.
6. **Rollback/disable at any point:** set the flag(s) OFF in `hosted-executor.env` + recreate `guvfx-backend` → `ARM_SELFCONNECT` OFF = probe-only (degraded); reconciler OFF = fully inert. Backups: `hosted-executor.env.bak.preSESSIONRECON`.

**Constraints honoured throughout:** P0 `MT5_BRIDGE_THREADED` OFF; no entitlement change; no manufactured trades; 25/35/36 identity/strategy/magic/sizing untouched; every production check read-only except the authorized DARK deploy + probe-only enablement. RDS SPLA/SAL remains a separate written commercial gate before 20-account production enablement.

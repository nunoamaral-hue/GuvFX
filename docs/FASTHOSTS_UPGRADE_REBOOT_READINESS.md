# FASTHOSTS_UPGRADE_REBOOT_READINESS — cold-boot unattended-recovery certification

**Date:** 2026-09-30 · **Author:** governance packet REBOOT-RECOVERY P0–P5 · **Reviewer:** pending-Sponsor
**Codebase:** all of P0–P5 merged to `main` (P0 `4da49d5`, P1 `216142a`, P2 `92114f1`, P3 `2ab679b`, P4-prep `2ab7d65`, P4-a `cfa25e3`, P4-b `b41cd1c`, P4-c `caff68d`, P5 `244f1ba`).

## VERDICT: **NOT YET `FASTHOSTS_UPGRADE_REBOOT_READY` — 3 deployment blockers (identified below).**

The recovery subsystem is **built, unit-tested, adversarially certified, and DARK-merged**; the **estate is flat, the bridges are healthy, 25/35/36 are untouched**, and the **arming *machinery* is present and correct in production**. But the reboot's purpose is to production-certify **unattended** recovery (reconciler ON + self-connect ON), and the recovery path is **not deployed** to production — so unattended recovery cannot run, and this certification is **withheld** per the Sponsor rule "if any arming prerequisite is unproven, do not issue; identify the blocker; do not weaken to probe-only." Probe-only is the degraded/rollback mode, **not** the target reboot configuration.

Both flags remain **OFF**. Nothing was deployed, enabled, or rebooted.

---

## 1. Fresh estate / exposure / bridge-health assessment (GREEN)

Read-only, captured 2026-09-30 against prod (VPS `100.119.23.29`) and host (`100.79.101.19`):

| Account | Open trades | Active plans | Identity | Magic | delivery_ready | workspace_node |
|---|---|---|---|---|---|---|
| 25 | **0 (flat)** | 0 | persistent_workspace, demo, active (…2587) | 1000000010 | true | node 2 (rdp 100.79.101.19), agrees with execution_node |
| 35 | **0 (flat)** | 0 | persistent_workspace, demo, active (…5672) | 1000000016 | true | node 2, agrees |
| 36 | **0 (flat)** | 0 | persistent_workspace, demo, active (…7146) | 1000000019 | true | node 2, agrees |

**Estate is FLAT.** Bridges: `8788` (CZ) / `8789` (node·25) / `8804` (35) / `8805` (36) all **LISTENING**; 6 `terminal64` processes; 11 `guvfx_u_*` RDS sessions. **25/35/36 identity/strategy/magic/sizing UNCHANGED** (recorded above as the untouched reference).

---

## 2. Exact automatic cold-boot recovery sequence (as built)

1. **Fasthosts brings Windows back.** Administrator `AutoAdminLogon` → `GuvFX_Autostart` → account-1/CZ MT5 + bridge `:8788`; per-tenant **bridges** recover (`GuvFX_TenantBridge_<id>`, logon-triggered); `GuvFXHostedExecutor` + Tailscale auto-start. **Per-tenant `/portable` TERMINALS do NOT auto-recover** — this is the gap the subsystem closes.
2. **Session-reconciler cron** (`run_hosted_session_reconciler`, own advisory-lock singleton) runs each cadence:
   - Selects armed, matched, non-reserved (CZ + 18 excluded), demo workspaces that are **STALE** (post-boot the projection goes stale within `WORKSPACE_OBSERVATION_FRESH_SECONDS`=300 s).
   - **PROBE_SESSION** (read-only `qwinsta`) → canonical `session_status`.
   - **Pinned state table:** `ACTIVE` → converge (no create); `DISCONNECTED` → reconnect the **same stable conn id** (Windows single-session rejoin, never a 2nd session); `ABSENT` → establish only after all server-derived gates; `UNKNOWN`/`ok:false`/missing/transport-failure → fail closed, no action.
   - **Establish/reconnect** = system-initiated guacd self-connect: short-TTL (~45 s) token → `POST /api/tokens` → drive the tunnel until guacd confirms the RDP connect → **immediate token revoke**. `/portable` RemoteApp auto-launches in the restored session.
3. **Runtime restored ≠ TRADING.** The independent observe → `capability_recovery` (re-assert AutoTrading) → `auto_arm` chain, gated by readiness freshness, is the **sole** re-arm authority. The reconciler arms nothing, logs in nothing, places no order.

## 3. Timing / backoff / concurrency

- **Staggering:** `MAX_ESTABLISH_PER_CYCLE = 4` self-connects per pass; the rest are deferred (no cooldown/claim burned) and retried next cron tick. For 25/35/36 (3 candidates) → **one pass**. For the future 20-account estate → ⌈20/4⌉ = **5 passes** × cadence.
- **Backoff:** per-workspace `MAX_RECOVERY_ATTEMPTS = 3`, `RECOVERY_COOLDOWN_S = 300`; a persistently-failing establish backs off and raises **one** operator alert (never a self-connect loop). Per-incident reset on a confirmed-ACTIVE session (a later reboot gets a fresh budget).
- **Per-tenant isolation:** one tenant's establish failure is caught and counted; siblings continue.
- **Cron cadence:** a **deployment setting, not yet chosen** (see Blocker 3). Recommend ~5 min; confirm the `(cap=4, cadence)` pair meets the reboot-recovery SLA.

## 4. Failure states

- `UNKNOWN` (blind qwinsta / exception / transport): fail-closed, no action, retried next pass.
- Establish failure: retried up to 3× with 300 s cooldown → then a CRITICAL `MT5_TERMINAL` operator alert.
- `node_divergence` (execution_node ≠ workspace_node): fail-closed, never probes/establishes a split host.
- Not deliverable / executor unarmed / guac unconfigured: fail-closed, no decryption, no self-connect.
- **Manual RemoteApp = fallback ONLY** on a defined failure/timeout, explicitly invoked — **not** the default.

## 5. Audit trail

- Per-attempt **`OperationalEvent`** (`workspace.session_recovery`, category RUNTIME, secret-free: internal account id + phase/status/decision/action/reason).
- **`reliability.AlertEvent`** (`MT5_TERMINAL`) session-down open/resolve.
- Read-only **`operations_summary._session_recovery_block`** (armed workspaces, stale candidates, in-flight recovery, open session-down alerts, both darkness gates).
- Structured `core/observability` logging with a per-pass correlation id. **No password / token / broker secret** in any audit, return, or log (adversarially verified).

## 6. Disable / rollback mechanism

- **Two independent flags, both DEFAULT OFF:** `HOSTED_SESSION_RECONCILER_ENABLED` (reconciler on/off) and `HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED` (self-connect on/off).
- **Rollback = flip flags OFF** — no code revert needed. `ARM_SELFCONNECT` OFF → probe-only (degraded, read-only). Reconciler OFF → fully inert. P0 `MT5_BRIDGE_THREADED` remains **OFF** (not part of this packet).
- All P0–P5 code is DARK by default; with flags off the merge is byte-identical to prior behaviour.

## 7. Arming-prerequisite status for `HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED`

| Prerequisite | Status |
|---|---|
| P4-c/P5 governed tests + adversarial certification | ✅ PROVEN (all merged; every SHIP-WITH-FIXES finding applied) |
| Arming machinery in prod (executor armed `HOSTED_HOST_EXECUTOR_ENABLED=1` + resolves; GUAC configured; 25/35/36 `delivery_ready` + `workspace_node` + node-agree) | ✅ PROVEN (fresh read-only check) |
| `Probe-GuvfxSession.ps1` Windows PowerShell 5.1 ParseFile (RULE 9 + RULE 11 controls) | ✅ PROVEN |
| `accounts.dat` broker auto-reconnect for 25/35/36 | ✅ PROVEN (prior MT5-journal evidence) |
| Estate flat + bridges healthy + 25/35/36 untouched | ✅ PROVEN (fresh) |
| **Backend P4/P5 code deployed to prod** | ❌ **BLOCKER 1** — reconciler flags read `NO_FLAG` in the running `guvfx-backend`; the recovery code is not deployed. |
| **PROBE_SESSION host-op deployed** (stage `Probe-GuvfxSession.ps1` + updated executor lib + restart `GuvFXHostedExecutor`; `verify_scripts` green) | ❌ **BLOCKER 2** — `probe_ps1_staged=False`, deployed lib has no `PROBE_SESSION`. The reconciler cannot probe → recovery is non-functional. |
| **Session-reconciler cron scheduled** (+ cadence decided vs SLA) | ❌ **BLOCKER 3** — not in crontab or compose; the reconciler would never run after boot. |
| End-to-end probe→establish→session validated against the real prod host/guacd | ⏳ **By design at the supervised reboot** (no prod spike; feasibility proven by the isolated 12/12 harness). Not a blocker per the Sponsor's revised framing — it is what the supervised reboot certifies. |

## 8. Sponsor-gated sequence to clear the blockers, then certify at the supervised reboot

1. Deploy P4/P5 backend to prod (recreate `guvfx-backend` + workers with the new image; DARK — flags off; byte-identical until armed).
2. Host-deploy PROBE_SESSION: stage `Probe-GuvfxSession.ps1` + the updated `hosted_workspace` executor lib to `C:\GuvFX\hosted\...`, restart `GuvFXHostedExecutor`; **`verify_scripts` must pass** (the `.ps1` is already ParseFile-validated; RULE 9). Deploy-ordering: stage the `.ps1` **before**/with the lib or the daemon fails closed for all ops.
3. Schedule the session-reconciler cron; choose the cadence and confirm `(cap=4, cadence)` meets the reboot-recovery SLA.
4. **Recommended:** a supervised single-tenant validation (enable both flags, probe + establish ONE account) to get the first real-guacd self-connect proof **before** estate-wide.
5. Enable **both** flags (reconciler ON + self-connect ON) — the target unattended-recovery configuration.
6. **Supervised certification reboot.** Automatic recovery runs; **do NOT manually open 25/35/36** unless automatic recovery reaches a defined failure/timeout state and fallback is explicitly invoked.

Only after steps 1–5 are executed and the fresh gates (§1) re-confirmed GREEN can `FASTHOSTS_UPGRADE_REBOOT_READY` be issued.

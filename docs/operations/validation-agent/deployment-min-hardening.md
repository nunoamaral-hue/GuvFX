# Validation Agent — Minimum Production Hardening: Deployment & Rollback Package (WS-L)

**Status: PREPARED, NOT APPLIED.** Every host/backend action below is **separately Sponsor-gated** and is
**not** performed by the packet that authored this file. This is the exact, reversible change plan the
Sponsor approves before any Windows-host or backend mutation. Design authority:
[VALIDATION_AGENT_PRODUCTION_HARDENING.md](../../VALIDATION_AGENT_PRODUCTION_HARDENING.md);
ADR: [0013 addendum 2026-08-06](../../ADRs/0013-beta-agent-service-host-winsw.md).

> **Blast-radius rule.** This change touches ONLY: the `GuvFXBetaAgent` service + its own bundle files, and
> the backend monitoring modules. It must NEVER touch `:8788` (live trade bridge), the wayond listener, the
> shadow worker, Wayond, Customer Zero **#12**, or live account **#1**.

## 0. What changed in the repository (this packet)

| File | Change |
|---|---|
| `deploy/beta-agent/agent_lifecycle.py` | NEW — lifecycle events, single-instance guard, launch classification (secret-safe, pure) |
| `deploy/beta-agent/agent.py` | classify launch; emit lifecycle log; **advisory** single-instance guard; **exclusive OS bind** (`allow_reuse_address=False` + `SO_EXCLUSIVEADDRUSE`); crash detection (`AGENT_CRASHED` + non-zero exit); optional refuse-to-bind; pass `agent_supervised` |
| `deploy/beta-agent/lib/mgmt_agent_core.py` + `backend/terminal_provisioning/mgmt_agent_core.py` | NEGOTIATE advertises `agent_supervised` (byte-identical copies) |
| `deploy/beta-agent/manifest.py` + `manifest.json` | cover `agent_lifecycle.py`; checksums re-pinned; `manifest_version=2026-08-06.1` |
| `deploy/beta-agent/winsw/GuvFXBetaAgent.supervised.xml` | NEW — supervised target profile (Automatic+delayed, bounded-backoff restart, launch markers) |
| `backend/terminal_provisioning/agent_health_probe.py` | NEW — signed-NEGOTIATE readiness probe (8 states, cadence, hysteresis) |
| `backend/terminal_provisioning/agent_monitoring.py` | NEW — metric/alert computation from durable sources |
| `backend/terminal_provisioning/agent_alert_sink.py` | NEW — alert-delivery abstraction (Null/Logging; no live external send) |
| `backend/terminal_provisioning/agent_status_presenter.py` | NEW — customer-safe vs operator-safe presenter |
| `frontend/src/lib/agent-status.ts` + `AgentStatusPanel.tsx` | NEW — read-only Ops surface (unrouted) |
| docs/tests | ADR addendum, runbook (unsupervised-listener), audit, this package, WS-K tests |

The DARK install-only `GuvFXBetaAgent.xml` is **unchanged** (it records current host state).

## 1. Pre-change host evidence (READ-ONLY, capture BEFORE anything)

Run and archive (redacted) — do NOT skip; RULE 11 (prove the measurement path with a positive control):
1. `Get-Service GuvFXBetaAgent | Format-List Name,Status,StartType` — current service state.
2. The `:8791` owner: `Get-NetTCPConnection -LocalPort 8791 -State Listen` → owning PID → `Get-Process -Id <pid>`.
3. Firewall rules `GuvFX-Beta-Agent-*` (`Get-NetFirewallRule`) — confirm both Enabled + default inbound Block.
4. `BETA_AGENT_BASE_URL`, `BETA_AGENT_BIND_HOST`, `BETA_AGENT_BIND_PORT` (machine env) — confirm `:8791` + agree.
5. Current bundle `manifest.json` `manifest_version` + `agent.py`/`lib/mgmt_agent_core.py` SHAs on host.
6. Last `agent-state/logs` WinSW write time + last diagnostic-artefact time (baseline for "did it serve?").
7. **Launcher Gate** (RULE 8): review the on-host `.bat`/watchdog launch paths that are not in Git.

## 2. Backup / rollback anchors

- **Bundle:** copy the current `C:\GuvFX\beta\agent` + `winsw\GuvFXBetaAgent.xml` to a dated backup dir on host.
- **No DB migration** in this packet — the backend adds no model, so there is **no schema rollback**. (Verify:
  `manage.py makemigrations --check terminal_provisioning` shows no new migration.)
- **Rollback anchor commit:** the merge base of this PR (record the SHA at deploy time).

## 3. Backend deploy (Sponsor-gated) — inert by default

The backend modules are pure/importable and change NO existing behaviour until a scheduler is wired. Deploying
the backend image only makes the probe/monitoring/presenter **available**; nothing runs them yet.
- To enable delivery later: set `AGENT_ALERT_SINK=logging` + `AGENT_ALERT_OWNER=<named human/rota>` (interim,
  local, inert sink). Any EXTERNAL channel is a further Sponsor-gated step (no approved sink exists yet).
- Wiring the probe on a cadence (management command / scheduler) is a **separate** change, not in this packet.

> **Install via the SANCTIONED INSTALLER ONLY (2026-08-06).** The 2026-08-06 deploy attempt proved a bare
> `winsw install` regresses the identity to `LocalSystem` on WinSW v2.12. `install_service.ps1` now supports
> both profiles via `-InstallProfile Dark|Supervised`: it does the post-install `sc config obj=` identity assignment
> + `SeServiceLogonRight` grant, verifies `SERVICE_START_NAME == NT SERVICE\GuvFXBetaAgent` (rejecting
> LocalSystem), and **auto-rolls-back** on any verification failure. **WinSW / `sc config` / `secedit` are
> never called by hand.** Contract: [installer-contract.json](installer-contract.json).

## 4. Windows-host deploy (Sponsor-gated) — via the SANCTIONED INSTALLER ONLY

Order (reversible at every step):
1. Re-stage the bundle (integrity-pinned): copy the new bundle to host; verify on-host `manifest.json`
   checksums match `compute_checksums` (fresh-disk check). The installer runs the RULE-9 interpreter/XML gates.
2. **PLAN first (no mutation):** `install_service.ps1 -InstallProfile Supervised` (no `-Apply`). It parses/validates
   the supervised XML contract (Automatic+delayed, restart tiers, launch markers, `stoptimeout > drain`) and
   the interpreter/wrapper pins. RULE-9 on-host parse gate = this PLAN run must succeed.
3. **APPLY (install-only, STOPPED):** `install_service.ps1 -InstallProfile Supervised -Apply`. The installer stages
   the supervised XML (substituting a NON-SECRET launch token), registers WinSW, **assigns the virtual account
   + grants the logon right**, and verifies `StartName == NT SERVICE\GuvFXBetaAgent`, `StartMode = Auto`,
   restart tiers present, no global DLL write. On ANY failure it **auto-rolls-back** to the prior service and
   raises. `BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH` ships `0` (do not brick manual recovery pre-proof).
4. **Start via the service ONLY** (never SSH/`python agent.py` — RULE 1): `Start-Service GuvFXBetaAgent`.
5. **Verify (health):** the backend readiness probe returns **HEALTHY** with `agent_supervised == true`; one
   signed NEGOTIATE succeeds; `agent_lifecycle.jsonl` shows `AGENT_STARTING`→`AGENT_LISTENING`→`AGENT_READY`
   with `supervised=true`.
6. **Verify (alert path):** confirm delivery reaches the approved internal route (Telegram ops → `support@`
   fallback). Do NOT test with #12/#1.
7. Only after 5–6 are green and stable for the confirm window, OPTIONALLY flip
   `BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH=1` (hard refuse) and restart the service.

## 5. Expected interruption

The installer registers STOPPED; step 4 starts the listener → `:8791` becomes available. Re-installing over an
existing service briefly deregisters/reregisters it (seconds) while it is already Stopped, so no live listener
is interrupted. `:8788` and all trading are unaffected.

## 6. Rollback (reversible, no data change)

- **Automatic (install-time):** any verification failure during `-Apply` triggers the installer's built-in
  rollback (restore prior XML + re-register + reassign `NT SERVICE\GuvFXBetaAgent` + prior StartMode; or clean
  uninstall if there was no prior service). It fails LOUD ("ROLLBACK INCOMPLETE") if the identity cannot be
  restored — never leaves a wrong identity.
- **Manual (post-start):** `Stop-Service GuvFXBetaAgent`, then `install_service.ps1 -InstallProfile Dark -Apply` to
  restore the DARK profile (Manual / recovery=none / NT SERVICE identity — verified by the installer), then
  re-stage the previous bundle from the §2 backup and verify `manifest_version` reverts.
- Backend: redeploy the rollback-anchor image (no migration to reverse); set `AGENT_ALERT_SINK` back to `null`.
- Verify: service back to Manual/STOPPED under `NT SERVICE\GuvFXBetaAgent`; one NEGOTIATE succeeds.

## 7. STOP conditions (abort + escalate, do not improvise)

- On-host `manifest.json` checksums do NOT match after re-stage → **abort before start** (integrity gate).
- Any `.ps1`/config fails the RULE-9 parser → abort.
- After start, `agent_supervised != true` or NEGOTIATE fails → stop, roll back, escalate to Engineering.
- The `:8791` owner is a process you did NOT start → the unsupervised-listener runbook, not a restart.
- Any temptation to touch `:8788`, #12, or #1 → STOP; that is out of blast radius.

## 8. Post-deploy verification checklist

- [ ] `Get-Service GuvFXBetaAgent` = Running, StartType Automatic (Delayed).
- [ ] Readiness probe HEALTHY + `agent_supervised == true`.
- [ ] `agent_lifecycle.jsonl` shows the supervised start sequence.
- [ ] Alert delivery reaches the named owner on a forced DEGRADED.
- [ ] `:8788` / wayond / shadow-worker / #12 / #1 all untouched (spot-check unchanged).
- [ ] `makemigrations --check` clean (no schema drift).

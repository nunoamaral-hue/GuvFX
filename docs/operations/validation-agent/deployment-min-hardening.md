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

## 4. Windows-host deploy (Sponsor-gated) — the supervised switch

Order (reversible at every step):
1. Re-stage the bundle (integrity-pinned): copy the new bundle to host; run the RULE-9 parse gate on all
   `.ps1`/config; verify on-host `manifest.json` checksums match `compute_checksums` (fresh-disk check).
2. Set the launch markers as service env in the WinSW config: `BETA_AGENT_SERVICE_IDENTITY=GuvFXBetaAgent`,
   `BETA_AGENT_SUPERVISED_TOKEN=<install value>`, `BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH=0` (keep OFF first).
3. Install the supervised profile: replace the running WinSW XML with `GuvFXBetaAgent.supervised.xml`
   (`winsw uninstall` → `winsw install` OR `winsw reload` per the host's WinSW version). Confirm
   `stoptimeout > BETA_AGENT_DRAIN_TIMEOUT_S` (install asserts this).
4. **Start via the service ONLY** (never SSH/`python agent.py` — RULE 1): `Start-Service GuvFXBetaAgent`.
5. **Verify (health):** the backend readiness probe returns **HEALTHY** with `agent_supervised == true`; one
   signed NEGOTIATE succeeds; `agent_lifecycle.jsonl` shows `AGENT_STARTING`→`AGENT_LISTENING`→`AGENT_READY`
   with `supervised=true`.
6. **Verify (alert path):** force a benign DEGRADED (e.g. probe with VALIDATE_LOGIN unarmed on a disposable
   config) and confirm the `LoggingAlertSink` emits one line to `guvfx.validation_agent.alerts` reaching the
   named owner. Do NOT test with #12/#1.
7. Only after 5–6 are green and stable for the confirm window, OPTIONALLY flip
   `BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH=1` (hard refuse) and restart the service.

## 5. Expected interruption

Steps 3–4 restart the listener → `:8791` is briefly unavailable (seconds). During that window a customer
validation would return a transport failure (retry-able, no credential implication). Schedule outside any
active demo. `:8788` and all trading are unaffected.

## 6. Rollback commands (reversible, no data change)

1. `Stop-Service GuvFXBetaAgent`.
2. Reinstall the DARK profile: restore `winsw\GuvFXBetaAgent.xml` (Manual / `onfailure=none`) and
   `winsw install` (or reload). Remove the added service env markers.
3. Re-stage the previous bundle from the §2 backup; verify `manifest_version` reverts.
4. Backend: redeploy the rollback-anchor image (no migration to reverse). Set `AGENT_ALERT_SINK` back to
   `null` (or unset).
5. Verify: service back to Manual/STOPPED (or started under the old bundle), one NEGOTIATE succeeds.

## 7. STOP conditions (abort + escalate, do not improvise)

- On-host `manifest.json` checksums do NOT match after re-stage → **abort before start** (integrity gate).
- Any `.ps1`/config fails the RULE-9 parser → abort.
- After start, `agent_supervised != true` or NEGOTIATE fails → stop, roll back, escalate to Engineering.
- The `:8791` owner is a process you did NOT start → the unsupervised-listener runbook, not a restart.
- Any temptation to touch `:8788`, #12, or #1 → STOP; that is out of blast radius.

## 7a. Verified host + VPS baseline (read-only, 2026-08-06 — merge/cert packet)

Captured read-only at merged `main` `be7f215`; every value confirms the deployment assumptions and the DARK state.

**Windows host `WIN-RD8VDS93DK7` (100.79.101.19), boot 2026-06-10 (no reboot):**
- `GuvFXBetaAgent`: **Stopped / Manual / recovery=none** (RESET_PERIOD 86400, no failure actions); identity `NT SERVICE\GuvFXBetaAgent`; WinSW exe `C:\GuvFX\beta\agent-winsw\GuvFXBetaAgent.exe` (sha16 `923111C7…`), DARK XML `GuvFXBetaAgent.xml` (sha16 `DCDB8DFD…`).
- **`:8791` NO LISTENER** (agent down); **`:8788` LISTEN pid=15396** (trade bridge — DO NOT TOUCH).
- Firewall: `GuvFX-Beta-Agent-In` (Allow, backend) + `GuvFX-Beta-Agent-Block-NonBackend` (Block) **both Enabled**.
- Deployed bundle `C:\GuvFX\beta\agent`: **manifest `2026-08-03.3`**, `agent_lifecycle.py` **ABSENT**, `agent_lifecycle.jsonl` **ABSENT** (min-hardening not deployed — EXPECTED_STALE vs repo `2026-08-06.1`).
- Tasks: `GvfxValidationRunner` **Ready**; `GuvFXBetaRuntime-1..4` + `…Stop-1..4` Ready.
- ACLs: `NT SERVICE\GuvFXBetaAgent` = ReadAndExecute on `agent` + `agent-winsw`; **Modify** on `agent-state` and `agent-state\logs` (⇒ lifecycle log + single-instance lock writable under the service identity — **no permission change required**). C: free 414.8 GB.

**VPS (100.119.23.29):** `guvfx-backend` container `d108e18a…` image `c096e83b…` (provenance commit `6b0fdf96`, **pre-merge** — backend hardening NOT deployed), restarts 0; `guvfx-frontend` `d01a914c…` image `2310ad62…`. Signing keyring `BETA_AGENT_KEYRING`/`KEY_ID`/`BASE_URL` **SET**; `AGENT_ALERT_SINK`/`AGENT_ALERT_OWNER` **NOT SET** (⇒ alert sink defaults to safe `NullAlertSink`). Latest DB backup `guvfx-pre288-20260805T165413Z.sql.gz`. Rollback images incl. `guvfx-prod-guvfx-backend:latest` (`c096e83b`) + `rollback-pre288-…`. `:8788` bridge + wayond listener + shadow worker all up and untouched.

**Path corrections for §4:** the WinSW profile lives under `C:\GuvFX\beta\agent-winsw\` (not the bundle dir); back up + swap `GuvFXBetaAgent.xml` there. The lifecycle log/lock resolve to `C:\GuvFX\beta\agent-state\logs\` (service has Modify).

## 7b. Adversarial pre-deployment review — folded-in refinements (2026-08-06)

Six lenses against the captured host evidence. Repo/doc refinements applied here; host-only uncertainties are **deployment-day STOP checks**.

- **Minimise blast radius — Windows-host-ONLY first deploy.** The backend hardening modules are **inert until a probe schedule is wired** (§3), so the backend recreate is **optional and deferrable**. The strictly-needed change is the Windows-host supervised switch. **Recommendation:** first deploy = host-only (supervised profile); defer the `guvfx-backend` recreate until the probe schedule + alert owner are decided — this avoids any API/orchestration interruption on the VPS entirely.
- **Maintenance = `Stop-Service`, never `taskkill`.** A graceful `Stop-Service` exits 0 → WinSW treats it as a clean stop → **no restart**. A `taskkill`/kill is a non-zero/abnormal exit → WinSW **would** restart. For any planned maintenance, use `Stop-Service GuvFXBetaAgent` only.
- **Rollback must revert `StartType` to Manual.** Restoring the DARK `GuvFXBetaAgent.xml` reverts StartType; verify `Get-Service … StartType = Manual` after rollback so the old bundle cannot auto-start.
- **Alert delivery is NOT real until owner + schedule exist (RR-11).** `AGENT_ALERT_SINK`/`OWNER` are unset on prod ⇒ `NullAlertSink` (delivers nowhere). The interim `LoggingAlertSink` only writes a local log; a **named human + a scheduled poll cadence** (or an approved external channel) is required or the silent-outage risk recurs. **This is the GATE-4 Sponsor decision.**
- **Wedge detection is partial (RR-12, fast-follow).** `validation_wedged` fires from customer `validation_busy` rate; `oldest_inflight_validation_seconds` is not agent-exposed yet, so a wedge with no concurrent customer traffic may not page until a customer hits it. Acceptable for a ≤5–10-user manual demo; named as fast-follow.

### Deployment-day STOP checks (host-only — prove live, post-deploy)
1. **Exclusive bind proven live:** after `Start-Service`, a 2nd `python agent.py` FAILS to bind `:8791` (WSAEADDRINUSE) and logs `AGENT_LAUNCH_REJECTED`; the service keeps serving; `:8788` unaffected.
2. **`SO_EXCLUSIVEADDRUSE` + rebind:** simulate an abnormal exit and confirm the supervised restart **re-binds** `:8791` within the restart window (no lingering-socket lockout).
3. **WinSW failure tiers valid:** RULE-9 parse the supervised XML on-host and confirm the host's WinSW version honours the 3 `onfailure=restart` tiers before install.
4. **Supervised proof live:** WinSW-launched agent reports `agent_supervised=true`; manual launch reports `false` and never HEALTHY.
5. **Maintenance-stop-no-restart:** `Stop-Service` produces `AGENT_STOPPING`/`AGENT_STOPPED` and does NOT trigger an `onfailure` restart.

## 8. Post-deploy verification checklist

- [ ] `Get-Service GuvFXBetaAgent` = Running, StartType Automatic (Delayed).
- [ ] Readiness probe HEALTHY + `agent_supervised == true`.
- [ ] `agent_lifecycle.jsonl` shows the supervised start sequence.
- [ ] Alert delivery reaches the named owner on a forced DEGRADED.
- [ ] `:8788` / wayond / shadow-worker / #12 / #1 all untouched (spot-check unchanged).
- [ ] `makemigrations --check` clean (no schema drift).

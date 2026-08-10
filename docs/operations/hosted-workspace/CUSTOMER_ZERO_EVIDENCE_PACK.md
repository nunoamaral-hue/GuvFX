# Customer Zero — Certification Evidence Pack

Compiled 2026-08-10. Subject: TradingAccount id=1, broker **1302561**, server **IS6Technologies-Demo**
(demo), hosted portable runtime `C:\GuvFX\accounts\1\terminal` under non-admin **guvfx_u_1**, delivered as a
Guacamole RemoteApp. **Execution DARK throughout; no order placed/closed/modified.** Every row states the
evidence source; where a claim rests on an external gate it is marked PENDING, not asserted.

## 1. Repository / CI
- main `3cac014`. Relevant merged PRs: **#330** (single-session invariant), **#331** (AppLocker AuditOnly
  hardening + legacy plan + tech-debt), **#332** (AppLocker Enforce capability + enforce evidence). Earlier:
  #325 (full-desktop suppression), #326 (keyboard focus), #327/#328 (clipboard/layout), #329 (clipboard CSP).
- Execution negative-proof suite: **165 tests OK** (`execution.tests_broker_gate`, `tests_bridge_binding`,
  `tests_bridge_pin`, `tests_bridge_idempotency`, `tests_bridge_lostack`, `tests_bridge_mutation_identity`,
  `tests_bridge_guarded_attach`, `tests_dispatch_gate`, `tests_hosted_execution`, `tests_runtime_pause`).

## 2. Production (VPS, `docker exec guvfx-backend`)
- `HOSTED_MT5_EXECUTION_ENABLED=[]` (empty) — **execution DARK**.
- `operational_health`: OVERALL DEGRADED (faults=1). HEALTHY=7 (backend, database, cache, workers, bridge,
  agent_monitor, **hosted_workspace — 1 workspace**); mt5=DEGRADED(UNKNOWN, expected while dark); 6
  AWAITING_SPONSOR (guacamole, broker_health, operational_events, delivery, execution, onboarding — the
  intended dark / host-cert-pending states). NO-FAKE-READY: nothing claimed ready that was not.
- `rollback_plan`: FULLY reversible, all flag-unset steps `destructive=False`.

## 3. Host — identity, singleton, runtime (Windows `WIN-RD8VDS93DK7`)
- Non-admin identity `guvfx_u_1`, SID `S-1-5-21-2216203845-1747098376-1637942580-1003`.
- **Singleton:** `fSingleSessionPerUser=1`; exactly **one** Active `guvfx_u_1` session (rdp-tcp#3). Root cause
  of prior duplicates (`=0`) fixed. Sponsor behavioural positive control accepted
  (`HOSTED_SINGLETON_BEHAVIOUR_CERTIFIED`).
- **Portable runtime running under Enforce:** `terminal64` pid from `C:\GuvFX\accounts\1\terminal` in session 3.

## 4. Broker login (journal, flushed, under Enforce)
`C:\GuvFX\accounts\1\terminal\logs\20260810.log`:
- `'1302561': authorized on IS6Technologies-Demo through Access Server 1`
- `'1302561': terminal synchronized with IS6 Technologies Ltd: 0 positions, 0 orders, 96 symbols`
- `'1302561': trading has been enabled - hedging mode`
This closes the earlier evidence-quality gap (the prior "Invalid account" was a concurrent-login collision
with the legacy terminal; see `LEGACY_RETIREMENT_PLAN.md` §3).

## 5. RemoteApp / isolation (session-3 process set, under Enforce)
- Processes: `terminal64` (MT5) + RemoteApp/RDP infra only (`rdpshell`, `rdpinit`, `rdpclip`,
  `ShellAppRuntime`, `ShellHost`, `sihost`, `taskhostw`, `winlogon`, `csrss`, `dwm`, `fontdrvhost`, `svchost`).
- **`explorer` present: NO. `cmd`/`powershell`: NO.** No desktop, no shell.

## 6. AppLocker (AuditOnly → Enforce)
- Policy: `applocker/guvfx-hosted-auditonly.xml`; deploy/rollback via `Set-GuvfxAppLocker.ps1`
  (`Deploy|Verify|Rollback|Evidence`, `-Enforce`). ASCII-only (RULE 9 parse-validated on host).
- **AuditOnly review (RULE 11 positive control):** hosted user generated **29 `8002` "allowed" events, 0
  `8003` would-be-blocks** during the Sponsor session — audit path proven to capture the hosted user's
  activity, so "0 blocks" is a true clean result. No policy refinement required.
- **Enforce active:** effective `Exe=Enabled, Msi=Enabled, Script=Enabled`. AppIDSvc Auto/Running.
- **Escape decision-proof** (`Test-AppLockerPolicy`, enforced policy, hosted SID): `cmd`, `powershell`,
  `explorer`, `taskmgr`, `regedit`, `mmc`, `mshta`, `control` → **Denied**; `terminal64.exe` → **Allowed**.
- **Administrator recovery:** admin commands run under Enforce (Admin `Allow-*`).
- **Rollback:** round-trip verified — `-Mode Rollback` clears effective to `<none>`, redeploy restores.
- PENDING: **behavioural** escape attempts (actually invoking each binary as the customer to produce an
  `8004` block event) — requires the hosted-user session; STOP-condition Sponsor step. Marker withheld.

## 7. Execution readiness (DARK)
- Gate `backend/execution/readiness.py` `PersistentWorkspaceProvider.evaluate` — layered, ANDed, fail-closed
  arming (subsystem flag, execution flag, is_active, not disconnected, is_demo, workspace present,
  per-workspace `execution_enabled`, proj_connected, proj_account_match, human ACK, canonical execution_ready,
  observation fresh). `HOSTED_MT5_EXECUTION_ENABLED` OFF ⇒ `RW_EXECUTION_FEATURE_DISABLED` denies all.
- Node authority: workspace_node=1, execution_node=1, TradingAccount.terminal_node=1; `rdp_host`=100.79.101.19
  (delivery) is distinct from execution identity. Delivery-connected != execution-authorized.
- `EXECUTION_CERTIFICATION_READY` — the exact host-cert runbook is `EXECUTION_ENGINE_HOST_CERTIFICATION.md`.

## 8. Markers
- **Emitted (evidence-supported):** `HOSTED_SINGLETON_BEHAVIOUR_CERTIFIED`, `EXECUTION_CERTIFICATION_READY`,
  AppLocker **Enforce active**, `CUSTOMER_ZERO_ROLLBACK_VERIFIED` (earlier).
- **Withheld (evidence not yet produced):** `CUSTOMER_ZERO_REMOTEAPP_ISOLATION_CERTIFIED` (needs behavioural
  escape `8004` evidence), `HOSTED_EXECUTION_CERTIFIED` (needs the Sponsor demo trade).

## 9. Phase-8 final adversarial review (6 lenses, verify pass)

- **HIGH #1 — AppLocker writable-allowed-path bypass (FIXED + re-verified).** The EXE Allow rule over the
  user-writable runtime tree (`C:\GuvFX\accounts\*`) let a renamed Microsoft-signed shell (cmd.exe copied to
  `…\x.exe`) run under Enforce, defeating the leaf-name deny list. **Fix:** replaced the path-Allow with a
  MetaQuotes **publisher** Allow (`O=METAQUOTES LTD., …`). Re-verified on host: `terminal64.exe`→Allowed,
  renamed-cmd-in-tree→**DeniedByDefault**, cmd.exe→Denied. Corrected policy re-Enforced (Exe/Msi/Script
  Enabled); hosted MT5 unaffected. My earlier "structural confinement is in place" claim was **wrong** and is
  corrected here.
- **HIGH #2 — SURVIVING (requires Sponsor/CA decision). Customer Zero uses the LIVE account id=1 (1302561)
  as the execution-cert subject.** Account #1 is actively traded by `ti_signals` (asn#8) + `wayond` (asn#7)
  signal-copy via the legacy bridge. Running the execution-certification demo trade on that account couples
  the hosted path to a live-trading account. The legacy-retirement plan §4 M4 (legacy logout of 1302561 is a
  prerequisite) partially mitigates it, but the **cleaner path is a disposable/dedicated demo account for the
  execution cert**. **Decision needed before the demo-trade gate.** Execution stays DARK meanwhile.
- **MEDIUM (residuals to harden):** (a) deny list omits `%WINDIR%` LOLBINs (rundll32/regsvr32/msbuild/…),
  which remain Everyone-allowed — not trivially reachable (no shell in-session) but the proper fix is a
  stricter allow-model, not more name-denies; (b) the "escapes Denied" evidence is **decision-level**
  (`Test-AppLockerPolicy`), not behavioural `8004` — honestly reflected (isolation marker withheld);
  (c) "confine to MT5 only" wording is stronger than the current default-allow+deny model warrants.
- **LOW (documented):** SINGLE_SESSION_INVARIANT.md's "MT5 holds a per-data-dir single-instance lock"
  defense-in-depth claim is contradicted by the observed duplicate-terminals evidence — treat as unproven;
  un-gated `record_delivery_attempt` could regress a seq-gated CONNECTED state; duplicate `.bak` gate-module
  copies in the tree (tracked in TECH_DEBT_REGISTER #15).

## 10. Outstanding Sponsor gates
0. **CA decision on HIGH #2** — execution-cert subject: live account #1 (with legacy logged out) vs a
   disposable demo account. **Blocks the demo-trade gate.**
1. Behavioural escape test (customer attempts shells → `8004` blocks) → isolation marker.
2. Tiny execution-certification demo trade → `HOSTED_EXECUTION_CERTIFIED`.

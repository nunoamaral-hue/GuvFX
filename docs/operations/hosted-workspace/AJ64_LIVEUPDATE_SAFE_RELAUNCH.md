# AJ#6.4 — LiveUpdate-safe tenant relaunch (corrective for the AJ#6.3 Shape-3 defect)

**Status:** implemented + reviewed; DARK (behaviour reached only when `HOSTED_CAPABILITY_RECOVERY_ENABLED=1`,
which remains OFF until this corrective is deployed and re-certified). **Surface:** one host artefact
(`backend/terminal_provisioning/windows/Relaunch-GuvfxTerminal.ps1`) + its static test bar
(`backend/hosted_workspace/tests_relaunch_liveupdate_safe.py`). No backend/dispatch/runner change (the primitive
args are unchanged), no migration.

## 1. The defect (AJ#6.3, proven in prod 2026-08-17)

`RELAUNCH_TERMINAL` closed account 24's connected terminal then launched `terminal64.exe /portable`. A pending
MetaTrader **LiveUpdate** intercepted the launch — the updater
(`%APPDATA%\MetaQuotes\Terminal\<hash>\liveupdate\terminal64.exe /update`) ran instead of the trading terminal
and stuck, regressing the workspace CONNECTED → down. Forensics confirmed the tenant's AppData held a staged
build-6090 payload + a stuck updater; the real binary (`5.0.0.5833`) was never updated.

## 2. The corrective

Apply the **certified Variant-A LiveUpdate containment** (host-proven 2026-07-31 in
`deploy/beta-agent/slot_launch.ps1::Apply-LiveUpdateContainment`) **before** relaunch: kill the tenant's own
stuck updater, purge `%APPDATA%\MetaQuotes\WebInstall` + `Terminal\<hash>\liveupdate`, and Deny-write those
staging paths for the tenant SID — so `terminal64.exe /portable` launches the canonical trading terminal, not
the updater. The primitive distinguishes trading vs updater by **executable path** and returns `ok:true` **only**
when a terminal64 at the canonical `C:\GuvFX\accounts\<id>\terminal\terminal64.exe` is running; otherwise it
fails closed (`relaunch_hit_liveupdate` / `trading_terminal_not_restored` / `containment_failed` /
`containment_task_did_not_run`).

**Containment runs as the tenant, not LocalSystem.** The orchestrator (the executor, LocalSystem) dispatches the
purge/Deny through a per-account **tenant-principal scheduled task** (Limited token), exactly like the certified
prior art — then reads only the task's exit code (`LastTaskResult`). The orchestrator itself performs **no**
recursive delete or `Set-Acl` over any tenant-writable path; it only reads and registers tasks.

## 3. Adversarial review (2 rounds, Workflow-driven)

**Round 1** (6-lens attack + verify) surfaced a **CONFIRMED HIGH confused-deputy**: an earlier inline design ran
the purge/`Set-Acl` as LocalSystem over the tenant's staging paths with no reparse-point guard, so a
tenant-planted directory junction (`mklink /J WebInstall -> C:\GuvFX\accounts\1\terminal`) would have made
LocalSystem delete/re-ACL **Customer Zero's** files. Also confirmed: `NTAccount->SID` can hang on this workgroup
host; the reconstructed `C:\Users\<user>` path isn't the real profile; `GetOwner`-null under-counts live
terminals. **All fixed** by moving containment into the tenant task (Limited token bounds the blast radius;
`$env:APPDATA`/`GetCurrent().User` give the real profile + SID with no NTAccount) + reparse rejection + PID/
canonical-path detection.

**Round 2** (re-review of the corrected design) verdict on the original HIGH: **CLOSED (NONE)** — "every mutating
op executes under the tenant Limited token; the LocalSystem orchestrator does ONLY reads plus task registration;
NO Customer Zero file is deleted or re-ACLd." Of 5 material findings sent to adversarial verify, **4 were
REFUTED** (leftover-pid detection is total; force-kill hits staging not the canonical binary; the exact-path
match excludes an updater from success; `$self`/scripts-dir is server-config, not tenant-influenced) and **1 was
CONFIRMED MEDIUM** — see §4. Net after the fixes: **0 HIGH, 0 MEDIUM open** (the one MEDIUM is an accepted,
mitigated architectural constraint, below).

## 4. Accepted architectural constraint (the one confirmed MEDIUM)

Capability recovery targets a **CONNECTED** (up, broker-connected) terminal stuck at `trade_allowed=False`, and
MT5 `/portable` is a **singleton per data directory** — so a launch-verify-then-close atomic swap is impossible;
the close must precede the relaunch. A **persistent** relaunch failure therefore leaves the terminal transiently
DOWN. This is:

- **fail-closed** — the primitive never returns `ok:true` with a dead terminal;
- **bounded to a single occurrence** — after one regression the observer drops the workspace from the candidate
  set; the next pass relaunches the down runtime (`trading_before=False`, no close) and, with containment in
  force, succeeds;
- **mitigated** — the LiveUpdate cause (the actual AJ#6.3 failure) is removed by containment before the close, and
  the relaunch is **retried once** inside the primitive for a transient Session-0 launch no-op;
- **inert while unauthorised** — the pre-regression `trade_allowed=False` state is non-executable, and the whole
  edge is DARK unless `HOSTED_CAPABILITY_RECOVERY_ENABLED=1`.

The close→relaunch order cannot be reversed (MT5 constraint); the residual is accepted and recorded here rather
than papered over.

## 5. Certification

Behavioural proof (already-staged update → containment → trading terminal restored → `trade_allowed=True` →
`EXECUTION_READY`, while unarmed/unauthorised) is the AJ#6.4 on-host certification on support@/account 24
(Phases 11-18). Static safety is enforced by the 23-test bar and the host ParseFile/ASCII gate.

# B3P — service APPLY STOPPED; production-scope DLL writes + LocalSystem mis-install

`WIN-RD8VDS93DK7`, `2026-07-24T06:35Z`, main `a544359`. The service was installed but VERIFY threw and I
**STOPPED**. The service is **Stopped, ProcessId=0 — it has never run.** The live bridge (pid 13292) and
production MT5 (pid 4336) are **UP and unaffected**. No task was enabled, no runtime staged, no MT5 launched.
No remediation performed (evidence preserved; no manual host patch).

## What the APPLY did before it stopped

Interpreter verified (venv `py.exe`, 3.11.9, pywin32 importable), 5 service-SID ACL grants succeeded, then:

```
install service 'GuvFXBetaAgent' (pywin32, startup=manual)
  moving host exe 'C:\GuvFX\beta\agent-venv\Lib\site-packages\win32\pythonservice.exe'
    -> 'C:\GuvFX\beta\agent-venv\pythonservice.exe'
  copying helper dll '...\pywin32_system32\pywintypes311.dll'
    -> 'C:\Program Files\Python311\pywintypes311.dll'      <-- BASE INTERPRETER DIR
  Service installed
set service logon to 'NT SERVICE\GuvFXBetaAgent' ...
VERIFY -> throw: service identity is 'LocalSystem', expected 'NT SERVICE\GuvFXBetaAgent' - do NOT start
```

## Problem 1 — the service installed as LocalSystem (the `obj=` assignment did not take)

`sc qc` confirms `SERVICE_START_NAME : LocalSystem`, `State Stopped`, `ProcessId 0`. The
`install_service.ps1` LocalSystem-refusal check worked exactly as designed and **refused to start it**.

**Root cause.** pywin32's `service.py install` registers the service as LocalSystem; the script's next step,

```powershell
sc.exe config $ServiceName obj= "$RunAsUser" password= "" start= demand | Out-Null
```

is meant to reassign it to `NT SERVICE\GuvFXBetaAgent` — and it did not. That `sc.exe config` has **no
`$LASTEXITCODE` check**, so its failure was swallowed and only surfaced at VERIFY. The likely cause is the
PowerShell → `sc.exe` passing of `password= ""` (sc.exe's space-delimited `key= value` syntax is fragile
when a value is an empty quoted string). This is a genuine install_service.ps1 defect: (a) the sc config
result is unchecked, and (b) the empty-password virtual-account assignment is not passed robustly.

## Problem 2 — pywin32 writes helper DLLs to GLOBAL locations; the venv does NOT isolate them

This is the architectural finding, and it is not a one-off. Two separate events wrote pywin32 helper DLLs
outside the venv:

| File | When | Cause |
|---|---|---|
| `C:\Windows\System32\pywintypes311.dll` | **05:38** | Workstream B `pywin32_postinstall -install` |
| `C:\Windows\System32\pythoncom311.dll` | **05:38** | Workstream B `pywin32_postinstall -install` |
| `C:\Program Files\Python311\pywintypes311.dll` | **06:35** | this service install |

pywin32's `pythonservice.exe`, run from a venv, resolves its helper DLLs against `sys.base_prefix` (the
base interpreter) and/or `System32` — so installing a pywin32 **service** from a venv necessarily places
`pywintypes311.dll` next to the base interpreter, and its post-install places `pywintypes311.dll` /
`pythoncom311.dll` in `System32`. **The venv isolates `site-packages`, but not these global DLLs.** The
whole reason for the dedicated venv (Report B: "do not modify the production interpreter") is defeated for
the service host.

### Impact on production: NONE observed, and the writes are inert for the running bridge

- The bridge runs `C:\Program Files\Python311\python.exe C:\GuvFX\mt5_signal_bridge.py` — **stdlib, no
  pywin32.** `Get-Process -Id 13292 -Module` shows **no `pywintypes`/`pythoncom` module loaded**. A new DLL
  file on disk cannot affect an already-running process, and the bridge does not import these modules, so
  even a restart would not load them. The bridge did **not** restart (started `2026-07-22T08:33:09Z`,
  unchanged) and still owns `:8788`.
- So the writes are real but currently inert. They are still **unauthorised modifications of the production
  interpreter directory and System32**, which the packet forbids.

## I must own a gap in Report B

Report B claimed "the base interpreter was NOT modified" and verified `site-packages\win32` absent and the
`Program Files\Python311` directory timestamp. **It did not check `System32`**, where the venv post-install
had just written the two DLLs at 05:38. The claim was too strong: `site-packages` was untouched, but the
global helper DLLs were not. I record this as a measurement gap, per the evidence rule.

## State now — unchanged except the two problems above

```
service GuvFXBetaAgent  Stopped, ProcessId=0, StartName=LocalSystem   (installed, never ran)
firewall GuvFX-Beta-*   ABSENT (firewall step not reached)
bridge python           pid 13292, owns 8788, started 2026-07-22 (unchanged)
production MT5           pid 4336, Session 3 (unchanged)
beta tasks              8/8 Disabled; 0 beta terminal64
```

## Why this needs a decision, not autonomous remediation

The recovery is straightforward mechanically (uninstall.ps1 removes the service; the three DLLs can be
deleted; the bridge is unaffected). But the **approach** must change, and that is a design choice:

1. **Accept the global DLLs.** pywin32 services universally require `pywintypes`/`pythoncom` in System32 /
   next to the base python. If that is acceptable (they are standard, signed, inert for the bridge), keep
   pywin32 and just fix the `obj=` assignment. Simplest; but it means the "no production interpreter
   modification" rule cannot hold for a pywin32 service.
2. **Drop pywin32; use a non-Python service wrapper** (NSSM / WinSW) that runs the venv python as a child
   and writes nothing global. Preserves isolation; adds a dependency and a new artefact to review.
3. **Install pywin32 into the base interpreter deliberately** (abandon the venv for the service host),
   since the DLLs go global regardless. Honest about what pywin32 needs; loses venv isolation.

Each also needs the `obj=` fix (checked result + robust virtual-account assignment).

**No remediation taken.** The mis-installed LocalSystem service is left Stopped (it does nothing), the DLLs
left in place, pending the decision. On direction I will: fix `install_service.ps1` (and the chosen
approach) through the pipeline, then run `uninstall.ps1` to remove the service, then clean the DLLs, then
re-install.

---

## Recovery performed (2026-07-24T06:46Z) — service removed, DLLs left, per decision

Nuno's decision: host the service via a **WinSW wrapper** (see `docs/B3P_SERVICE_HARNESS_COMPARISON.md`);
**remove the mis-installed service, leave the DLLs**.

`uninstall.ps1` was **NOT** used — it is a full teardown (unregisters the 8 tasks, disables/removes the 4
identities, revokes `SeBatchLogonRight`) and would have destroyed the verified Phase 2A pool. A **scoped**
removal was used instead:

```
sc.exe delete GuvFXBetaAgent   ->  [SC] DeleteService SUCCESS (exit 0)
```

Post-recovery, verified:

```
beta_service          ABSENT
beta_identities       4        (pool intact)
beta_tasks_disabled   8/8      (pool intact)
service-SID ACL grant present  (left in place, reused by the WinSW install under the same account)
bridge python         pid 13292, owns 8788   (unchanged)
production MT5         pid 4336, Session 3    (unchanged)
DLLs                  left in place per decision (System32 + Program Files\Python311)
```

The host is back to the pre-service-install state except for the (retained) service-SID ACL grants and the
(retained) pywin32 helper DLLs. Next: implement the WinSW harness through the pipeline, then re-install.

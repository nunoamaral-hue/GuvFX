# B3P-2 — Unprivileged PID→SessionId mechanism comparison (host evidence)

- Date: 2026-07-25
- Host: `WIN-RD8VDS93DK7`, Windows Server 2025, 8.3 name creation **ENABLED** on `C:`.
- Requirement: the beta observation layer must, from the **least-privilege** service identity
  `NT SERVICE\GuvFXBetaAgent` (Session 0), determine the **session** of an *unopenable* same-name
  (`terminal64.exe`) candidate so it can EXCLUDE the operator's production terminal (a different session)
  and resolve an empty slot to `ABSENT` rather than fail-closed `UNAVAILABLE`.
- Task: **prove whether any DOCUMENTED unprivileged mechanism satisfies the requirement before selecting an
  undocumented system API** (Nuno, 2026-07-25). Tested on the real host, positive+negative controls, under
  BOTH Administrator (characterisation) and the actual service identity (authoritative).
- Test target: production MT5 `terminal64.exe` **pid 4336**, actually in **Session 3** (admin);
  observer/service session = **0**. A correct mechanism must report **3** for pid 4336 from the service.
- Service-context method: a temporary diagnostic scheduled task registered with principal
  `NT SERVICE\GuvFXBetaAgent` (LogonType Password, RunLevel Limited), writing to a service-writable path
  (`…\slots\_ev`, since the service has **no** rights on `C:\GuvFX\beta`), then removed. `whoami` inside the
  task confirmed `nt service\guvfxbetaagent`. No MT5, slot, ACL or privilege change; fully reversible.

## Result summary

| Mechanism | Documented | Min access | Works under service acct | Returns SessionId | Returns proc identity | Perf | Stability | Maint. risk |
|---|---|---|---|---|---|---|---|---|
| **`ProcessIdToSessionId(pid)`** | Yes | `PROCESS_QUERY_(LIMITED_)INFORMATION` on target | **NO** — `err 5 ACCESS_DENIED` for the admin proc (own pid works → 0) | Yes (if allowed) | No | Fast | High | Low |
| `WTSEnumerateProcesses` | Yes | TS query on the WTS server | **NO** — denied to the service (the original OBS-0015 blocker) | Yes (per proc) | pid+name+sid | Fast | High | Low |
| `CreateToolhelp32Snapshot` / `Process32*` | Yes | none (system snapshot) | Yes (enumerates) | **No** — `PROCESSENTRY32W` has **no** SessionId field | pid+name+ppid | Fast | High | Low |
| `WTSEnumerateSessions` | Yes | WinStation enumerate | wrapper arg error (87) in this pywin32; **no pid→session** regardless | session list only | No | Fast | Med | Low |
| `WTSQuerySessionInformation` | Yes | query on a session | given a session id, not a pid; **no pid→session** | per-session info | No | Fast | High | Low |
| `WTSGetActiveConsoleSessionId` | Yes | none | Yes (returned 1) | console session only; **no pid mapping** | No | Fast | High | Low |
| `QueryFullProcessImageNameW` (+ open) | Yes | `PROCESS_QUERY_LIMITED_INFORMATION` on target | **NO** — cannot open the admin proc | No (path only) | path (if openable) | Fast | High | Low |
| `NtQuerySystemInformation(SystemProcessInformation)` | **No (undocumented)** | none (system query) | Yes (callable; enumerated 205–209) | Yes, **but** the SessionId struct offset is OS-version-dependent → the probe read **0** (wrong) for pid 4336 | pid+base name | Fast (one call) | **Med** (offset drift per OS build) | **High** (undocumented; per-build offset maintenance) |
| **WMI `Win32_Process.SessionId`** | **Yes** | WMI query on `root\cimv2` (Authenticated Users by default) | **YES** — returned **`3`** for pid 4336 under `nt service\guvfxbetaagent`; enumerated all 207 procs | **Yes (correct)** | pid, Name, ExecutablePath, … | Slower (CIM query; one filtered/whole query per observe is acceptable for the low-frequency lifecycle path) | High (documented, stable schema) | Low |

## Raw evidence

Administrator context (characterisation):
```
{"target_pid":4336,"whoami_session_of_self":0,"ProcessIdToSessionId":3,
 "WTSGetActiveConsoleSessionId":1,"WTSEnumerateSessions":"EXC:error:(87,...incorrect)",
 "NtQuerySystemInformation":{"status":0,"process_count":205,"session_of_target":0}}
WMI Win32_Process.SessionId(4336) = 3
```

Service context — `NT SERVICE\GuvFXBetaAgent` (authoritative):
```
whoami                 = nt service\guvfxbetaagent
wmi_session_of_4336    = 3           <-- documented mechanism, CORRECT, under the service account
wmi_all_count          = 207
ProcessIdToSessionId   = FAIL err=5  <-- ACCESS_DENIED (the blocker)
WTSGetActiveConsoleSessionId = 1
WTSEnumerateSessions   = EXC err 87
NtQuerySystemInformation = status 0, 209 procs, session_of_target = 0  (WRONG offset — undocumented)
```

## Conclusion

**A documented mechanism satisfies the requirement under the real service identity: WMI
`Win32_Process.SessionId`.** It is Microsoft-documented, needs no per-process handle and no service
privilege/ACL grant, works from `NT SERVICE\GuvFXBetaAgent`, and returned the correct session (3) for the
operator's admin process — the exact case `ProcessIdToSessionId` is denied. Every other documented option
either cannot yield a pid→session mapping (`Toolhelp`, `WTSEnumerateSessions/QuerySessionInformation`,
`WTSGetActiveConsoleSessionId`, `QueryFullProcessImageName`) or is denied to the service (`ProcessIdToSessionId`,
`WTSEnumerateProcesses`).

Per the governing directive — *"if a documented solution exists, use it; only if every documented option is
conclusively ruled out should `NtQuerySystemInformation` become the recommendation"* — the **undocumented
`NtQuerySystemInformation` is NOT required** and is not recommended (its correctness here already depends on
an OS-version-specific struct offset that the probe read wrong). **No ADR amendment accepting an undocumented
dependency is needed.**

**Recommended next step (a separate code packet, through the pipeline):** in the observation session
pre-filter, obtain a pid→session map via **one WMI `Win32_Process` query** (via `win32com`, already in the
agent venv) instead of per-process `ProcessIdToSessionId`; keep the exact fail-closed semantics (a session
that still cannot be determined leaves the candidate unresolved → `UNAVAILABLE`, never `ABSENT`). Cost: a
single CIM query per observe on a low-frequency lifecycle path. No ACL/privilege change.

## Boundary / safety

No ACL, privilege, production, slot-identity or slot-1 change was made. Production MT5 (pid 4336) and the
bridge (pid 13292) were untouched throughout; slot 1 remained preserved (1340 items). The diagnostic task
and its temp directory were removed; the beta service is Running the merged `2026-07-25.2` code.

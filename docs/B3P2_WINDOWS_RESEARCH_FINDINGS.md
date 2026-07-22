# CVM-Inc-3 B3P-2 — Windows API research findings

**Provenance and its limits.** Documentation-only research across five surfaces (Task Scheduler, process
observation, filesystem, golden-copy integrity, service context), 192 claims, of which 173 received an
independent adversarial verdict. **Nothing here was executed.** No Windows host was contacted. Every
statement below is what Microsoft (or CPython source) documents, not what `WIN-RD8VDS93DK7` does.

The verifier was instructed to default to "partially wrong" rather than rubber-stamp, so the raw counts
(33 CONFIRMED / 140 PARTIALLY_WRONG) understate agreement: most "partially wrong" verdicts confirm the API
names, constants and values verbatim and correct a *surrounding* claim — sourcing, scope, or an omitted
caveat. The categorisation below reflects the substance, not the label.

---

## 1. Confirmed — safe to implement against

### Filesystem / move
- **`MoveFileExW`: "When moving a directory, the destination must be on the same drive."** Not
  flag-dependent; `MOVEFILE_COPY_ALLOWED` does not help, because it is documented to *simulate* the move
  via `CopyFile` + `DeleteFile`. Flag values confirmed exactly: `REPLACE_EXISTING`=1, `COPY_ALLOWED`=2,
  `DELAY_UNTIL_REBOOT`=4, `WRITE_THROUGH`=8, `CREATE_HARDLINK`=16, `FAIL_IF_NOT_TRACKABLE`=32.
- **CPython's `os.rename`/`os.replace` call `MoveFileExW` without `MOVEFILE_COPY_ALLOWED`** — verified in
  `Modules/posixmodule.c` (`flags = is_replace ? MOVEFILE_REPLACE_EXISTING : 0`). They therefore **cannot**
  degrade into copy-plus-delete.
- **`shutil.move` silently degrades.** `Lib/shutil.py` is `try: os.rename(...) except OSError:` with no
  errno/winerror inspection, so *any* `OSError` — including a permission error — turns an atomic rename into
  `copytree` + `rmtree`. Verified by source inspection *and* by execution. **This is a decisive finding: a
  tombstone implemented with `shutil.move` is a copy-plus-delete waiting to happen.**
- **`MOVEFILE_REPLACE_EXISTING` cannot replace an existing destination directory** ("If lpNewFileName names
  an existing directory, an error is reported").
- **`os.path.realpath` in its default `strict=False` mode swallows errors** — CPython falls back to
  `_getfinalpathname_nonstrict`, which tolerates a documented list of winerrors and returns a *partially
  resolved* path. It is **not** a containment primitive in that mode.
- **`os.path.islink()` returns `False` for directory junctions** (CPython 3.8 What's New, verbatim). Any
  containment or walk gate built on `islink` is bypassed by a junction.
- **`os.path.isjunction()` is 3.12+**, absent in 3.11. The 3.11 route is
  `os.lstat().st_reparse_tag` vs `stat.IO_REPARSE_TAG_MOUNT_POINT`.
- **Comparing drive letters is not a valid same-volume test** — a directory can be a mounted folder for a
  different volume (`GetVolumePathNameW` Remarks give the worked `C:\Mnt\Ddrive` example).
- `GetFileAttributesW` / `FILE_ATTRIBUTE_REPARSE_POINT` = 0x400; `FILE_FLAG_BACKUP_SEMANTICS` = 0x02000000
  is required to open a directory handle; `GetFinalPathNameByHandleW` flag values
  (`FILE_NAME_NORMALIZED`=0x0, `VOLUME_NAME_DOS`=0x0, `VOLUME_NAME_GUID`=0x1) — all exact.

### Process observation
- **`GetProcessTimes` `lpCreationTime` is a `FILETIME`**: 64-bit count of 100-ns units since 1601-01-01 UTC.
- **`(pid, creation_time)` is Microsoft's own process-identity construct** — `RM_UNIQUE_PROCESS` is
  documented as "Uniquely identifies a process by its PID and the time the process began", with
  `ProcessStartTime` defined as the value from `GetProcessTimes`. *(This is a strengthening: the researcher
  called it an undocumented inference; the verifier found it documented.)*
- **`PROCESSENTRY32W.szExeFile` is a bare file name, not a path.** Matching a process by executable name is
  a silent wrong-answer risk — which is exactly why the design matches by image-path containment.
- **`QueryFullProcessImageNameW`** is the documented way to get another process's Win32-format image path;
  needs `PROCESS_QUERY_INFORMATION` **or** `PROCESS_QUERY_LIMITED_INFORMATION`.
- **`ProcessIdToSessionId` requires `PROCESS_QUERY_INFORMATION`** (the full right), per its Remarks.
- **`psutil.create_time()` loses precision** — it converts to a float of seconds since 1970
  (`(ticks - 116444736000000000) / 10000000`). 58% of tick values do not round-trip. **psutil is unusable
  for process-birth identity.**
- `CreateToolhelp32Snapshot` `TH32CS_SNAPPROCESS` = 0x00000002; a 32-bit caller snapshotting a 64-bit
  process fails with `ERROR_PARTIAL_COPY` (299).
- `WTSEnumerateProcessesExW` level 1 returns SessionId, ProcessId, name and user SID in one call.
- **`NtQuerySystemInformation` is documented as internal and subject to change** — not usable.

### Task Scheduler
- ProgID **`Schedule.Service`**; `ITaskService::Connect` → `GetFolder("\")` → `GetTask` / `GetTasks`.
- **`TASK_STATE`**: UNKNOWN=0, DISABLED=1, QUEUED=2, READY=3, **RUNNING=4**.
- **`TASK_LOGON_TYPE`**: NONE=0, **PASSWORD=1**, S4U=2, INTERACTIVE_TOKEN=3, GROUP=4, SERVICE_ACCOUNT=5,
  INTERACTIVE_TOKEN_OR_PASSWORD=6. For PASSWORD, "the password must be supplied at registration time"; for
  S4U, "no password is stored by the system **and there is no access to either the network or to encrypted
  files**" — which is why S4U was rejected.
- **`Run`/`RunEx` returning success proves only that the scheduler accepted the demand-start request.** It
  is not evidence that any process was created. *(Correction: judge on `SUCCEEDED(hr)`, not `hr == S_OK` —
  `S_FALSE` is a success HRESULT and `[MS-TSCH]` documents `SchRpcRun` returning `S_FALSE` on failure.)*
  **This independently validates the REQUESTED-vs-OBSERVED split already implemented.**
- **`IRunningTask::get_EnginePID` is the PID of the task *engine*, not of the executable in the task's
  action.** Using it as the MT5 pid would be wrong.
- `ITaskSettings::AllowDemandStart` governs whether a demand start is permitted — and
  `SCHED_E_START_ON_DEMAND` is documented **not** to be returned by `Run`/`RunEx`, so a suppressed demand
  start is **invisible in the return value**. Another reason a trigger is not proof.
- `SCHED_E_*` and `SCHED_S_*` constant tables verified verbatim (`SCHED_S_TASK_HAS_NOT_RUN` = 0x00041303,
  `SCHED_E_TASK_NOT_RUNNING` = 0x8004130B, `SCHED_S_BATCH_LOGON_PROBLEM` = 0x0004131C, …).
- **`SeBatchLogonRight` is required** for a `TASK_LOGON_PASSWORD` task's *principal* to launch. It is a
  **logon right, not a privilege** — it does not appear in `whoami /priv`; read it with
  `LsaEnumerateAccountRights` or `secedit /export`.

### Copy integrity
- **Robocopy exit ≥ 8 means failure; exit 1 ("All files were copied successfully") is the normal result of
  a healthy fresh copy.** `subprocess.run(..., check=True)` or any `rc != 0` gate **rejects the success
  path** and must not be used.
- `/MIR` = `/E` + `/PURGE`, and **`/PURGE` really deletes** destination files not in the source.
- `xcopy` is unsuitable (documented as superseded/limited).
- `hashlib.file_digest` exists in 3.11+.

### Service context
- pywin32's `SvcRun()` reports `SERVICE_RUNNING` **before** calling `SvcDoRun()`, so a slow `SvcDoRun` body
  cannot itself cause error 1053 — the exposure is module import and `__init__`, which run inside
  `SERVICE_START_PENDING`.
- Since pywin32 build 300, an exception escaping `SvcRun` reports `SERVICE_STOPPED` with a non-zero code.
- **Services run in session 0 and cannot interact with a user**; `NoInteractiveServices` defaults to 1. A
  non-interactive service uses window station `Service-0x0-3e7$\default` for LocalSystem.

---

## 2. Still uncertain — implemented fail-closed or deferred

| Question | Why it is unresolved | What the adapter does |
|---|---|---|
| Exact HRESULT for a **missing task** via `GetTask` | No Microsoft source maps a specific code to an absent task | Enumerate `GetTasks(0)` and test membership by name — absence from the enumeration is *positive* evidence, an error is not |
| Whether `ProcessIdToSessionId` succeeds with only `PROCESS_QUERY_LIMITED_INFORMATION` | Doc says the full right; the function takes a PID, not a handle | Request `PROCESS_QUERY_INFORMATION` first, fall back to LIMITED, and record which succeeded |
| `GetLastError` for `OpenProcess` on a genuinely dead PID | ERROR_INVALID_PARAMETER (87) is empirical, not documented | 87 → treat as gone (skip); **5 → treat as denied, never as absent** |
| Whether **8.3 aliasing** is enabled on the volume holding `C:\GuvFX` | Per-volume setting; unknowable off-host | Normalise both paths with `GetLongPathNameW`; if normalisation fails, **raise** rather than risk a wrong containment verdict |
| Whether **more than one** process can legitimately have an image beneath a slot | MT5 may spawn helpers — explicitly unknown | Prefer the exact `<slot>\terminal\terminal64.exe`; if several candidates and none is the terminal, **raise** rather than pick arbitrarily |
| Any supported way to prove **no open handles** beneath a directory | Restart Manager rejects directories (`ERROR_ACCESS_DENIED` at `RmGetList`) *and* cannot act across sessions from LocalSystem; `NtQuerySystemInformation` is undocumented; `openfiles` needs a reboot | `open_handles()` **raises**. The cleanup proof is therefore unmet and release is blocked. See §5. |
| Whether same-volume NTFS directory rename is **atomic under power loss** | Widely believed, documented nowhere | Not relied upon; the move is verified by re-observing the source path |
| Whether `FILE_ID_INFO` / `GetFileInformationByHandleEx` works on Server 2025 | Requirements table reads "None supported" | Not used; volume identity uses the GUID-path route |
| Robocopy codes 2–7 on a **fresh** copy | Never observed | Accept **only 0 or 1** into a destination proven empty; anything else fails |
| MT5 **portable marker** | MetaQuotes documents *no* on-disk marker — `/portable` is a per-launch command-line property | The marker is an explicit **GuvFX-authored** file placed in the golden image by the operator, plus a new read-only check that the launch task's arguments contain `/portable` |
| Whether the Task Scheduler **Operational channel** (event 129) is enabled | Host state | Not depended upon anywhere |
| Where task credentials are actually stored (LSA vs Credential Manager) | Only forum sources, no Microsoft reference | **Not recorded as fact** in `docs/SECRET_INVENTORY.md` |

---

## 3. Contradictions found between sources

1. **`OpenProcessToken`'s required access right.** The `OpenProcessToken` page (2022) says
   `PROCESS_QUERY_LIMITED_INFORMATION`; the newer *Process Security and Access Rights* page (2025) — which
   `OpenProcessToken` itself links to — says `PROCESS_QUERY_INFORMATION`. Genuine documentation conflict.
   → Resolved by requesting the stronger right first.
2. **`Run` vs `RunEx` on a disabled task.** Microsoft's pages contain three mutually inconsistent
   statements: `Run` is documented as "equivalent to `RunEx`", `Run` is documented to return
   `SCHED_E_TASK_DISABLED`, and `RunEx` is documented not to. → Resolved by **checking `Enabled`/`State`
   explicitly before triggering** rather than relying on either error path.
3. **`SCHED_E_ALREADY_RUNNING` as the collision signal.** The constant is real, but `[MS-TSCH]` specifies
   demand-start collision handling through `MultipleInstances` policy, not through this return.
   → Not used; collisions are detected by observation.
4. **`SCHED_E_SERVICE_NOT_LOCALSYSTEM` is not an HRESULT** — it is a bare `6200L` Win32 error, unlike every
   other constant on the same page, so its severity bit is clear and `FAILED()` would read it as success.
   → Not compared against.
5. **`icacls` syntax on Server 2025** — the current Learn page contradicts itself between its syntax block
   and its parameter table (`/inheritance:` vs `/inheritancelevel:`). → Irrelevant here: the adapter has no
   ACL-write method.
6. **Task Scheduler event-ID message strings** are quoted from *archived Server 2008/2008 R2* pages;
   Microsoft has not republished them for modern Server. Event 129 (`CREATED_TASK_PROCESS`), the only
   documented artefact binding a task instance to a PID, is in that archived set. → Not depended upon.
7. **Robocopy's exit code as a bitmask** originates in the Server 2003 Resource Kit; current Microsoft
   pages list only 0,1,2,3,5,6,7,8 and never use the word bitmask. → Only the one-directional documented
   statement ("≥ 8 indicates failure") is relied upon, and even then conservatively.

---

## 4. For the bounded on-box viability trial

In priority order. Items 1–3 decide whether the execution model works at all.

1. **Can a GUI MetaTrader 5 run correctly under a `TASK_LOGON_PASSWORD` task** with no interactive session
   for the runtime account? Microsoft documents that such a process lands in a non-interactive window
   station; whether MT5 initialises, renders and stays alive there is *not* answerable from documentation.
   Microsoft's own guidance for this situation is to ask the application vendor.
2. **Is `SeBatchLogonRight` granted automatically** on task registration for a local account, or must the
   operator grant it? Read-only checkable on the host via `secedit /export /areas USER_RIGHTS`.
3. **Which session** does the launched process land in, and does MT5 function there?
4. Does a **non-admin** agent identity retain enough access to read another local account's process image
   path, creation FILETIME, SID and session? (If not, the service-account decision changes.)
5. Is 8.3 alias generation enabled on the volume holding `C:\GuvFX`?
6. How many processes have image paths beneath a live slot? (Decides whether "one or None" is right.)
7. Does the terminate task reliably end MT5, and what settling time does `confirm_terminated` legitimately
   need before `process_still_running` means a real failure?
8. Actual robocopy exit codes from a real staging run of the real golden tree.
9. Do multiple portable MT5 instances coexist under different non-admin identities on **this** host — the
   load-bearing assumption of the whole per-slot model.
10. Registration state of `GuvFXBetaRuntime-N` / `GuvFXBetaRuntimeStop-N`: LogonType, UserId,
    `AllowStartOnDemand`, `MultipleInstances`, `RunLevel`, exec Command/Arguments/WorkingDirectory, DACL.
11. Whether pywin32 is installed and which CPython build the agent runs on (decides the real
    `pythonservice.exe` path a firewall `-Program` rule must match).
12. Whether MT5 is subject to any protected-process or security-product restriction that would make
    `OpenProcess` fail even for LocalSystem.

---

## 5. One decision this research forces, which is Nuno's to make

**The sixth cleanup proof (`no_runtime_handles`) has no supported implementation.** Every documented route
is disqualified: Restart Manager rejects directories outright *and* cannot act on another session from a
LocalSystem service; `NtQuerySystemInformation` is explicitly internal and subject to change; `openfiles`
requires a reboot to enable.

The adapter therefore **fails closed**: `open_handles()` raises, the proof is recorded unmet, and slot
release is blocked. That is safe and honest, but it means the lifecycle cannot complete on the box without
a decision. The options are:

- **(a)** Redefine the proof in terms of what *is* provable — the tombstone move succeeded and the slot
  directory is absent — and record `no_runtime_handles` as *not independently verifiable*;
- **(b)** Keep the proof and accept an explicit, audited **operator attestation** for it during the trial;
- **(c)** Keep the proof strictly and accept that release requires manual operator completion.

This touches the release protocol, so it is **Amber** and I am not choosing it unilaterally. Until a
decision is recorded, the code does (c) — the strictest option — by simply failing closed.

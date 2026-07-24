# CVM-Inc-3 B3P-2 — Windows adapter interface contract

> **SERVICE-HOST NOTE ([ADR 0013](ADRs/0013-beta-agent-service-host-winsw.md), 2026-07-24).** Where this
> contract names `service.py` as the "service harness / pywin32 SCM wrapper", that is no longer the service
> host: the agent runs under a **WinSW wrapper** (`SCM → GuvFXBetaAgent.exe → venv Python → agent.py`).
> `service.py` is retained in the bundle but is off the service path. pywin32 is still used by
> `win_slot_ops.py` (lazily, at runtime, from the venv). The layer boundary this contract defines is
> unchanged.

**Status:** contract agreed; real adapter implemented in `win_slot_ops.py` against it, using only
documented facts that survived adversarial verification (see `docs/B3P2_WINDOWS_RESEARCH_FINDINGS.md`);
**no method has ever executed on a Windows host.** Every claim below about *behaviour on the box* is an expectation to be measured at the
bounded viability trial, not an observation.

This is the implementation contract required before the real adapter was written (review requirement 4).
The fake adapter used throughout the test suite and the real adapter satisfy **the same contract** — that
is what makes the test suite evidence about the real system rather than about a mock.

---

## 1. Why this layer exists at all

The adapter is the **only** component that may call Win32, COM or the Task Scheduler (review requirement
6). Nothing above it may. Concretely, none of these may contain a Windows API call:

| Layer | File | May call Win32? |
|---|---|---|
| Occupancy / binding | `occupancy.py` | **No** |
| Sequencing, audit, integrity, evidence | `stores.py` | **No** |
| Lifecycle vocabulary | `lifecycle.py` | **No** |
| Lifecycle policy | `pool_op_impls.py` | **No** |
| Stages (read-only) | `win_primitives.py` | **No** |
| Stages (mutating) | `win_mutations.py` | **No** |
| Adapter **interface** | `win_ops.py` | **No** — the contract only |
| Adapter **implementation** | `win_slot_ops.py` | **Yes — every host operation** |
| Service harness | `service.py` | **Yes — Service Control Manager only** |

Two files may touch Win32, and only two. The interface is deliberately separate from the implementation so
every layer can import the contract without importing the host. `service.py` is a genuinely separate
concern: it is the pywin32 SCM wrapper that lets the agent run as a Windows service at all, and it performs
no slot, task or process work. Recording it as a second permitted file is honest; folding it into "the
adapter" would blur two different responsibilities (Rule 5).

A test enforces this by scanning the bundle's AST for imports of `win32*`, `win32com`, `pywintypes`,
`winreg`, `ctypes`, `subprocess`, `shutil`, `psutil` and `wmi`, plus calls to `os.system`/`os.popen`, and
failing if any appear outside `win_slot_ops.py` and `service.py`. A second test asserts the interface file
itself stays clean. The boundary is enforced, not merely described.

The practical payoff: **the adapter is the only component that needs Windows-specific testing.** Everything
above it is provably exercised off-host, which is why 1300+ tests can run on a Mac and still mean something.

---

## 2. General rules that bind every method

1. **A method takes a slot-derived path or a fixed task name. Never a runtime UUID, generation, occupancy
   id or job id.** A primitive that needs one of those to do its work is a design error — pass it the slot.
2. **A method reports facts; it never decides.** No adapter method may refuse an operation on policy
   grounds, quarantine anything, or "fix up" state it finds inconvenient.
3. **Absence and unavailability are different return values, never the same one.** A method that cannot
   observe must raise, so the caller records `*_observation_unavailable` rather than "not there".
   Returning `None`/`False` for an unreadable state is the single most dangerous mistake this layer can
   make, because the caller will honestly report an unproven claim.
4. **`PermissionError` is distinguished from every other exception** by the stages, so the adapter must
   raise it (not a generic `OSError`) when access is denied.
5. **No method logs, prints or raises anything containing a credential, token or full command line.**
6. **No method may create a user, register or modify a scheduled task, change autologon, alter a session,
   touch port 8788, or write outside `C:\GuvFX\beta`.** The install-only gate creates those objects; the
   agent only triggers and observes them.

---

## 3. Method contracts

Each entry gives: **In / Out / Failures / Side effects / Idempotency / Evidence / Security assumption.**

### 3.1 `golden_source_info() -> dict`
- **In** — nothing.
- **Out** — `{"digest": str, "manifest_version": str}` describing the approved golden MT5 image.
- **Failures** — raise if the golden image is missing or unreadable. Never return a partial dict.
- **Side effects** — none (read-only).
- **Idempotency** — pure.
- **Evidence** — feeds `source_digest_matches` and `source_manifest_version_matches`.
- **Security** — the golden image is operator-installed and read-only to the agent identity. If the agent
  could write it, every slot could be compromised at once.

### 3.2 `destination_info(path) -> dict`
- **In** — the fixed slot terminal path.
- **Out** — `{"digest", "executable_digest", "portable_marker", "ownership_marker"}`.
- **Failures** — raise on unreadable. Return falsy members for genuinely absent ones.
- **Side effects** — none.
- **Idempotency** — pure.
- **Evidence** — the four stage-copy post-checks; also the proof behind `ALREADY_COMPLETED`.
- **Security** — a directory digest is the only thing standing between a tampered runtime and a launch, so
  it must cover file contents, not timestamps or sizes.

### 3.3 `path_exists(path) -> bool`
- **Out** — existence only. **Never** infer "absent" from an access denial — raise instead.
- **Side effects** — none. **Idempotency** — pure.
- **Evidence** — `destination_absent`, `slot_directory_empty`, tombstone idempotency.

### 3.4 `real_path(path) -> str | None`
- **Out** — the fully resolved final path (reparse points followed), or `None` when the path does not exist.
- **Failures** — raise on unreadable.
- **Evidence** — `destination_not_reparse`, and the per-component reparse report in `inspect_filesystem`.
- **Security** — this is the junction-escape guard. A resolver that silently returns the input on failure
  would defeat it entirely; it must resolve or raise.

### 3.5 `same_volume(a, b) -> bool`
- **Out** — whether both paths live on the same volume.
- **Security** — comparing drive letters is **wrong**: a directory can be a mount point for another volume.
  The implementation must compare volume identity, not the first two characters of the path. A tombstone
  across volumes is a copy-plus-delete, which is exactly what "never delete" forbids.

### 3.6 `move_dir(src, dest) -> None` *(mutating)*
- **Side effects** — moves a directory tree. **The only method that relocates data. There is deliberately
  no delete method anywhere in the interface.**
- **Failures** — raise on cross-device, destination-exists, or sharing violation.
- **Idempotency** — not idempotent; the caller checks `path_exists` first and reports `ALREADY_COMPLETED`.
- **Security** — destination is validated by `assert_authorised_tombstone_dir` *above* this layer; the
  adapter must not "helpfully" create parents outside the given path.

### 3.7 `read_owner_tag(path) -> str | None`
- **Out** — the raw ownership marker, or `None` if genuinely absent.
- **Note** — an absent or corrupt marker is treated as a **mismatch** by the integrity gate, never as a
  free slot. The adapter's job is only to report what is there.

### 3.8 `write_owner_tag(path, raw) -> None` *(mutating)*
- **Side effects** — writes the marker file. Called **only after** a stage copy is proven complete.
- **Idempotency** — overwrite is safe; content is deterministic for a given occupancy.

### 3.9 `copy_golden(path) -> None` *(mutating)*
- **Side effects** — copies the golden image into the slot directory.
- **Failures** — raise on any incomplete copy. Success here is **not** trusted: the caller re-verifies with
  `destination_info` before the runtime is eligible for launch.
- **Idempotency** — not idempotent; the caller guarantees the destination is absent first.
- **Security** — must not follow reparse points out of the slot directory, and must not copy per-instance
  state (broker credentials, logins, profiles) from the golden image.

### 3.10 `query_task(name) -> dict | None`
- **Out** — `{task_name, run_as_identity, run_as_sid, executable, working_directory, logon_type,
  run_level, enabled, last_result}`, or `None` if the task does not exist.
- **Failures** — `PermissionError` for access denied; any other exception for an unreadable scheduler.
- **Evidence** — the whole of stage 1, including `task_definition_digest` drift detection.
- **Security** — read-only. The agent must never register, modify, enable or delete a task.

### 3.11 `run_task(name) -> bool` *(mutating)*
- **Out** — whether the scheduler **accepted the trigger**. This is emphatically *not* whether the target
  process started or stopped.
- **Failures** — `PermissionError` → `*_trigger_permission_denied`; other exceptions →
  `*_trigger_unavailable`; `False` → `*_trigger_rejected`.
- **Idempotency** — not idempotent at this layer; the caller observes first (`START` never triggers a
  second launch for a running runtime).
- **Security** — the name is derived from the slot number and validated against the beta task namespace
  before it arrives. The adapter must not accept an arbitrary command line — only a registered task name.

### 3.12 `task_running(name) -> bool`
- **Out** — whether an instance of the task is currently executing.
- **Evidence** — the `no_task_running` cleanup proof, checked for **both** the launch and terminate tasks.

### 3.13 `query_slot_process(slot_path) -> dict | None`
- **Out** — `{pid, created_at_filetime, image, image_digest, user_sid, session_id}` for the process whose
  **image path is beneath `slot_path`**, or `None` when genuinely none is running.
- **Failures** — `PermissionError` for denial; other exceptions for an unreadable enumeration. **Never
  return `None` because a query failed** — that would let the agent report a live runtime as terminated.
- **Evidence** — the whole of process-birth identity and every termination claim.
- **Security** — selection is by **image path containment**, never by executable name. Matching
  `terminal64.exe` by name would match the operator's production MT5.
- **Time** — `created_at_filetime` must be an integer FILETIME (100-ns ticks). A locale-formatted string is
  refused upstream, so returning one degrades the observation to `creation_time_unusable` rather than
  producing a wrong identity comparison.

### 3.14 `open_handles(path) -> bool`
- **Out** — whether any process holds an open handle beneath the path.
- **Honest limitation** — this is expected to be **best-effort**. It must fail *closed*: if it cannot
  determine the answer, it raises, and the caller records the cleanup proof as unmet rather than met.

### 3.15 `read_acl(path) -> dict | None`
- **Out** — an observation of the ACL for evidence purposes.
- **Side effects** — none. **The interface has no `set_acl`.** ACLs are established by the operator at the
  install-only gate; an agent that could widen an ACL could break slot isolation.

---

## 4. What the contract deliberately does **not** include

| Not present | Why |
|---|---|
| `stop_pid` / any process kill | Termination goes through the fixed per-slot terminate task, so the agent never needs the privilege to kill another user's process. |
| `launch_runtime` / any `CreateProcess` | Launching goes through the fixed per-slot launch task; the agent holds no runtime credential. |
| `delete_dir` / `rmtree` | Removal is a **move to tombstone**. There is no delete to call by accident. |
| `set_acl`, `register_task`, `create_user` | Install-time operator actions. Their absence from the interface is the enforcement. |

---

## 5. Security assumptions the contract rests on

1. The agent service identity can **trigger** the per-slot tasks but holds **no runtime password**;
   credentials live in Task Scheduler / LSA (`TASK_LOGON_PASSWORD`).
2. The pool identities, their tasks, their directories and their ACLs are created **once, by a human, at
   the install-only gate**. The agent creates no OS object.
3. The golden image is read-only to the agent identity.
4. The agent is non-admin. Every method above must work without administrative rights, or the design is
   wrong — **this is one of the primary things the viability trial must measure.**
5. Nothing in `C:\GuvFX\accounts`, `C:\GuvFX\terminals`, the operator's MT5 installation, port 8788 or port
   8787 is reachable through any method here; the namespace guard runs above the adapter and the adapter
   receives only slot-derived paths.

---

## 6. Open questions — for the bounded viability trial only

These cannot be settled off-host and must not be guessed:

1. **Can a GUI MetaTrader 5 run correctly under a `TASK_LOGON_PASSWORD` task** when the runtime account has
   no interactive session? This is the single question that decides the execution model.
2. **Is `SeBatchLogonRight` granted automatically** when such a task is registered for a local account, or
   must the operator grant it separately?
3. **Which session** does the launched process land in, and does that satisfy MT5?
4. Does a non-admin agent identity retain enough access to enumerate another local account's process image
   path, creation time, SID and session?
5. Is there any supported way to determine open handles beneath a directory from a non-admin process, or is
   `open_handles` permanently best-effort?
6. Does the terminate task reliably end MT5, and how long may `confirm_terminated` legitimately need to
   wait before `process_still_running` means a genuine failure rather than an in-progress shutdown?


---

## 7. Implementation decisions forced by the research

Recorded here because each one is a place where the obvious implementation would have been wrong.

| Decision | Why |
|---|---|
| `os.rename` for the tombstone, **never** `shutil.move` | `shutil.move` catches every `OSError` from `os.rename` and falls back to `copytree` + `rmtree`, turning the move into the copy-and-delete this design forbids. `os.rename` calls `MoveFileExW` with `dwFlags=0`, so it *cannot* degrade. The legacy B2 adapter had the `shutil.move` defect and was fixed in the same change. |
| `os.path.realpath(..., strict=True)` | The default non-strict mode silently tolerates a documented list of errors and returns a partially resolved path — useless as a containment guard. |
| `os.lstat`, never `os.path.exists` | `os.path.exists` returns `False` on an access denial, which would report an existing directory as absent. |
| Reparse points classified per entry, never `os.path.islink` | `islink()` returns `False` for directory junctions, so `os.walk(followlinks=False)` walks into them. |
| Robocopy accepted only on exit **0 or 1** | Microsoft documents only that "≥ 8 indicates failure"; "0–7 is success" is folklore. Codes 2–7 mean extras or mismatches, which cannot legitimately occur when copying into a destination already proven absent. Equally, `check=True` would reject exit 1, the normal success. |
| No `/MIR` | `/MIR` implies `/PURGE`, which really deletes destination content. |
| Volume **GUID** comparison, not drive letters | A directory can be a mounted folder for a different volume. |
| Raw `FILETIME` via `ctypes`, not `psutil` | `psutil.create_time()` is a float of seconds since 1970; 58 % of tick values do not round-trip. Process-birth identity needs the exact 100-ns value. |
| Task presence by **enumeration**, not by a `GetTask` error | No Microsoft source maps a specific HRESULT to an absent task, so a failed call cannot be read as "not there". Absence from the folder listing is positive evidence. |
| `Enabled`/`State` checked before triggering | Microsoft's pages are mutually inconsistent about whether `Run` or `RunEx` reports a disabled task. |
| `IRunningTask.EnginePID` discarded | It is the PID of the task *engine*, not of the executable in the task's action. |
| Both paths normalised with `GetLongPathNameW`, raising on failure | 8.3 aliasing is per-volume configurable and unknowable off-host; a containment verdict from possibly-aliased paths is worse than no verdict. |
| Ambiguous slot process **raises** | If several processes run from one slot and none is `terminal64.exe`, picking by enumeration order would bind the termination chain to an arbitrary process. |
| `open_handles()` **raises, always** | No supported API can answer it. See findings §5 — this blocks release and needs a decision. |
| Portable mode read from the launch task's `/portable` argument | MetaQuotes documents **no** on-disk portable marker; it is a per-launch command-line property. The in-tree marker is an explicit GuvFX artefact, not an MT5 one. |

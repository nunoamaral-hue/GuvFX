# B3P-2 — Phase 2A APPLY failure, root cause, and recovery PLAN

`WIN-RD8VDS93DK7`. The credentialed APPLY completed **every mutation correctly** and then aborted inside
its own VERIFY block. Nothing on the host is misconfigured.

## Root cause — a verification defect that could never pass

`install_pool.ps1` asked:

```powershell
if (@($task.Triggers).Count -gt 0) { throw "task '$t' has a trigger; expected on-demand only" }
```

`Get-ScheduledTask` returns **`$null`** for `.Triggers` on a trigger-less task, and **`@($null).Count` is 1**
in PowerShell — `@()` wraps the null into a one-element array. The check therefore fired on every task that
was *correct*, and could not have passed for any trigger-less task.

Confirmed on the host, alongside the control `@(@()).Count = 0`:

```
COM       Definition.Triggers.Count = 0
raw XML   <Triggers />                     (empty, self-closing)
schtasks  Schedule Type: On demand only | Start Time: N/A | Next Run Time: N/A
```

Every alternative was ruled out with evidence: the installer never passes `-Trigger`;
`Register-ScheduledTask` synthesised nothing (the XML element is empty, not populated); Task Scheduler did
not require one; the tasks did not pre-exist (`beta_tasks=0` captured minutes before APPLY).

**Note on the investigation.** The first inspection probe reproduced the identical bug, reporting
`trigger_count_XML = 1` via `@($x.Task.Triggers.ChildNodes).Count`. It was caught only because COM
disagreed. This is the third instance of one class in this programme — the `secedit` BOM false negative
(RULE 11), the `Write|Modify` substring false negative on the golden ACL, and now a false positive.

## State after the failure — all verified read-only

| | |
|---|---|
| Identities | `guvfx_b_slot1..4` exist, enabled, SIDs `…-1004`…`…-1007` |
| Groups | **`Users` only** — none privileged |
| `SeBatchLogonRight` | four slot identities **+ all three Windows defaults preserved** |
| Tasks | 8 exist, **Disabled**, **0 triggers**, correct exe/args/workdir/principal, LogonType `1` (PASSWORD), RunLevel `0` (Limited) |
| Ever run | **No** — `267011 SCHED_S_TASK_HAS_NOT_RUN` on all eight, 0 missed runs |
| Beta MT5 | none; the only `terminal64.exe` is production pid 4336 |
| Golden ACL | inheritance broken; only SYSTEM + Administrators FullControl; four slot SIDs `ReadAndExecute` only; **no `BUILTIN\Users`, no `CREATOR OWNER`** |
| Slot ACLs | each identity Modify on its own slot, **no ACE on any other slot** |
| `approved_tasks.json` | 8 entries, no BOM, ACL = Administrators F / SYSTEM F / service SID Read |
| Production MT5 | pid **4336**, Session 3 — unchanged |
| Bridge | pid **13292**, owns **8788** — unchanged |
| Beta service | **ABSENT** |

## The fix — merged at `306f443` (PR #186)

`Get-GuvfxCount` is null-safe, and the trigger check now corroborates it against the Task Scheduler COM
definition, refusing to judge if the two sources disagree. Proven by control on the host:

```
OLD idiom  @($t.Triggers).Count = 1     <- the bug
NEW Get-GuvfxCount              = 0     <- correct
POSITIVE control GuvFX_Autostart = 1    <- a task that DOES have a trigger
scalar 1 | empty 0 | null 0
```

The class was swept (`uninstall.ps1`, `firewall.ps1` — the latter failed *open* in an exposure gate) and a
conformance test now fails the build on any `@($x).Count`.

## RECOVERY PLAN

**It will do NEITHER of the two options offered. No task is removed, recreated or updated.** Every object
on the host is already correct and independently verified; the only thing that failed was the assertion
about them. The recovery re-runs the assertions alone.

```
Set-Location C:\GuvFX\beta\agent
.\install_pool.ps1 -VerifyOnly -GoldenDir 'C:\GuvFX\golden\newMT5'
```

| Requirement | How it is met |
|---|---|
| No identity recreated | `New-LocalUser` is inside a `DoIt` block; `DoIt` is gated on `$Mutate = [bool]$Apply`, false here |
| No password requested | all three `Get-SlotSecret` call sites are inside `DoIt` blocks — asserted by a brace-matching test |
| No task registered or removed | `Register-ScheduledTask` / `Disable-ScheduledTask` are inside `DoIt` |
| No ACL written | `Invoke-GuvfxIcacls`, `Set-Acl`, `WriteAllText` all inside `DoIt` |
| No user right changed | `Grant-GuvfxBatchLogonRight` gated on `$Mutate` |
| Verification still runs | three blocks gated on `$Check = $Apply -or $VerifyOnly`, each pinned by a test |

`-Apply` and `-VerifyOnly` are mutually exclusive; passing both is a refusal.

**What it asserts:** all four identities exist and hold `SeBatchLogonRight`; none privileged and all in
`Users`; eight tasks present, Disabled, no trigger (corroborated across two sources), correct principal and
run level; the launch task carries `/portable`; **the terminate task is scoped to its own slot executable
and is not a name-only `Get-Process | Stop-Process` pipeline**; the golden ACL on all seven approved
properties plus its tree digest; per-slot ACLs with no cross-slot access; and the five estate tasks present
with unchanged principals.

**Expected final line:**

```
ok   pool VERIFIED. Nothing was created, changed or started by this run.
```

Any `throw` is a STOP: report it and do not proceed to `install_service.ps1`.

**Rollback is not warranted** and none is proposed. Nothing is misconfigured, nothing has run, and no
estate object was touched. `uninstall.ps1` remains available if the sponsor decides otherwise.

## Not covered

- The trial question — whether a GUI MT5 runs under a `TASK_LOGON_PASSWORD` task with no interactive
  session — remains **unanswered**. No evidence here bears on it.
- `open_handles()` has no supported implementation; TOMBSTONE refuses before moving anything.
- `release()` is implemented but unwired; the pool exhausts after `pool_size` tombstones.

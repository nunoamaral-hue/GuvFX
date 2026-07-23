# B3P-2 — Phase 2A VERIFIED

`WIN-RD8VDS93DK7`, `2026-07-23T20:5x Z`. `install_pool.ps1 -VerifyOnly -GoldenDir 'C:\GuvFX\golden\newMT5'`
at main `6c3b922`. **Read-only: nothing was created, changed, started or prompted for.**

```
ok   pool VERIFIED. Nothing was created, changed or started by this run.
```

## The second verification defect, found by running it

The first authorised `-VerifyOnly` run **threw**:

```
golden image: 'S-1-5-21-...-1004' holds write-class rights (ReadAndExecute, Synchronize)
- unexpected writable principal - STOP
```

The host was correct; the check was not. **PowerShell variable names are case-insensitive**, so these are
one variable:

```powershell
$WRITEISH = (...Write -bor ...Delete -bor ...)            # the mask, 0xD0156
$writeish = (($raw -band [int]$WRITEISH) -ne 0) -or ...    # the per-ACE result
```

The first ACE — SYSTEM, FullControl — set the result to `$true`, overwriting the mask. `[int]$true` is 1,
so every later ACE was tested against bit `0x1` (ReadData), which `ReadAndExecute` contains. It threw on
the first slot identity and could never pass on a correct image.

Measured, not argued — instrumenting the script's own block:

```
ITER who=S-1-5-18       raw=0x1F01FF WRITEISH=0x1 writeish=True  inWriters=True
ITER who=S-1-5-32-544   raw=0x1F01FF WRITEISH=0x1 writeish=True  inWriters=True
ITER who=...-1004       raw=0x1200A9 WRITEISH=0x1 writeish=True  inWriters=False  -> THREW
```

`WRITEISH=0x1`, where the same eleven-operand expression in isolation gives `0xD0156`.

Fixed at `6c3b922` (PR #188): `$WriteMask` / `$hasWriteRight`, plus three further case-only pairs swept
(all cross-scope, none live) and a test that fails the build on any case-only collision in the four
scripts. **The fixed block was re-run against the real host ACL before it was committed** —
`mask=0xD0156` on every ACE, `write=False` for all four slot identities.

**This was the fourth check in this programme that could not produce its correct answer** (`secedit`
BOM-less UTF-16; the `Write|Modify` substring that missed `AppendData`/`CreateFiles`; `@($null).Count`;
this). All four were mine, and all four shared one cause: a check shipped without being exercised against
real state. The dry-run habit above is the correction.

## Acceptance — all 20 items

The script proves 14 of them. Six are **not** in the script and were captured separately, read-only; they
are marked `[sep]` so no claim rests on the script that the script does not make.

| # | Item | Result |
|---|---|---|
| 1 | four slot identities | `guvfx_b_slot1..4`, all exist |
| 2 | Users-only membership | in `Users`, in no privileged group — all four |
| 3 | `SeBatchLogonRight` | held by all four (1 right each) |
| 4 | default principals preserved | `*S-1-5-32-544,*S-1-5-32-551,*S-1-5-32-559` intact |
| 5 | eight tasks | all present |
| 6 | zero triggers | 0, corroborated `Get-ScheduledTask` vs COM |
| 7 | tasks disabled | all eight Disabled |
| 8 | tasks never run | `[sep]` all eight `267011 SCHED_S_TASK_HAS_NOT_RUN` |
| 9 | launch task pinned | `/portable`, correct exe/workdir/principal, logon 1, level 0 |
| 10 | terminate task pinned | recorded in approvals, all 7 identity fields |
| 11 | terminate scope correct | each scoped to its own slot image path; no name-only kill |
| 12 | golden ACL | inheritance broken; only Administrators+SYSTEM write-class; Admin+SYSTEM retained |
| 13 | slot ACLs | Modify on own slot, **no ACE on any other slot**, read-only on golden |
| 14 | tombstone ACL | `[sep]` protected; SYSTEM+Administrators FullControl; **0 slot ACEs** |
| 15 | approvals ACL | `[sep]` Administrators F / SYSTEM F / service SID Read — no write for the agent |
| 16 | approvals contents | `[sep]` 8 entries, no BOM, all logon 1 / level 0 / correct identity |
| 17 | golden digest | `3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10`, 584 files |
| 18 | production MT5 unchanged | `[sep]` pid **4336**, Session 3, started `2026-07-14T23:05:37Z` |
| 19 | bridge unchanged | `[sep]` pid **13292**, owns **8788**, started `2026-07-22T08:33:09Z` |
| 20 | beta service absent | `[sep]` **ABSENT** |

Plus: 0 beta `terminal64.exe`; 0 of 4 slots staged; all five estate tasks present with unchanged principals.

### One extra check, and a near-miss worth recording

Nothing in the acceptance list compares `approved_tasks.json` against what is actually registered — the
script asserts the registered definitions directly and never re-reads the file. Checked separately.

A first attempt reported **4 mismatches** on the terminate tasks. That was a **third false positive in the
same family**: `working_directory` is `""` in the file and `$null` from COM, and `"" -ne $null` is true in
PowerShell. The agent normalises `None → ""` (`win_slot_ops.py:459`), so it is unaffected — confirmed by
replaying the agent's own normalisation and `occupancy.TASK_IDENTITY_FIELDS` comparison against the live
tasks:

```
ok  GuvFXBetaRuntime-1 .. -4        matches approved on all 7 identity fields
ok  GuvFXBetaRuntimeStop-1 .. -4    matches approved on all 7 identity fields
agent_gate_would_pass_on_all_eight = True
```

`enabled` differs by design (approved `True`, installed `False`): the gate runs at first START, after the
tasks are enabled under a separate approval.

### `GuvFX_BridgeWatchdog` state

Reported `Running -> Ready` within the run. It is a watchdog and changes state on its own; the check
asserts **presence and principal**, and reports state without treating it as interference. Principal
unchanged on all five estate tasks.

## Phase 2A: COMPLETE

Infrastructure commissioning is done. Nothing has started; no task has run; no runtime is staged.

**Not proven, and unchanged:** whether a GUI MetaTrader 5 runs under a `TASK_LOGON_PASSWORD` task with no
interactive session — the trial question, on which no evidence here bears. `open_handles()` still has no
supported implementation (TOMBSTONE refuses before moving anything); `release()` is implemented but
unwired, so the pool exhausts after `pool_size` tombstones.

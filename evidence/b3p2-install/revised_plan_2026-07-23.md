# B3P-2 — revised PLAN at merged commit `2b2622e`

Captured `2026-07-23T10:24Z` from `WIN-RD8VDS93DK7`. **No identity, right, task, ACL, directory, service or
firewall rule was created or modified. Nothing was started.**

Supersedes `golden_and_plan_2026-07-23.md` §4. That PLAN was produced by an artefact that has since been
corrected; the differences are listed below and each is a defect that PLAN could not surface, because PLAN
never reaches the step that fails.

## Governance chain

| Step | Result |
|---|---|
| PR #181 merged | `55a1b24ce3bbba638c16e3cf5a6a9b7b8c4bf970` |
| PR #182 CI | 5/5 green (backend, frontend, governance, market-data-foundation, research-foundation) |
| PR #182 focused final review | 19 findings raised, 11 survived 2-vote verification, all actioned |
| PR #182 merged | **`2b2622e7d17639f0fe924770b450e1d3003e9076`** |
| Bundle re-staged | 26/26 files byte-identical to the merged source; 0 stray files |
| Manifest integrity | `2026-07-23.2`, 15 modules, **0 problems** |
| RULE 9 parser check | `install_pool` / `install_service` / `uninstall` / `firewall` — **0 errors, 0 non-ASCII bytes** each, PowerShell `5.1.26100.32522` |

## The defect that would have stopped the trial

`BETA_AGENT_GOLDEN_DIGEST` was **wrong**, and would have blocked the first MATERIALISE on a clean image.

The installer canonicalised the tree manifest with forward slashes and a culture-aware sort; the agent's
`win_slot_ops.tree_digest()` — the only consumer — uses `normalise()`, which forces backslashes and sorts
ordinally. Feeding the real 584 files through the agent's own function:

```
AGENT   (win_slot_ops.tree_digest, 584 real files)  3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10
RECORDED in config.example.json / STATUS / evidence  9458098538cbc613c4cd35ce1ad02ffbf03db3a0db10971082dadbc677d7ce32
```

`source_digest_matches` is the first entry in `STAGE_PRE_CHECKS`; a mismatch yields
`stage_copy_precheck_failed` with status `BLOCKED`. The trial would have failed at its first action, with
the install complete and the passwords already entered.

Both sides now agree, confirmed by running the corrected installer block on the host against the same tree:

```
installer 3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10
agent     3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10
```

## Approved golden image

| Field | Value |
|---|---|
| Path | `C:\GuvFX\golden\newMT5` |
| Build | `5.0.0.6036` |
| Files | 584 |
| `BETA_AGENT_GOLDEN_DIGEST` | `3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10` |
| `BETA_AGENT_GOLDEN_MANIFEST_VERSION` | `5.0.0.6036` |

## What changed since the superseded PLAN

| Defect | Consequence had it run |
|---|---|
| `icacls /grant "NT SERVICE\GuvFXBetaAgent:(R)"` → 1332 | the **last** step of APPLY throws, after four passwords; the approvals file left with inheritance stripped and no ACE |
| `install_service.ps1` grants the same account **before** `sc create` | Phase 2B aborts at its first grant |
| terminate task pinned nowhere, gated by nothing | the only scope on `Stop-Process -Force` was an unverified argument string, against a process sharing the production terminal's image name |
| golden tree never inheritance-broken | slot identities inherit `AppendData` + `CreateFiles` from `BUILTIN\Users`, plus inherit-only `CREATOR OWNER GENERIC_ALL` — files planted there propagate into every future slot |
| 6 of 8 `icacls` calls unchecked | a silently failed ACL reported as success |
| non-admin assertion failed **open** | an enumeration error read as "not a member" |
| 4 of 4 password BSTRs unfreed | plaintext resident in unmanaged memory for the session |
| golden digest canonicalisation | above |

## Revised PLAN — verbatim

`powershell -NoProfile -ExecutionPolicy Bypass -File C:\GuvFX\beta\agent\install_pool.ps1 -GoldenDir C:\GuvFX\golden\newMT5`

```
ok   namespace refusals pass (estate paths, estate tasks, identity + task prefixes)
ok   estate captured before any mutation (5 of 5 estate tasks present)
==> validate golden image (RULE 10: dedicated clean install, never the production terminal)
ok   structure: terminal64.exe present
ok   structure: no MQL5 (non-portable install; /portable creates it in the slot at first run)
ok   marker: .guvfx_golden_manifest present
ok   marker: .guvfx_portable present
ok   version: terminal64.exe 5.0.0.6036 matches the pinned build
ok   clean: bases\ holds only the shipped Default tree (537 installer files, no broker directory)
ok   provenance: 557 scanned file(s) contain no path from another runtime or user profile
ok   golden image validated: clean, versioned, correctly structured
==> LSA interop self-test (read-only policy handle; no account touched)
ok   LSA interop available (LsaOpenPolicy/LsaClose round-trip succeeded)
==> PLAN:  create non-admin identity 'guvfx_b_slot1' (password prompted, never a parameter)
==> PLAN:  create non-admin identity 'guvfx_b_slot2' (password prompted, never a parameter)
==> PLAN:  create non-admin identity 'guvfx_b_slot3' (password prompted, never a parameter)
==> PLAN:  create non-admin identity 'guvfx_b_slot4' (password prompted, never a parameter)
==> SeBatchLogonRight via the LSA policy API (adds one right to one account; no policy line is rewritten)
PLAN:  guvfx_b_slot1 does not exist yet; WOULD ADD SeBatchLogonRight after creation (enumerate path not exercised until the account exists)
PLAN:  guvfx_b_slot2 does not exist yet; WOULD ADD SeBatchLogonRight after creation (enumerate path not exercised until the account exists)
PLAN:  guvfx_b_slot3 does not exist yet; WOULD ADD SeBatchLogonRight after creation (enumerate path not exercised until the account exists)
PLAN:  guvfx_b_slot4 does not exist yet; WOULD ADD SeBatchLogonRight after creation (enumerate path not exercised until the account exists)
==> PLAN:  create slot + tombstone directories
==> PLAN:  break inheritance on C:\GuvFX\beta and set explicit ACLs
==> PLAN:  break inheritance on C:\GuvFX\golden\newMT5 so the slot grants below are the ONLY non-admin access
==> PLAN:  grant 'guvfx_b_slot1' Modify on C:\GuvFX\beta\slots\1 only
==> PLAN:  grant 'guvfx_b_slot1' ReadAndExecute on C:\GuvFX\golden\newMT5
==> PLAN:  grant 'guvfx_b_slot2' Modify on C:\GuvFX\beta\slots\2 only
==> PLAN:  grant 'guvfx_b_slot2' ReadAndExecute on C:\GuvFX\golden\newMT5
==> PLAN:  grant 'guvfx_b_slot3' Modify on C:\GuvFX\beta\slots\3 only
==> PLAN:  grant 'guvfx_b_slot3' ReadAndExecute on C:\GuvFX\golden\newMT5
==> PLAN:  grant 'guvfx_b_slot4' Modify on C:\GuvFX\beta\slots\4 only
==> PLAN:  grant 'guvfx_b_slot4' ReadAndExecute on C:\GuvFX\golden\newMT5
==> PLAN:  restrict C:\GuvFX\beta\tombstones to Administrators + SYSTEM
==> PLAN:  register 'GuvFXBetaRuntime-1' (disabled, no trigger, /portable, runs as guvfx_b_slot1)
==> PLAN:  register 'GuvFXBetaRuntimeStop-1' (disabled, no trigger, terminates ONLY this slot's image)
==> PLAN:  register 'GuvFXBetaRuntime-2' (disabled, no trigger, /portable, runs as guvfx_b_slot2)
==> PLAN:  register 'GuvFXBetaRuntimeStop-2' (disabled, no trigger, terminates ONLY this slot's image)
==> PLAN:  register 'GuvFXBetaRuntime-3' (disabled, no trigger, /portable, runs as guvfx_b_slot3)
==> PLAN:  register 'GuvFXBetaRuntimeStop-3' (disabled, no trigger, terminates ONLY this slot's image)
==> PLAN:  register 'GuvFXBetaRuntime-4' (disabled, no trigger, /portable, runs as guvfx_b_slot4)
==> PLAN:  register 'GuvFXBetaRuntimeStop-4' (disabled, no trigger, terminates ONLY this slot's image)
==> PLAN:  write approved task definitions to C:\GuvFX\beta\agent-state\approved_tasks.json

PLAN complete. Re-run with -Apply on the host to provision the pool (install-only, no start).
```

## PLAN acceptance criteria — checked line by line

**Shown, as required:** four identities `guvfx_b_slot1..4`; four launch + four terminate tasks, each fixed
to its matching slot and identity, none triggered; `SeBatchLogonRight` added to those four identities only;
`C:\GuvFX\beta\slots\<n>\`, the approved tombstone root, the approved task-definition file, and the
approved golden path `C:\GuvFX\golden\newMT5`.

**Absent, as required:** no production identity; no legacy `guvfx_u_*`; no production MT5 path; no
production scheduled task; no bridge file and no port 8788; no autologon change; no reboot; no service
start; no MT5 launch.

## Post-PLAN state — unchanged

```
beta_identities=0        beta_tasks=0        slots_root_exists=False
tombstones_exists=False  approved_tasks_exists=False   beta_service=ABSENT

SeBatchLogonRight = *S-1-5-32-544,*S-1-5-32-551,*S-1-5-32-559
  (read from a fresh export path; BOM present; 31 rights parsed as a positive control - RULE 11)

proc python     pid=13292 session=1  port 8788 owner=13292   (bridge, untouched)
proc terminal64 pid=4336  session=3                          (production MT5, untouched)
estate tasks: GuvFX_Autostart Ready | GuvFX_SignalBridge Running | GuvFX_BridgeWatchdog Ready
              GuvFX_LaunchMT5 Disabled | GFX_LaunchIS6 Ready   - all principal=Administrator
estate identities: guvfx_u_1, guvfx_u_6, guvfx_u_7            (legacy TX-1, untouched)
```

`GuvFX_BridgeWatchdog` reads `Ready` here and `Running` in an earlier capture today. It is a watchdog and
changes state on its own; the installer's estate comparison therefore treats a state change as reportable
but not as interference, and asserts on presence and principal instead.

## What PLAN proves, and what it does not

**Proves.** Namespace refusals hold. The golden image passes on the real host. LSA interop works against
the live policy. The plan is exactly four identities, four rights, eight disabled tasks and one approval
file, against the approved golden path.

**Does not prove.** No mutating path has executed. `Grant-GuvfxBatchLogonRight`, every ACL, the inheritance
break on the golden tree, task registration, the approvals file and its service-SID grant, and the whole
VERIFY block are unexercised until APPLY. Task registration is the first step that can fail on password
quality.

## Not covered

- No APPLY. Nothing exists.
- Whether a GUI MT5 runs under a `TASK_LOGON_PASSWORD` task with no interactive session remains
  **unanswered** — the trial question, on which no evidence here bears.
- `open_handles()` has no supported implementation; TOMBSTONE refuses before moving anything.
- `release()` is implemented but unwired; the pool exhausts after `pool_size` tombstones.

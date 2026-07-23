# B3P-2 — golden-image validation, promotion, and `install_pool.ps1` PLAN

Captured `2026-07-23` from `WIN-RD8VDS93DK7`. **No identity, task, right, ACL, service or firewall rule was
created or modified.** The only host writes are the two promotion marker files named below.

Companion to `baseline_2026-07-22.md` — **read that file's retraction first**, it corrects a finding this
programme previously relied on.

---

## 1. The rejected tree — `C:\GuvFX\golden\mt5\5.0.0.5833\`

Not promoted. It passed every filename-based check, then failed on file **contents**:

```
foreign provenance: 66 file(s) contain paths belonging to another installation -
                    this tree was COPIED from an existing runtime, not installed fresh (RULE 10)
              -> MQL5\experts.dat references a per-account runtime directory
```

The 66 absolute paths are rooted at `C:\GuvFX\terminals\account_001\instance\`. That is conclusive: the
tree was copied out of a live per-account runtime. It is exactly the RULE 10 violation the validator exists
to prevent, and every check that looked only at filenames had passed it.

**This is why the provenance scan reads file contents.** Without it the tree would have been promoted.

## 2. The validator was wrong three times before it was right

Each failure below was a **false positive against genuine MetaQuotes installer output** — my assumption
about what an MT5 install contains, not an observation of one. Recorded because the corrections are the
substance of the check:

| Rejected | Why that was wrong |
|---|---|
| `MQL5\` missing | A non-portable install keeps data under `%APPDATA%\MetaQuotes\Terminal\<hash>`. A genuine install legitimately has no `MQL5\` beside `terminal64.exe`; `/portable` creates one in the slot at first run. |
| `bases\` non-empty | It **ships** populated — `Bases\Default\{History,Mail,Symbols}`, 537 installer files written within two seconds of install. Only a **broker-named** directory beside `Default` proves a connection. |
| `MQL5\Profiles` + sample EAs present | Both ship. `Advisors`, `Examples`, `Free Robots` are MetaQuotes samples. |

Every check is now on a **file a run leaves behind**, never on a directory an installer creates.

## 3. The promoted image — `C:\GuvFX\golden\newMT5\`

Full pass, read-only, `install_pool.ps1 -ValidateGoldenOnly`:

```
ok   structure: terminal64.exe present
ok   structure: no MQL5 (non-portable install; /portable creates it in the slot at first run)
ok   marker: .guvfx_golden_manifest present
ok   marker: .guvfx_portable present
ok   version: terminal64.exe 5.0.0.6036 matches the pinned build
ok   clean: bases\ holds only the shipped Default tree (537 installer files, no broker directory)
ok   provenance: 557 scanned file(s) contain no path from another runtime or user profile
ok   golden image validated: clean, versioned, correctly structured
```

**Promotion** wrote exactly two files and nothing else: `.guvfx_golden_manifest` containing `5.0.0.6036`,
and an empty `.guvfx_portable`. The installation itself was not modified.

**Recorded identity of the approved image** (re-verified independently after promotion):

| Field | Value |
|---|---|
| Path | `C:\GuvFX\golden\newMT5` |
| Build | `5.0.0.6036` (`terminal64.exe` FileVersion == `.guvfx_golden_manifest`) |
| Files | 584 |
| `BETA_AGENT_GOLDEN_DIGEST` | `9458098538cbc613c4cd35ce1ad02ffbf03db3a0db10971082dadbc677d7ce32` |
| `BETA_AGENT_GOLDEN_MANIFEST_VERSION` | `5.0.0.6036` |

Digest method: every file sorted by full path, one line `<relative-path-lowercase-slash>|<bytes>|<sha256>`,
UTF-8, SHA-256 of the whole. Reproduce it before any APPLY to prove the image has not drifted.

### Open reconciliation — the golden path is **not** the configured default

`config.example.json` ships `BETA_AGENT_GOLDEN_DIR = C:\GuvFX\beta\golden`, and that directory does not
exist; running the installer with its default `-GoldenDir` aborts (correctly) before doing anything:

```
golden image not staged at C:\GuvFX\beta\golden - commission a DEDICATED CLEAN MT5 install (RULE 10)
```

The approved image is at `C:\GuvFX\golden\newMT5`, which the script's namespace guard already permits.
**Both** the APPLY invocation and the agent's `BETA_AGENT_GOLDEN_DIR` must name that path, or the installer
grants slot identities read access to one directory while the agent stages from another.

---

## 4. `install_pool.ps1` PLAN — full output, no `-Apply`

`powershell -NoProfile -ExecutionPolicy Bypass -File C:\GuvFX\beta\agent\install_pool.ps1 -GoldenDir C:\GuvFX\golden\newMT5`

```
ok   namespace refusals pass (estate paths, estate tasks, identity + task prefixes)
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

### What PLAN proves, and what it does not

**Proves.** The namespace refusals hold. The golden image passes on the real host. **LSA interop works
against the live policy** — `LsaOpenPolicy`/`LsaClose` round-tripped, so the mechanism that replaces
`secedit` is not a theory. The plan is 4 identities, 4 rights, 8 disabled tasks and one approval file.

**Does not prove.** `Grant-GuvfxBatchLogonRight` has never executed — the accounts do not exist, so the
write path and the before/after right-preservation assertion are unexercised until APPLY. Every ACL, task
registration and the approval-file write are likewise unexercised. Task registration is where the operator
password is used and is the first step that can fail on credential quality.

### Post-PLAN state — unchanged, captured after the run

```
beta_identities=0
beta_tasks=0
slots_dir_exists=False
tombstones_dir_exists=False
approved_tasks_exists=False
beta_service=ABSENT
SeBatchLogonRight=SeBatchLogonRight = *S-1-5-32-544,*S-1-5-32-551,*S-1-5-32-559   (unchanged; see the
                                     baseline retraction - this was never absent)
estate_proc python     pid=13292     (bridge, untouched)
estate_proc terminal64 pid=4336      (production MT5, untouched)
```

Production MT5 pid `4336` and bridge pid `13292` are the same processes recorded in the 2026-07-22
baseline. Neither was observed beyond existence, and neither was signalled.

---

## 5. Artefact integrity

The staged bundle on the host is byte-identical to the reviewed source:

```
C:\GuvFX\beta\agent\install_pool.ps1
  host  BF490F2D288586E3B26DACCCB9C8CCB2E53AE0ED253005A27C2E4926251B4ABA   35545 bytes
  repo  bf490f2d288586e3b26dacccb9c8ccb2e53ae0ed253005a27c2e4926251b4aba   35545 bytes
```

The PLAN above was produced by that exact file. The commit that follows it in Git adds only the
`SeBatchLogonRight` comment correction from §1 of the baseline retraction — no executable change — so the
APPLY must re-stage and re-verify the checksum before running.

## 6. Not covered

- No APPLY. No account, right, ACL, task, service or firewall rule exists.
- Nothing has been started. No MT5 has been launched under any beta identity.
- Whether a GUI MT5 runs under a `TASK_LOGON_PASSWORD` task with no interactive session remains
  **unanswered** — it is the trial question and no evidence here bears on it.
- `open_handles()` still has no supported implementation; TOMBSTONE refuses before moving anything.
- `release()` remains unwired; the pool would exhaust after `pool_size` tombstones.

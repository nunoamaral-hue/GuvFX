# Golden Promotion — Execution Evidence (Phase A, 2026-07-31)

**Result: PASS.** The validated candidate golden (build 5.0.0.6073) is now the active beta golden at
`C:\GuvFX\golden\newMT5`; the prior golden is retired-but-retained; the agent runs re-pinned to the new
image. Executed under the Sponsor's standing authorisation once all three objective conditions were true
(PR #246 review complete; CI GREEN on the HEAD commit; no unresolved review comments).

Host: `WIN-RD8VDS93DK7` @ 100.79.101.19 (Tailscale). All actions via
`ssh administrator@100.79.101.19 "powershell -NoProfile -EncodedCommand <UTF16LE b64>"`.

## Pre-conditions verified (all true before host execution)
- PR #246 review complete: 2 adversarial rounds; 4 findings fixed (73dfd9e service-SID Set-Acl; 9b06c5d SID-typed
  read-back; 21fbc64 test assertion). HEAD = `21fbc64`.
- CI GREEN on `21fbc64`: backend, frontend, governance, market-data-foundation, research-foundation — all pass.
- No unresolved review comments affecting correctness/safety/idempotency/rollback/security.
- Reviewed installer `install_pool.ps1` sha256 `f9368f33...`, 100042 bytes, ASCII-only (0 non-ASCII).

## Adversarial pre-flight review of the runbook (independent agent) — corrections applied
- **B1**: run `-ValidateGoldenOnly` on staging with the NEW installer BEFORE any mutation (read-only; proves 4/5 pass).
- **B2 (critical)**: step 6 must re-pin BOTH `BETA_AGENT_GOLDEN_DIGEST` AND `BETA_AGENT_GOLDEN_MANIFEST_VERSION`.
  Verified in code: `config.py:138-142` requires both non-empty; `win_mutations.py:86-88` BLOCKS MATERIALISE unless
  BOTH match. Host manifest was stale `5.0.0.6036`; staging `.guvfx_golden_manifest` = `5.0.0.6073`.
  **This is a mandatory correction to the Sponsor's literal step 6 (which named only the digest).**
- **B3**: remove firewall by exact `-Name '{GUID}'` only (never -DisplayName — would match production siblings).
- **W1**: stop the agent before the rename, start at step 6 (agent keyring+key_id live in Machine env — verified,
  so restart is safe).
- **W5 cleared**: staging's only explicit ACE is Administrators:Full; Users-write ACEs are inherited (removed by
  `icacls /inheritance:r`).

## Env-propagation positive/negative control (RULE 11)
The WinSW XML has no `<env>` block and the host last booted 2026-06-10, so whether a plain `Start-Service` sees a
newly-set Machine env var had to be proven, not assumed.
- **Negative control**: set `BETA_AGENT_GOLDEN_DIGEST=''`, started the agent → it FAILED to start with
  `config.ConfigError: slot_pool execution model requires BETA_AGENT_GOLDEN_DIGEST` (`config.py:139`), service Stopped.
- **Positive control**: set the correct new values, started the agent → **Running**, agent.log
  `agent started bind=100.79.101.19:8791` (07:31:40), no new error.
- Conclusion: `Start-Service` reads the current Machine registry env; SCM does NOT serve a boot-time cache here.
  The agent loaded exactly `db54d94a...` / `5.0.0.6073`.

## Executed steps (exact commands + actual results)
1. **Stage reviewed installer** — backed up host `install_pool.ps1` → `install_pool.ps1.bak-preprom-20260731T072340Z`
   (old sha `038d04ed`), scp'd reviewed file. Host sha `f9368f33...` == local; RULE-9 `[Parser]::ParseFile` = 0 errors;
   ASCII-only 0 non-ASCII.
2. **B1 pre-flight** — `install_pool.ps1 -ValidateGoldenOnly -GoldenDir C:\GuvFX\golden\staging` → PASS
   (build 6073, servers.dat accepted, markers present, 558 files no foreign provenance).
3. **Retire old golden** — re-verified pid 5912 = `newMT5\terminal64.exe`; `Stop-Process 5912 -Force` → gone;
   0 processes under newMT5. `Remove-NetFirewallRule -Name '{253F21AE-7109-4A89-A377-5796DC50530B}'` → rule count 0.
   `Stop-Service GuvFXBetaAgent -Force` → Stopped. Prod terminals 4336/8748 untouched.
4. **Pivot rename** — `newMT5` → `newMT5.retired-20260731T072529Z` (584 files, retained); `staging` → `newMT5`
   (585 files, manifest 5.0.0.6073). staging gone.
5. **Golden ACL** — `install_pool.ps1 -ApplyGoldenAclOnly -GoldenDir C:\GuvFX\golden\newMT5` → RULE-10 validated,
   "inheritance removed, Administrators + SYSTEM Full, 4 slot identities + service SID ReadAndExecute; no non-admin
   writer"; read-back verified; exit 0.
6. **Full VerifyOnly** — `install_pool.ps1 -VerifyOnly -GoldenDir C:\GuvFX\golden\newMT5` → `pool VERIFIED`;
   `golden: 585 files, tree digest db54d94a51e18b9f2d592042e7bddb2a11eb576a6a927ea15ab7eab0a6509db0` (exact match);
   inheritance protected; each slot modify-own-slot-only + read-only golden; launcher/tasks/estate all unchanged.
7. **Re-pin + start** — set Machine `BETA_AGENT_GOLDEN_DIGEST=db54d94a...` + `BETA_AGENT_GOLDEN_MANIFEST_VERSION=5.0.0.6073`;
   `Start-Service GuvFXBetaAgent` → Running; python child pid 6324; agent.log "agent started".

## Final host state (checksummed / read back)
| Field | Value |
|---|---|
| Active golden path | `C:\GuvFX\golden\newMT5` |
| Active golden files | 585 |
| Active golden manifest | `5.0.0.6073` |
| Canonical tree digest (VerifyOnly) | `db54d94a51e18b9f2d592042e7bddb2a11eb576a6a927ea15ab7eab0a6509db0` |
| Retired golden (retained) | `C:\GuvFX\golden\newMT5.retired-20260731T072529Z` (584 files) |
| `BETA_AGENT_GOLDEN_DIGEST` (Machine) | `db54d94a...` |
| `BETA_AGENT_GOLDEN_MANIFEST_VERSION` (Machine) | `5.0.0.6073` |
| `BETA_AGENT_GOLDEN_DIR` (Machine) | `C:\GuvFX\golden\newMT5` (unchanged) |
| Beta Agent service | Running (python pid 6324, start 07:31:39) |
| Strategy Tester firewall rule | removed |
| Host installer sha256 | `f9368f33...` (reviewed) |
| Rollback backup installer | `install_pool.ps1.bak-preprom-20260731T072340Z` (`038d04ed`) |

## Rollback (still available; retired golden retained until Customer Zero Runtime Running)
Stop GuvFXBetaAgent; rename `newMT5` → `staging`, `newMT5.retired-20260731T072529Z` → `newMT5`; set Machine
`BETA_AGENT_GOLDEN_DIGEST=3a7fa663...` + `BETA_AGENT_GOLDEN_MANIFEST_VERSION=5.0.0.6036`; restore host installer from
`install_pool.ps1.bak-preprom-20260731T072340Z`; start GuvFXBetaAgent.

## Limitations / not covered
- No MATERIALISE was exercised end-to-end (the beta-provisioner keyring is still EMPTY — Phase B). The new pins are
  proven LOADED by the agent (positive/negative control) but a real stage-copy against the new golden was NOT run.
- `%APPDATA%` runtime state of the candidate was not independently inspected; provenance rests on the clean-install
  origin + the in-tree RULE-10 validation (which passed).
- The retired golden was NOT deleted (Sponsor directive: retain until Customer Zero Runtime Running demonstrated).
- The `GuvFXBetaAgent.err.log` retains the negative-control ConfigError (07:31:07); it is superseded by the
  successful start (07:31:40) and left in place as immutable evidence.

# CVM-Inc-3 B3P-2 — INSTALL AUTHORISATION PACKET

**Phase 1 deliverable. Execution planning only — no implementation, no host contact.**
Nothing in this packet has been executed. `WIN-RD8VDS93DK7` has not been contacted at any point in B3P-2.

Approve this packet to authorise **Phase 2 (install)** and **Phase 3 (verification)**. Phase 4 (service
start) requires a further, separate approval.

---

## 0. The division of labour, stated first because it constrains everything else

**I cannot perform the credentialed steps.** Creating the four local accounts and registering their
scheduled tasks requires typing passwords. I do not handle passwords — not as parameters, not interactively,
not read from a file. `install_pool.ps1` prompts for them as `SecureString` precisely so that a human types
them into their own session and they never exist anywhere else.

So **Phase 2 splits**:

| Step | Who | Why |
|---|---|---|
| Stage the golden MT5 image | **Operator** | requires a MetaTrader installation and a judgement about which build is approved |
| `install_pool.ps1` (identities, rights, dirs, ACLs, tasks, approvals) | **Operator** | prompts for four passwords |
| Copy the agent bundle to the host | **Model**, if authorised | file transfer, no credentials |
| `install_service.ps1` | **Model**, if authorised | no credentials; virtual service account needs no password |
| `firewall.ps1` | **Model**, if authorised | no credentials |
| Phase 3 verification | **Model** | entirely read-only |

If you would rather run all four scripts yourself, that changes nothing about the packet — my steps become
"observe and record". **Say which you prefer when you approve.** Every script is dry-run by default, so each
one is run twice: once to read the PLAN, once with `-Apply`.

---

## 1. Corrections required before Phase 2 (small, bounded, not yet made)

Phase 1 authorises no implementation, so these are flagged rather than fixed. Both are in install artefacts
and both would mislead an operator following them.

| id | Defect | Correction |
|---|---|---|
| **P-1** | `config.example.json` is stale: it still shows the B2 `BETA_AGENT_ROOT` layout, has **none** of the `slot_pool` variables, and sets `BETA_AGENT_DRAIN_TIMEOUT_S=20` — which the pool model now **refuses at startup** (it must exceed the 30 s settle window). An operator following it would produce a service that cannot start. | update to the slot-pool variable set with `DRAIN_TIMEOUT_S=45` |
| **P-2** | `RUNBOOK.md` describes the B2 install order and does not mention `install_pool.ps1`, the approvals file, or the launch gate. | add the pool step and the approval file |

Neither blocks the *design* of the install. Both should land before anyone follows the documents.
**Requesting authorisation to make these two corrections as part of Phase 2 preparation.**

---

## 2. Exact execution order

Times are **estimates from the operations they perform**, not measurements. The golden-image copy dominates
and depends on the MT5 build size and disk.

### Pre-flight (model, no host contact) — ~5 min

| # | Action | Evidence |
|---|---|---|
| 0.1 | Confirm `main` is `7f98982`, `make check` RC=0 | test counts, commit sha |
| 0.2 | Record bundle checksums + `manifest.json` version (`2026-07-22.28`, 15 modules) | checksum list |
| 0.3 | Tag a rollback point: `git tag b3p2-preinstall` | tag sha |

### Phase 2A — Operator, in their own session

| # | Action | Est. | Rollback |
|---|---|---|---|
| 2.1 | Stage the golden MT5 image into `C:\GuvFX\beta\golden\`, clean of per-instance state | 10–20 min | delete the directory |
| 2.2 | Write `.guvfx_golden_manifest` (the approved image version) and `.guvfx_portable` | 1 min | delete the files |
| 2.3 | Compute the golden tree digest; record it — it becomes `BETA_AGENT_GOLDEN_DIGEST` | 1–3 min | n/a (read-only) |
| 2.4 | `install_pool.ps1` **dry-run**. Read the PLAN. Confirm every line is expected | 2 min | n/a (no changes) |
| 2.5 | `install_pool.ps1 -Apply`. Prompts for 4 passwords, each confirmed | 5–10 min | §4 R-3, R-4, R-5 |

**Stop after 2.5 and hand back.** The script's own VERIFY block asserts tasks disabled, no triggers, correct
principal, `RunLevel Limited`, identities non-admin — and refuses to continue if any fails.

### Phase 2B — Model (or operator), non-credentialed

| # | Action | Est. | Rollback |
|---|---|---|---|
| 2.6 | Copy the 15-module bundle + `manifest.json` to `C:\GuvFX\beta\agent\` | 1 min | delete the directory |
| 2.7 | Verify on-host checksums equal the repo's | 1 min | n/a |
| 2.8 | `install_service.ps1` **dry-run**; read the PLAN | 1 min | n/a |
| 2.9 | `install_service.ps1 -Apply` — service installed `start=demand`, recovery disabled, **STOPPED** | 2 min | §4 R-2 |
| 2.10 | `firewall.ps1` **dry-run** — this is the step that *refuses* if a pre-existing rule could expose :8791 | 1 min | n/a |
| 2.11 | `firewall.ps1 -Apply` | 1 min | §4 R-1 |
| 2.12 | Tailscale ACL: allow `100.119.23.29 → 100.79.101.19:8791`, deny all other peers | operator, 2 min | revert the policy |

**Secrets are NOT provisioned in Phase 2.** `BETA_AGENT_KEYRING` / `BETA_AGENT_KEY_ID` are provisioned only
at the Phase 4 start, per the existing runbook. The service is stopped and does not read them.

### Phase 3 — verification (model, read-only) — ~10 min

The §7 checklist of `docs/B3P2_INSTALL_ONLY_REVIEW.md`, parts A and B only. **Part C — the observation
probe — is explicitly NOT run in Phase 3** (the directive places it in Phase 5).

Total wall-clock: **~45–60 min**, dominated by 2.1.

---

## 3. Evidence collection points

Per §9 of the install-only review. Redacted: no passwords, no keyring material, digests and prefixes only.

| When | What |
|---|---|
| Before 2.1 | Task XML digests for the five `GuvFX_*` estate tasks; `netstat` for :8787/:8788 with owning PID **and creation FILETIME**; `secedit /export /areas USER_RIGHTS`; firewall rule inventory; uptime |
| Each script | Full **PLAN** transcript, then the `-Apply` transcript |
| After 2.5 | `icacls` for every directory; the 8 task definition digests; `approved_tasks.json` digest |
| After 2.9 | `sc qc`, `sc qfailure`, `Get-Service` (expect **Stopped**), `Win32_Service.StartName` |
| After 2.11 | `Get-NetFirewallRule` for the new rule; confirmation no rule touches 8787/8788 |
| Phase 3 | Repeat the "before" set and **diff**. Estate items must be byte-identical |

Filed as an evidence manifest under `evidence/` with an explicit "not covered" list.

---

## 4. Rollback points

Each is independent; none deletes runtime or audit data.

| id | After | Undo | Cost |
|---|---|---|---|
| R-1 | 2.11 | `Remove-NetFirewallRule GuvFX-Beta-Agent-In` | seconds |
| R-2 | 2.9 | `uninstall.ps1 -Apply` (service + rule + ACL revokes) | ~1 min |
| R-3 | 2.5 | `uninstall.ps1 -Apply` — unregisters **both** task families, revokes `SeBatchLogonRight`, **disables** the identities | ~2 min |
| R-4 | 2.5 | `uninstall.ps1 -Apply -RemoveIdentities` — also deletes the accounts | ~2 min |
| R-5 | 2.1 | delete `C:\GuvFX\beta\golden\` | seconds |
| R-6 | any | `git checkout b3p2-preinstall` — repo only; does not touch the host | seconds |

**Retained by every rollback:** `beta\slots\`, `beta\tombstones\`, `beta\agent-state\` (the evidence chain).

**Untouched by every rollback:** Nuno's terminal (Session 3), the bridge (:8788), :8787, autologon, startup
tasks, `C:\GuvFX\accounts`, `C:\GuvFX\terminals`.

---

## 5. Stop conditions

**Halt immediately, change nothing further, and return evidence** if any of these occurs.

### From the directive
- Observation cannot be performed under `NT SERVICE\GuvFXBetaAgent`
- MT5 requires autologon, or requires Administrator execution
- Scheduled tasks cannot execute under the approved identity
- Session 1 or Session 3 would be modified
- Any bridge interaction occurs
- Production MT5 is observed, or a production task is targeted
- An ACL boundary is violated
- Any evidence contradicts the approved architecture

### Specific to this install
- A script's **dry-run PLAN** shows any action not in §1 of the install-only review
- `firewall.ps1` reports a pre-existing rule that could expose :8791 → **resolve before continuing**
- The interface profile is not default-deny inbound
- `install_service.ps1` reports the identity as LocalSystem (means `obj=` failed)
- The golden tree digest does not match `BETA_AGENT_GOLDEN_DIGEST`
- Any estate evidence differs between the before and after captures
- `secedit` returns non-zero, or `SeBatchLogonRight` afterwards holds principals we did not add
- Nuno's `terminal64.exe` PID **or creation FILETIME** changes at any point

**No workaround is authorised for any of these.** I will not retry, adjust, or route around a stop
condition; I will stop and return what was observed.

---

## 6. Success criteria

Phase 2 + 3 succeed only if **all** hold:

- [ ] 4 identities created, non-admin, `SeBatchLogonRight` granted and verified
- [ ] Directories created; `icacls` matches the review's §5 table; no path component is a reparse point
- [ ] 8 tasks registered, **disabled**, no triggers, correct principal, `RunLevel Limited`, `/portable` on launch
- [ ] `approved_tasks.json` written, readable by the service account, BOM-free, digest recorded
- [ ] Golden image staged, digest matches, free of per-instance state
- [ ] Bundle checksums equal `manifest.json` (15 modules, `2026-07-22.28`)
- [ ] Service installed, `start=demand`, recovery disabled, identity `NT SERVICE\GuvFXBetaAgent`
- [ ] **Service STOPPED**
- [ ] One firewall rule; profile default-deny inbound; nothing touching 8787/8788
- [ ] Tailscale ACL applied
- [ ] Estate evidence byte-identical before and after
- [ ] Evidence manifest filed

And **none** of these occurred: MT5 launched, runtime staged into a slot, task triggered or enabled,
observation probe run, service started, reboot, autologon change, session change.

---

## 7. What Phase 2 deliberately leaves undone

Stated so that none of it reads as an omission later:

- **No secrets provisioned.** Keyring and key id come at Phase 4. The stopped service reads nothing.
- **No observation probe.** Phase 5.
- **No runtime staged, no task enabled, no task triggered.** Phases 5–6.
- **`open_handles()` still fails closed** — accepted Amber. TOMBSTONE will refuse at `precheck_cleanup` and
  move nothing.
- **`release()` still unwired** — accepted Amber. The slot stays allocated after a tombstone.

---

## 8. One thing worth weighing before you approve

**These scripts have never been executed.** They were reviewed twice and are covered by 44 conformance
tests, and that process found real defects — including one of mine that would have left the pool provisioned
and permanently unusable. But conformance tests check what a script *says it does*; they cannot check that
`icacls`, `secedit`, `Register-ScheduledTask` and `New-LocalUser` behave as expected on this host.

The dry-run PLAN pass at steps 2.4, 2.8 and 2.10 is the first real execution of this code, and it is
designed to be the safe one: it performs no changes, and `firewall.ps1`'s pre-existing-rule check runs in
dry-run too. **Read those PLAN outputs carefully before authorising each `-Apply`** — they are the last
review point where nothing has happened yet.

The one step that reaches beyond `C:\GuvFX\beta` is `secedit`, which rewrites machine-wide user-rights
policy. It is now encoding-correct and exit-code-checked, and the uninstall side refuses to write a result
that removed more than exactly our own SIDs. It remains the step I would watch most closely.

---

## 9. Approval requested

1. **Authorise Phase 2 + Phase 3** as specified above.
2. **State who runs the non-credentialed scripts** — me over SSH, or you.
3. **Authorise corrections P-1 and P-2** (stale `config.example.json`, stale `RUNBOOK.md`).

Phase 4 (service start) is **not** requested here and will be returned for separately.

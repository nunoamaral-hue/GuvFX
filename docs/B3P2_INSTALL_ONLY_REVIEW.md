# CVM-Inc-3 B3P-2 — INSTALL-ONLY REVIEW

**Status: submitted for approval. Nothing has been executed. No Windows host has been contacted at any
point in B3P-2.**

The gate question is *"if we install today, do we understand exactly what will happen?"*

**The honest answer is: yes — and what would happen is the wrong install.** The install scripts merged in
B2/B3P-1 (`install_service.ps1`, `firewall.ps1`, `uninstall.ps1`) predate the per-slot execution model. Run
today they would provision the B2 uuid-directory layout: no slot identities, no scheduled tasks, no golden
image, and ACLs on `C:\GuvFX\beta\accounts` rather than `C:\GuvFX\beta\slots\<n>`. **Section 11 lists
exactly what must change first.** This review documents the install as it must be, and marks precisely which
parts exist today and which do not.

---

## 0. Two corrections to my earlier draft

Recorded first because both were wrong in the version summarised at the last gate.

**C-1 — I withdraw the request for temporary LocalSystem.** The merged `install_service.ps1` already runs
the service as the virtual service account `NT SERVICE\GuvFXBetaAgent`, and it *hard-fails* if the identity
resolves to LocalSystem — the script's own words: *"LocalSystem means the obj= assignment failed; do NOT
start"*. I proposed an elevation that the existing, reviewed install explicitly refuses. Section 10 replaces
that request with what should have been there: a bounded observation-capability probe under the intended
account, and an elevation question that only arises if the probe fails.

**C-2 — the integrity manifest covers 15 modules, not 17.** `manifest.json` version `2026-07-22.26`.

---

## 1. Scope of the proposed host change

| # | Object | Action | Exists today? | Reversed by |
|---|---|---|---|---|
| 1 | 4 local accounts `guvfx_b_slot1..4` | create, non-admin | **No script** | §8 step 6 |
| 2 | `SeBatchLogonRight` for those 4 | grant | **No script** | §8 step 6 |
| 3 | `C:\GuvFX\beta\` tree (`slots\1..4\`, `tombstones\1..4\`, `golden\`, `agent\`, `agent-state\`) | create | Partial — `agent\`, `agent-state\` only | §8 step 5 |
| 4 | ACLs on that tree | set | Partial — wrong paths (§11 F1) | §8 step 4 |
| 5 | 8 tasks `GuvFXBetaRuntime-{1..4}` / `GuvFXBetaRuntimeStop-{1..4}` | register, **disabled** | **No script** | §8 step 3 |
| 6 | Golden MT5 image + `.guvfx_golden_manifest` + `.guvfx_portable` | stage, digest recorded | **No script** | retained (§8) |
| 7 | Agent bundle (15 modules + `manifest.json`) | copy to `C:\GuvFX\beta\agent\` | Yes | §8 step 5 |
| 8 | Service `GuvFXBetaAgent` | install, `start=demand`, recovery disabled, **not started** | Yes | §8 step 1 |
| 9 | Firewall rule `GuvFX-Beta-Agent-In` | create | Yes | §8 step 2 |

**Nothing else.** No change to autologon; no reboot; no touching Session 1 or Session 3; no change to
`GuvFX_Autostart`, `GuvFX_SignalBridge`, `GuvFX_BridgeWatchdog`, `GuvFX_LaunchMT5`, `GFX_LaunchIS6`; no
change to ports 8787/8788; no modification of `C:\GuvFX\accounts`, `C:\GuvFX\terminals` or the operator's
MT5 install; no stop or restart of Nuno's terminal.

---

## 2. Windows identities

### Created

`guvfx_b_slot1`, `guvfx_b_slot2`, `guvfx_b_slot3`, `guvfx_b_slot4` — one per pre-provisioned slot.

The agent **cannot** create these. It has no user-creation method, holds no credential, and the code derives
the identity name from the slot number alone (`win_primitives.RUNTIME_IDENTITY_PREFIX` +`<n>`), refusing any
name outside that namespace and refusing `administrator`, `system` and `guvfx-rdp` outright — compared on
the account component, so `.\Administrator` and `Administrator@host` are both caught.

### Permissions

- Member of `Users` **only**. Not `Administrators`, not `Remote Desktop Users`, not `Backup Operators`.
- No relationship to the existing estate identities `guvfx_u_1`, `guvfx_u_6`, `guvfx_u_7`, or `guvfx-rdp`.
- Passwords generated in the operator's own session. Never pasted into chat, never committed, never placed
  on a command line that reaches a log or process listing, never written to a file in the repository. They
  exist afterwards only inside Task Scheduler's credential store.

### `SeBatchLogonRight`

Granted **explicitly** to all four.

It is a **logon right, not a privilege** — it will not appear in `whoami /priv`. Verify with
`secedit /export /areas USER_RIGHTS` or `LsaEnumerateAccountRights`. Whether task registration grants it
automatically is genuinely ambiguous in Microsoft's documentation (trial item 2); granting it explicitly
makes the ambiguity irrelevant to the install.

`SeInteractiveLogonRight` and `SeServiceLogonRight` are deliberately **not** granted.

### Ownership

The slot identities own **nothing**. Every directory is owned by `Administrators` (§5). The slot identity
receives `Modify` on its own slot directory and `Read+Execute` on the golden image — no ownership, so it
cannot re-ACL anything, including its own tree.

### Removal procedure

1. Confirm no process runs as the identity (§7 step 14 probe, inverted).
2. Unregister its two tasks — this removes the stored credential with them.
3. Revoke `SeBatchLogonRight`.
4. **Disable** the account.
5. Delete only after evidence capture (§9). Deleting an account orphans every file it owns; because the
   identities own nothing, deletion is safe once the tasks are gone.

---

## 3. Scheduled tasks

Eight, registered by the operator, **disabled at install**. The agent may trigger and read them; it has no
method to register, modify, enable or delete one.

| Property | `GuvFXBetaRuntime-<n>` (launch) | `GuvFXBetaRuntimeStop-<n>` (terminate) |
|---|---|---|
| Principal | `guvfx_b_slot<n>` | `guvfx_b_slot<n>` |
| Logon type | `TASK_LOGON_PASSWORD` (1) | `TASK_LOGON_PASSWORD` (1) |
| Run level | Least privilege (not elevated) | Least privilege |
| Executable | `C:\GuvFX\beta\slots\<n>\terminal\terminal64.exe` | terminate scoped to this slot only |
| Arguments | `/portable` | — |
| Working directory | `C:\GuvFX\beta\slots\<n>\terminal` | — |
| Triggers | **none** — on-demand only | none |
| `AllowStartOnDemand` | true | true |
| `MultipleInstances` | `IgnoreNew` (2) | `IgnoreNew` (2) |
| Enabled at install | **false** | **false** |

**Why `TASK_LOGON_PASSWORD` and not S4U:** S4U stores no password, but Microsoft documents that it also
grants *"no access to either the network or to encrypted files"* — which an MT5 terminal needs.

**Why `/portable` is on the launch task:** MetaQuotes documents **no** on-disk marker for portable mode; it
is a per-launch command-line property. The task's arguments are therefore the authoritative signal.
`inspect_task` records `portable_switch` from the definition. The in-tree `.guvfx_portable` file is an
explicit GuvFX artefact placed in the golden image, not an MT5 one.

**Why `IgnoreNew`:** defence in depth against a duplicate demand start. `start()` already refuses to trigger
unless the slot is observed ABSENT.

### Definition digest

`occupancy.task_definition_digest` is a SHA-256 (16-hex prefix) over exactly seven fields, order-independent:

```
task_name, run_as_identity, executable, working_directory, logon_type, run_level, enabled
```

`assert_task_matches_approved(approved, installed)` raises `TaskDefinitionDrift` when the digests differ
(naming the differing fields) **or** when the installed task is disabled. The agent never repairs a task —
drift is a refusal.

At install, the 16 digests (8 tasks × enabled-false at install, plus the 8 expected enabled-true values for
first start) are recorded in the evidence pack (§9) and become the approved baseline.

> **Gap F3 (§11):** the digest machinery exists and is tested, but `inspect_task` and
> `assert_task_matches_approved` have **no caller in the lifecycle**. Today the agent would trigger a launch
> task without first asserting the task still matches its approved definition. This must be wired before
> first start, not before install.

---

## 4. Windows service

| Property | Value | Source |
|---|---|---|
| Name / display | `GuvFXBetaAgent` / "GuvFX Beta Provisioning Agent" | `service.py` |
| Identity | **`NT SERVICE\GuvFXBetaAgent`** — virtual service account, no password, stable SID | `install_service.ps1` |
| Startup | `demand` (manual). Never `auto` at install | `sc config … start= demand` |
| State after install | **Stopped**, asserted by the script (`throw` if not) | `install_service.ps1` |
| Recovery | **Disabled** — `sc failure … reset= 0 actions= ""`, verified by `sc qfailure` | a crash-loop must be visible, not auto-restarted |
| Host process | pywin32 `PythonService.exe` wrapping `GuvFXBetaAgentService`, interpreter `C:\GuvFX\python311.exe` | not a raw `sc create` on `agent.py`, which would fail SCM start (error 1053) |
| Integrity at start | `build_agent(enforce_integrity=True)` hashes all **15** bundle modules against `manifest.json`; any drift refuses to start | `agent.py` |
| Bind | `100.79.101.19:8791`, pinned to that **exact** address | `assert_exact_bind` |
| Drain on stop | reports `STOP_PENDING` with wait hint `drain + 10s`, waits for in-flight mutations | `service.py` |

**Error 1053 exposure is bounded:** pywin32 reports `SERVICE_RUNNING` *before* calling `SvcDoRun()`, so a
slow body cannot cause a start timeout. The exposure is module import and `__init__`, inside
`SERVICE_START_PENDING` — and the bundle imports only the standard library at module scope.

**Drain must exceed the settle window.** For the slot-pool model, `load_config` **refuses to start** unless
`BETA_AGENT_DRAIN_TIMEOUT_S` exceeds the 30 s settle window. Install must set it to **45**. Left at the 20 s
default, a service stop during a STOP operation would force-kill the mutation mid-stage.

### Required environment at install

| Variable | Install value |
|---|---|
| `BETA_AGENT_BIND_HOST` / `BETA_AGENT_EXPECTED_BIND_HOST` | `100.79.101.19` |
| `BETA_AGENT_BIND_PORT` | `8791` (8787/8788/3389 are refused by config) |
| `BETA_AGENT_EXECUTION_MODEL` | `slot_pool` |
| `BETA_AGENT_SLOT_POOL_SIZE` | `4` (≥1 required) |
| `BETA_AGENT_GOLDEN_DIR` / `_DIGEST` / `_MANIFEST_VERSION` | golden path, its tree digest, its version — **all three required**, empty refused |
| `BETA_AGENT_DRAIN_TIMEOUT_S` | `45` |
| `BETA_AGENT_KEYRING` / `BETA_AGENT_KEY_ID` | provisioned via the Windows secret store, never from Git |
| `BETA_AGENT_SLOTS_ROOT` | omitted — any value other than the fixed root is refused at startup |

---

## 5. Filesystem

### Layout

```
C:\GuvFX\beta\
  agent\                     the 15-module bundle + manifest.json         (agent identity: read+execute)
  agent-state\               state.sqlite, slots.sqlite, logs\            (agent identity: modify)
  golden\                    the approved MT5 image + .guvfx_golden_manifest + .guvfx_portable
  slots\<n>\terminal\        one runtime occupancy                        (guvfx_b_slot<n>: modify)
  tombstones\<n>\<occid>\    retained former runtimes                     (admins + SYSTEM only)
```

### Ownership and ACLs

| Path | Owner | Access |
|---|---|---|
| `beta\` | Administrators | Administrators FC, SYSTEM FC, **inheritance disabled** |
| `beta\golden\` | Administrators | Administrators FC; each `guvfx_b_slot<n>` **Read+Execute only** |
| `beta\slots\<n>\` | Administrators | Administrators FC, SYSTEM FC, `guvfx_b_slot<n>` Modify. **No other slot identity has any access.** |
| `beta\tombstones\<n>\` | Administrators | Administrators + SYSTEM only |
| `beta\agent\` | Administrators | Administrators FC, service account **Read+Execute** — it cannot rewrite its own code |
| `beta\agent-state\` | Administrators | Administrators FC, service account Modify |

The golden image being read-only to the slot identities is load-bearing: if one runtime could write it, one
compromised slot would compromise every future slot.

**The agent has no `set_acl` method.** That absence is the enforcement.

### Junction / reparse protection

Four independent layers, all in code today:

1. `assert_authorised_slot_input` rejects any path containing a `..` component outright — `is_beneath` is a
   *lexical* prefix test and would otherwise accept `…\slots\..\..\accounts`.
2. `mgmt_agent_core._resolve` resolves the runtime directory **and its parent** and refuses if either
   resolves outside the base — the parent too, because MATERIALISE creates the leaf, so gating on leaf
   existence would let a junction planted at the parent be written through first.
3. `stage_copy` requires `real_path(slots\<n>)` to be **non-None and contained**. A slot directory that does
   not exist is a refusal, not a pass: robocopy would otherwise create the whole chain and materialise into
   a slot nobody provisioned.
4. `_tree_digest` classifies every entry with `os.lstat` and raises on any reparse point inside a runtime
   tree. `os.walk(followlinks=False)` is **not** sufficient on Windows — `os.path.islink()` returns False
   for directory junctions, so junctions are walked into by default.

Path resolution uses `os.path.realpath(strict=True)`; the default non-strict mode silently returns a
partially-resolved path and is not a containment primitive.

### Rollback of filesystem state

Runtime directories are **moved**, never deleted — there is no delete method in the adapter interface.
Cross-volume moves are refused (a cross-volume "move" is a copy-plus-delete), and volume identity is
compared by **volume GUID**, not by drive letter, because a directory can be a mount point for another
volume.

---

## 6. Networking

| Property | Value |
|---|---|
| Bind | `100.79.101.19:8791` — the exact Tailscale management address, pinned |
| Wildcard binds | refused (`0.0.0.0`, `::`, `*`) |
| Reserved ports | `8787`, `8788`, `3389` refused by config |
| Firewall rule | `GuvFX-Beta-Agent-In`: inbound, TCP 8791, `LocalAddress` 100.79.101.19, `RemoteAddress` **100.119.23.29 only** (the GuvFX backend), profile = the interface's own profile |
| Public exposure | none — the bind address is CGNAT (100.64.0.0/10), not routable from the internet |

`firewall.ps1` does three things beyond adding the rule, and they matter more than the rule itself:

1. It resolves the firewall profile governing the Tailscale interface and **fails** unless
   `DefaultInboundAction = Block`. A scoped allow is not safe without default-deny inbound.
2. It resolves the **actual listening image** from the installed service (under pywin32 the socket is owned
   by `PythonService.exe`, not `python311.exe`) and enumerates every enabled inbound allow rule, failing if
   any pre-existing rule could *also* authorise :8791 from a non-backend peer. A stray "Allow" that Windows
   offered the first time a Python process listened would otherwise expose the port to every tailnet peer.
3. It refuses the `Public` profile and never uses `-Profile Any`.

It never touches the `:8788` bridge or `:8787` backtest rules.

**Tailscale ACL** — second layer, applied in the Tailscale admin console, not on the host: allow
`100.119.23.29 → 100.79.101.19:8791` and deny every other tailnet peer to that port. This is the layer that
covers what the host firewall cannot: `firewall.ps1` canonicalises program paths but does not resolve 8.3
short names or symlinks, so a program-scoped rule is not a complete defence on its own.

---

## 7. Operational verification

Run immediately after installation, **before any approval to start**. Every step is a read-only
observation. Any failure stops the install and triggers §8 rollback — the service is never started to
"see if it works".

### A. The objects that were created

1. **Identities** — 4 accounts exist; each is in `Users` and **not** in `Administrators` or
   `Remote Desktop Users`; `secedit /export /areas USER_RIGHTS` shows all four holding
   `SeBatchLogonRight` and **none** holding `SeInteractiveLogonRight` or `SeServiceLogonRight`.
2. **Directories** — the §5 tree exists; `icacls` output matches §5 exactly, including inheritance
   disabled at `beta\`; **no component of any slot path is a reparse point**
   (`(Get-Item …).Attributes -band 'ReparsePoint'` is 0 for `beta\`, `beta\slots\`, `beta\slots\<n>\`).
3. **Tasks** — 8 exist; **all disabled**; each has principal `guvfx_b_slot<n>`, logon type 1
   (`TASK_LOGON_PASSWORD`), least-privilege run level, an executable beneath its own slot, and no trigger;
   the launch task carries `/portable`. Record the 8 definition digests (§3) as the approved baseline.
4. **Golden image** — present; its tree digest equals `BETA_AGENT_GOLDEN_DIGEST`; `terminal64.exe` present;
   `.guvfx_golden_manifest` and `.guvfx_portable` present; and **no per-instance state** is present:
   `config\accounts.dat`, `config\servers.dat`, `bases\`, `logs\`, `MQL5\Logs\`, `MQL5\Profiles\`.
5. **Bundle** — file checksums equal `manifest.json` for all 15 modules; manifest version recorded.
6. **Service** — exists; `sc qc` shows `start= demand` and `StartName = NT SERVICE\GuvFXBetaAgent`;
   `sc qfailure` shows **no recovery actions**; `Get-Service` reports **Stopped**.
7. **Firewall** — exactly one new rule, `GuvFX-Beta-Agent-In`: TCP 8791, local `100.79.101.19`, remote
   `100.119.23.29` only, on the interface's own profile; the profile's `DefaultInboundAction` is `Block`;
   **no rule references 8787 or 8788**.
8. **Tailscale ACL** — the console policy permits `100.119.23.29 → 100.79.101.19:8791` and denies every
   other peer to that port.

### B. The estate that must be untouched

These are the checks that prove the install did not disturb live trading. Captured before **and** after,
and **diffed** (§9 items 2, 3, 4, 7).

9. Task XML digests for `GuvFX_Autostart`, `GuvFX_SignalBridge`, `GuvFX_BridgeWatchdog`,
   `GuvFX_LaunchMT5`, `GFX_LaunchIS6` — **byte-identical**.
10. Nuno's `terminal64.exe` — **same PID and same creation FILETIME** as before. The pair is the identity;
    PID alone is not, because PIDs are reused.
11. Ports 8787 and 8788 — still bound, by the **same** processes.
12. Autologon registry values — unchanged. System uptime — unchanged (no reboot occurred).
13. No session was created or destroyed.

### C. Observation capability — the decisive pre-start measurement

14. Run the §10 probe under the intended service account. Record the result **whether it passes or fails**;
    both are results, and a failure here is an architecture finding, not an install failure.

This is the step that most justifies its own place in the checklist. If the agent cannot observe a slot
process, `STOP` and `TOMBSTONE` can never be confirmed — and that must be discovered now, against an empty
slot, rather than later with a live terminal in one and no way to prove it stopped.

---

## 8. Rollback and uninstall

Reverse order of §1. Nothing here deletes runtime or audit data.

1. **Service** — `sc stop` (it should not be running), `python service.py remove`, `sc delete`.
2. **Firewall** — remove `GuvFX-Beta-Agent-In`. `:8787`/`:8788` untouched.
3. **Tasks** — unregister `GuvFXBetaRuntime-*` **and** `GuvFXBetaRuntimeStop-*`. Removing a task removes its
   stored credential. *(F4: today's `uninstall.ps1` removes only the launch prefix.)*
4. **ACLs** — revoke the service-account grants, so no standing principal remains on retained data.
5. **Bundle** — remove `beta\agent\`. **`beta\agent-state\` is retained**: it holds the nonce, idempotency,
   slot-assignment and audit stores. Deleting it destroys the evidence chain.
6. **Identities** — per §2 removal procedure.
7. **Retained deliberately:** everything under `beta\slots\` and `beta\tombstones\`, plus `agent-state\`.

**Untouched throughout:** Nuno's terminal (Session 3), the bridge (:8788), :8787, autologon, startup tasks.

### Recovery from a failed operation

| Failure | On disk | In the store | Recovery |
|---|---|---|---|
| MATERIALISE blocked at a pre-check | nothing — no copy attempted | `stage_copy` BLOCKED | fix the precondition, retry |
| MATERIALISE fails a post-check | populated slot dir **with** its marker | `stage_copy` FAILED | retry TOMBSTONE |
| MATERIALISE interrupted between copy and marker | populated slot dir, **no** marker | `stage_copy` FAILED | **retry TOMBSTONE** — the gate proves identity from the durable store when the marker is absent |
| Launch trigger accepted, no process | unchanged | `request_launch` REQUESTED + `confirm_launch` FAILED | investigate the task; slot stays materialised |
| STOP trigger accepted, process survives | unchanged | `confirm_terminated` FAILED `process_still_running` | operator action — **the agent will never escalate to a kill** |
| TOMBSTONE refused at the pre-move check | **nothing moved** | `precheck_cleanup` BLOCKED | costless; fix and retry |
| TOMBSTONE moved, cleanup then failed | dir under `tombstones\<n>\<occid>\` | `tombstone` COMPLETED + `verify_cleanup` FAILED | retry is safe — the gate accepts a torn-down slot and resumes at cleanup |
| Integrity mismatch at any gate | unchanged | slot **quarantined** | operator-only clearance requiring identity + evidence reference |

---

## 9. Evidence collected during installation

Captured **before** and **after**, and compared. Redacted per the security rule: no passwords, no keyring
material, no token values — digests and prefixes only.

**Before**
1. `sc qc` / `sc qfailure` for `GuvFXBetaAgent` (expected: absent).
2. Task XML digests for all five production tasks (`GuvFX_Autostart`, `GuvFX_SignalBridge`,
   `GuvFX_BridgeWatchdog`, `GuvFX_LaunchMT5`, `GFX_LaunchIS6`).
3. `netstat` bindings for :8787 and :8788 with owning PIDs and creation times.
4. Nuno's `terminal64.exe` PID **and creation FILETIME** — the pair is the identity, since PIDs are reused.
5. `Get-NetFirewallRule` inventory (enabled, inbound, allow).
6. `secedit /export /areas USER_RIGHTS`.
7. System uptime.

**During**
8. Full transcript of each script's **dry-run (PLAN)** output, then its `-Apply` output.
9. `icacls` output for every directory created.
10. Task registration output + the 8 definition digests (§3).
11. Bundle checksums vs `manifest.json`, and the manifest version (`2026-07-22.26`).
12. Golden image tree digest + `.guvfx_golden_manifest` contents.

**After**
13. Repeat 1–7 and **diff**. Items 2, 3, 4, 7 must be byte-identical; 1, 5, 6 differ only by the objects §1
    authorises.
14. `Get-Service GuvFXBetaAgent` → **Stopped**; `Win32_Service.StartName` → `NT SERVICE\GuvFXBetaAgent`.
15. The §6 (verification) checklist result, pass/fail per item.
16. The §10 observation-capability probe result.

Filed as an evidence manifest under `evidence/` per the evidence rule, with an explicit "not covered" list.

---

## 10. Service identity and the observation-capability question

*(This section replaces the LocalSystem request, per correction C-1.)*

### Why LocalSystem is **not** required for the install

The install performs no observation. It creates objects and stops. Nothing in §1 needs to enumerate a
process, open a handle, or read another account's token. The service is installed **stopped**, so the agent
never executes during the install at all.

The intended and already-implemented identity is the virtual service account
**`NT SERVICE\GuvFXBetaAgent`** — no password, stable SID, scoped ACLs on the beta tree only.

### Which observations might not be performable under that identity

These are the operations whose feasibility under a non-admin virtual account is genuinely unknown, and they
matter only from **first start** onward, not at install:

| Operation | API | Concern |
|---|---|---|
| Enumerate process owners | `WTSEnumerateProcessesEx` level 1 | Microsoft's Remarks describe it as requiring Administrators. If it fails, the agent cannot scope process attribution at all |
| Read a slot process's image path | `QueryFullProcessImageNameW` | needs `PROCESS_QUERY_(LIMITED_)INFORMATION` on another local account's process |
| Read its creation FILETIME | `GetProcessTimes` | same handle |
| Read its session | `ProcessIdToSessionId` | documented to need the **full** `PROCESS_QUERY_INFORMATION` |

If enumeration fails, `query_slot_process` raises and every observation degrades to
`process_observation_unavailable` — which, by design, means **no launch can be confirmed and no stop can be
confirmed**. The system fails closed and does nothing. It does not misreport.

### The probe, and the evidence it produces

A bounded, read-only probe run **after install, before first start** (§7 step 14), under the intended service account:

1. `WTSEnumerateProcessesEx` level 1 — does it return, and does it include processes owned by other accounts?
2. `LookupAccountName("guvfx_b_slot1")` — does the SID resolve?
3. `OpenProcess(PROCESS_QUERY_INFORMATION)` against **any** process owned by a non-admin account, then
   `QueryFullProcessImageNameW` + `GetProcessTimes` + `ProcessIdToSessionId` on it.

Evidence recorded: the exact API called, success/failure, and on failure the `GetLastError` value. No
process is started, stopped or signalled. This is item 14 of §7.

**This is why the probe exists at all:** if the agent cannot observe a slot process, that must be discovered
against an *empty* slot — not later, with a live MT5 terminal in one and no way to confirm stopping it.

### What would justify elevation, and what would not

| Probe result | Consequence |
|---|---|
| All three succeed | **No elevation.** Proceed to first start under the virtual account. Expected outcome. |
| Enumeration succeeds, `OpenProcess` fails on another user's process | Try granting the service account a *targeted* right rather than elevating wholesale; if none suffices, this is an **architecture finding** — the least-privilege agent cannot observe its own runtimes — and it goes back to you as a decision, not a workaround |
| Enumeration itself fails | Same: an architecture finding. |

**A temporary elevation, if it is ever proposed, exists only long enough to prove observation capability and
must be accompanied by:** the probe evidence under both identities, a written statement of exactly which
call needed it, and a scheduled revert. *"It works better as LocalSystem"* is not a justification —
convenience is exactly how a least-privilege boundary erodes. Retaining LocalSystem permanently would need
its own architecture decision, because it contradicts §5.4 of the adapter contract.

---

## 11. What must change before the install can be performed

These are **not** requests to implement now — no implementation is authorised. They are the review's finding
that the install cannot proceed as scripted today.

| id | Finding | Remedy |
|---|---|---|
| **F1** | `install_service.ps1` / `uninstall.ps1` ACL `C:\GuvFX\beta\accounts` — the **B2** uuid-directory layout. The pool model needs `slots\<n>` and `golden\` | update the path set; add `golden\` as read+execute for slot identities |
| **F2** | No script creates the 4 slot identities, grants `SeBatchLogonRight`, or registers the 8 tasks | a new `install_pool.ps1`, dry-run by default, in the same style — and it must prompt for passwords interactively, never take them as parameters |
| **F3** | `inspect_task` / `assert_task_matches_approved` have **no lifecycle caller** — the agent would trigger a launch task without asserting it still matches its approved definition | wire the assertion into `start()`. Required before **first start**, not before install |
| **F4** | `uninstall.ps1` unregisters `GuvFXBetaRuntime-*` but not `GuvFXBetaRuntimeStop-*`, and removes no slot identities | extend the teardown |
| **F5** | Install must set `BETA_AGENT_DRAIN_TIMEOUT_S=45`; at the 20 s default the pool model refuses to start | document in the install parameters; the config check enforces it |

**Recommended sequencing:** approve this review's *content*, authorise the scripted work in F1–F5 as a
bounded increment, re-review the scripts (short, script-only), then authorise the install itself.

---

## 12. Amber items — accepted, documented, not solved during the install

### Amber 1 — `open_handles()` has no supported Windows implementation

Every documented route is disqualified: Restart Manager rejects directories outright (`ERROR_ACCESS_DENIED`
at `RmGetList`) *and* cannot act on another session from a LocalSystem service;
`NtQuerySystemInformation` is documented as internal and subject to change; `openfiles` requires a reboot.

**Behaviour: fail closed.** `open_handles()` raises, so the `no_runtime_handles` proof never holds and
`precheck_cleanup` **blocks before the move**. Nothing is relocated; the refusal is costless and reversible.
No release. No silent assumption.

### Amber 2 — `release()` implemented, not wired

`no_mutation_lock_held` is one of the seven release proofs, and `tombstone()` runs *inside* that lock — a
release issued from there could satisfy the proof only by lying. `release()` exists and is tested as a
separate step; wiring it to a lifecycle operation changes the protocol surface.

**Behaviour: the slot remains allocated.** TOMBSTONE returns `released: false, release_pending: true`, and
those fields are allowlisted so the backend sees the qualification rather than an unqualified success. The
pool would exhaust after `pool_size` tombstones.

**Neither blocks the viability trial.** MATERIALISE → START → VERIFY → STOP still runs end to end, and that
sequence is what answers the trial's actual question.

---

## 13. Install success criteria

The install is successful **only** if all of the following hold:

- [ ] Service installed, `start=demand`, recovery disabled, identity `NT SERVICE\GuvFXBetaAgent`
- [ ] **Service remains STOPPED**
- [ ] 4 identities created, non-admin, `SeBatchLogonRight` granted and verified
- [ ] 8 scheduled tasks registered and **disabled**, definition digests recorded
- [ ] Firewall rule added; profile default-deny inbound asserted; no pre-existing rule exposes :8791
- [ ] ACLs applied and verified by `icacls` on every directory
- [ ] Bundle checksums match `manifest.json` (15 modules, `2026-07-22.26`)
- [ ] Golden image staged; tree digest recorded and matching `BETA_AGENT_GOLDEN_DIGEST`
- [ ] Validation (§7) passes every item
- [ ] Before/after evidence diff shows the estate untouched (§9 items 2, 3, 4, 7 byte-identical)
- [ ] Observation-capability probe (§10) recorded — pass or fail, both are results

And **none** of the following occurred:

- [ ] No MT5 runtime launched
- [ ] No task triggered
- [ ] No process created under any slot identity
- [ ] No user interaction with MT5
- [ ] No reboot; no autologon change; no session change; no change to :8787/:8788

---

## 14. What this install still cannot tell us

The install proves the objects exist and are shaped correctly. It proves nothing about behaviour. The twelve
questions in `docs/B3P2_WINDOWS_RESEARCH_FINDINGS.md` §4 remain open; the first three decide whether the
execution model works at all:

1. Can a GUI MetaTrader 5 run correctly under a `TASK_LOGON_PASSWORD` task with no interactive session?
2. Is `SeBatchLogonRight` granted automatically on registration, or only by the explicit grant in §2?
3. Which session does the launched process land in, and does MT5 function there?

---

## 15. Approval requested

Approval of this review's **content**, and a decision on the §11 sequencing.

**Not requested, and not to be inferred:** performing the install, creating any user, registering any task,
starting the service, triggering any task, launching MT5, or contacting the Windows host.

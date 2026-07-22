# CVM-Inc-3 B3P-2 — INSTALL-ONLY REVIEW

**Nothing in this document has been executed.** No Windows host has been contacted at any point in B3P-2.
This is the review to be approved *before* the first host modification, and it describes exactly what that
modification would be.

**What "install-only" means here:** create the OS objects, install the service **stopped**, verify them, and
stop. No service start, no task trigger, no MT5 launch, no runtime materialisation. The first manual start
is a separate approval.

---

## 1. Scope of the proposed host change

| # | Object | Action | Reversible by |
|---|---|---|---|
| 1 | 4 local accounts `guvfx_b_slot1..4` | create, non-admin, password never expires | §7 step 1 |
| 2 | `SeBatchLogonRight` for those 4 accounts | grant | §7 step 2 |
| 3 | `C:\GuvFX\beta\` tree (`slots\1..4\`, `tombstones\1..4\`, `golden\`, `agent\`, `agent-state\`) | create | §7 step 3 |
| 4 | ACLs on those directories | set | §7 step 3 |
| 5 | 8 scheduled tasks `GuvFXBetaRuntime-{1..4}` / `GuvFXBetaRuntimeStop-{1..4}` | register, **disabled** | §7 step 4 |
| 6 | Golden MT5 image in `C:\GuvFX\beta\golden\` | copy in, digest recorded | §7 step 3 |
| 7 | Agent bundle in `C:\GuvFX\beta\agent\` | copy in | §7 step 5 |
| 8 | Service `GuvFXBetaAgent` | install, **start=demand**, **not started** | §7 step 5 |
| 9 | Firewall rule (inbound, TCP 8791, Tailscale interface, scoped to the service program) | create | §7 step 6 |

**Nothing else.** In particular: no change to autologon, no reboot, no touching Session 1/3, no change to
`GuvFX_Autostart`, `GuvFX_SignalBridge`, `GuvFX_BridgeWatchdog`, `GuvFX_LaunchMT5`, `GFX_LaunchIS6`, no
change to ports 8787/8788, no modification of `C:\GuvFX\accounts`, `C:\GuvFX\terminals` or the operator's
MT5 installation, and no stop/restart of Nuno's terminal.

---

## 2. Service

| Property | Value | Why |
|---|---|---|
| Name | `GuvFXBetaAgent` | distinct from every existing `GuvFX_*` task |
| Start type | **Demand** at install; Automatic only after the first approved manual start | an install must not become a start by reboot |
| Account | `LocalSystem` **for the trial**, pending trial item 4 | it must read another local account's process image path, creation time, SID and session; whether a lesser account can is the measurement |
| Bind | `100.79.101.19:8791`, pinned to that exact address | `assert_exact_bind`; a loopback or alternate-NIC bind would side-step the interface-scoped firewall rule |
| Integrity | `build_agent(enforce_integrity=True)` hashes all 17 bundle modules against `manifest.json` at start | a drifted bundle refuses to start |
| Recovery | none configured at install | a crash-loop must be visible, not auto-restarted |

**LocalSystem is a deliberate, temporary concession and the single biggest open question in this review.**
The architecture's intent is a least-privilege agent. Section 5.4 of the adapter contract states that every
adapter method must work without administrative rights *or the design is wrong*. Running the trial as
LocalSystem measures MT5 behaviour first and privilege second; if the trial shows a non-admin agent cannot
observe slot processes, that is an architecture finding requiring a decision, not something to paper over
by leaving the agent privileged.

**Error 1053 exposure.** pywin32's `SvcRun()` reports `SERVICE_RUNNING` *before* calling `SvcDoRun()`, so a
slow body cannot cause a start timeout; the exposure is module import and `__init__`, which run inside
`SERVICE_START_PENDING`. The bundle imports only the standard library at module scope.

---

## 3. Identities

Four accounts, one per slot, created by the operator — **the agent creates no OS object and holds no
runtime password.**

```
guvfx_b_slot1 .. guvfx_b_slot4
```

- Non-admin. Member of `Users` only. **Not** `Administrators`, **not** `Remote Desktop Users`.
- Password: generated in the operator's own session, never pasted into chat, never committed, never in a
  command line that reaches a log. Stored only in Task Scheduler's credential store via
  `TASK_LOGON_PASSWORD` at task registration.
- `SeBatchLogonRight` ("Log on as a batch job") granted explicitly. It is a **logon right, not a
  privilege** — it will not appear in `whoami /priv`; verify with `secedit /export /areas USER_RIGHTS` or
  `LsaEnumerateAccountRights`. Whether registration grants it automatically is **trial item 2**; granting
  it explicitly makes the question moot for the install.
- Deliberately **not** granted `SeInteractiveLogonRight` or `SeServiceLogonRight`.
- No relationship to `guvfx_u_1`, `guvfx_u_6`, `guvfx_u_7` (the existing estate identities) or to
  `guvfx-rdp`. The beta identities are a separate namespace; `FORBIDDEN_IDENTITIES` refuses
  `administrator`, `system` and `guvfx-rdp` as a task principal, compared on the account component so
  `.\Administrator` and `Administrator@host` are both caught.

---

## 4. Scheduled tasks

Eight tasks, registered by the operator, **disabled at install**. The agent may only trigger them.

| Property | Launch task | Terminate task |
|---|---|---|
| Name | `GuvFXBetaRuntime-<n>` | `GuvFXBetaRuntimeStop-<n>` |
| Principal | `guvfx_b_slot<n>` | `guvfx_b_slot<n>` |
| Logon type | `TASK_LOGON_PASSWORD` (1) | `TASK_LOGON_PASSWORD` (1) |
| Run level | Least privilege | Least privilege |
| Action | `C:\GuvFX\beta\slots\<n>\terminal\terminal64.exe` | `taskkill` scoped to the slot's own process |
| Arguments | `/portable` | — |
| Working dir | `C:\GuvFX\beta\slots\<n>\terminal` | — |
| Triggers | **none** (on-demand only) | none |
| `AllowStartOnDemand` | true | true |
| `MultipleInstances` | `IgnoreNew` (2) | `IgnoreNew` (2) |
| Enabled at install | **false** | **false** |

Notes that come directly from the research:

- `S4U` was rejected: it stores no password but also grants "no access to either the network or to
  encrypted files", which an MT5 terminal needs.
- `/portable` in the launch arguments is the **authoritative** portable-mode signal. MetaQuotes documents no
  on-disk marker; portable mode is a per-launch command-line property. `inspect_task` records
  `portable_switch` from the task definition, and the in-tree `.guvfx_portable` file is an explicit GuvFX
  artefact placed in the golden image, not an MT5 one.
- `IgnoreNew` prevents a duplicate demand start from producing a second terminal. This is defence in depth:
  `start()` already refuses to trigger unless the slot is observed ABSENT.
- The task names are validated by `assert_authorised_slot_input`, which requires the
  `guvfxbetaruntime` prefix and refuses every production task name.

---

## 5. ACLs

| Path | Owner | Access |
|---|---|---|
| `C:\GuvFX\beta\` | Administrators | Administrators FullControl, SYSTEM FullControl, inheritance **disabled** |
| `C:\GuvFX\beta\golden\` | Administrators | Administrators FullControl; each `guvfx_b_slot<n>` **Read+Execute only** |
| `C:\GuvFX\beta\slots\<n>\` | Administrators | Administrators FullControl, SYSTEM FullControl, `guvfx_b_slot<n>` Modify. **No other slot identity has any access.** |
| `C:\GuvFX\beta\tombstones\<n>\` | Administrators | Administrators + SYSTEM only |
| `C:\GuvFX\beta\agent\` | Administrators | Administrators FullControl, SYSTEM Read+Execute. The service account cannot write its own code. |
| `C:\GuvFX\beta\agent-state\` | Administrators | Administrators + SYSTEM FullControl |

The golden image being read-only to the slot identities matters: if a runtime could write it, one
compromised slot would compromise every future slot.

**The agent has no `set_acl` method.** That absence is the enforcement — ACLs are established once, by a
human, and the agent can only *read* them (`read_acl`, evidence only).

`C:\GuvFX\beta\slots\<n>\` must **exist before** first use: `stage_copy` now refuses when
`real_path(slot_dir)` is `None`, because robocopy would otherwise create the whole chain and materialise
into a slot nobody provisioned — no identity, no ACL, no tasks.

---

## 6. Operational verification (install-only, read-only)

Run after install, before any approval to start. Every step is an observation.

1. **Accounts** — 4 exist, non-admin, `Users` only; `secedit /export /areas USER_RIGHTS` shows all four
   holding `SeBatchLogonRight`; none holds `SeInteractiveLogonRight`.
2. **Directories** — the tree exists; `icacls` output matches §5; no slot directory is a reparse point.
3. **Tasks** — 8 exist, all **disabled**, each with the expected principal, logon type 1, action path
   beneath its own slot, `/portable` present on the launch task, no triggers.
4. **Golden image** — present; its tree digest equals the value that will be set as
   `BETA_AGENT_GOLDEN_DIGEST`; `terminal64.exe` present; no per-instance state
   (`config\accounts.dat`, `config\servers.dat`, `bases\`, `logs\`, `MQL5\Logs\`, `MQL5\Profiles\`).
5. **Bundle** — file checksums equal `manifest.json`; `manifest_version` recorded.
6. **Service** — exists, start type Demand, **not running**, correct account, correct `ImagePath`.
7. **Firewall** — one inbound rule, TCP 8791, scoped to the Tailscale interface and the service program;
   **no rule touches 8787 or 8788**.
8. **Estate untouched** — `GuvFX_Autostart`, `GuvFX_SignalBridge`, `GuvFX_BridgeWatchdog`,
   `GuvFX_LaunchMT5`, `GFX_LaunchIS6` unchanged (compare task XML digests before/after);
   Nuno's MT5 process still running with the same PID and creation time as before the install; ports
   8787/8788 still bound by the same processes; autologon registry values unchanged.
9. **No side effects** — no reboot occurred (uptime unchanged); no session was created or destroyed.

Steps 8 and 9 are the ones that prove the install did not disturb live trading. They should be captured
before **and** after, and compared.

---

## 7. Rollback and uninstall

Reverse order of §1. Each step is independently reversible; nothing here deletes user data.

1. **Firewall** — remove the single named rule.
2. **Service** — `sc stop` (it should not be running), `sc delete GuvFXBetaAgent`.
3. **Bundle** — remove `C:\GuvFX\beta\agent\`. `agent-state\` is **retained** (it holds the durable nonce,
   idempotency, slot and audit stores; deleting it destroys the evidence chain).
4. **Tasks** — unregister the 8 `GuvFXBetaRuntime*` tasks. Removing a task also removes its stored
   credential.
5. **Directories** — `C:\GuvFX\beta\slots\<n>\` contents are **moved to tombstones**, never deleted. The
   tree itself is retained for inspection.
6. **Rights** — revoke `SeBatchLogonRight` from the four accounts.
7. **Accounts** — disable first, delete only after the evidence has been captured. Deleting an account
   orphans any file it owns.

**Rollback after a failed operation** (what the operator finds, and what to do):

| Failure | Left on disk | Left in store | Recovery |
|---|---|---|---|
| MATERIALISE blocked at a pre-check | nothing (no copy attempted) | one `stage_copy` BLOCKED stage record | retry after fixing the precondition |
| MATERIALISE fails at a post-check | a populated slot directory **with** its ownership marker | one `stage_copy` FAILED record | operator tombstones the slot directory manually, then retry |
| Launch trigger accepted, no process | nothing changed | `request_launch` REQUESTED + `confirm_launch` FAILED | investigate the task; the slot stays materialised |
| STOP trigger accepted, process survives | nothing changed | `confirm_terminated` FAILED `process_still_running` | operator intervention; **the agent will not escalate to a kill** |
| TOMBSTONE moves, cleanup fails | slot directory now under `tombstones\<n>\<occupancy_id>\` | `tombstone` COMPLETED + `verify_cleanup` FAILED | **retry is safe** — the gate accepts a torn-down slot and resumes at cleanup |
| Integrity mismatch at any gate | unchanged | slot **quarantined** | operator-only clearance, requiring identity + evidence reference |

---

## 8. Known blockers to a *complete* lifecycle — stated before, not after

Two things will stop the trial short of a full teardown. Both are deliberate and both need a decision that
is not mine:

1. **`open_handles()` raises unconditionally.** No supported Windows API can prove no process holds a handle
   beneath a directory. The `no_runtime_handles` cleanup proof therefore never holds, `verify_cleanup`
   fails, and slot release is blocked. Options are in `docs/B3P2_WINDOWS_RESEARCH_FINDINGS.md` §5.
2. **`release()` is implemented and tested but not wired.** `no_mutation_lock_held` is one of the seven
   release proofs and `tombstone()` runs inside that lock, so releasing from there could only satisfy the
   proof by lying. Wiring release to a lifecycle operation changes the protocol surface — an **Amber**
   decision. Until then TOMBSTONE reports `released: false, release_pending: true`, and the pool would
   exhaust after `pool_size` tombstones.

A trial that goes MATERIALISE → START → VERIFY → STOP → TOMBSTONE and stops before release is still the
decisive test: it answers whether MT5 runs at all under this execution model.

---

## 9. What this install still cannot tell us

The install-only gate proves the objects exist and are shaped correctly. It proves nothing about behaviour.
The twelve questions in `docs/B3P2_WINDOWS_RESEARCH_FINDINGS.md` §4 remain open, and the first three decide
whether the execution model works at all:

1. Can a GUI MetaTrader 5 run correctly under a `TASK_LOGON_PASSWORD` task with no interactive session?
2. Is `SeBatchLogonRight` granted automatically on registration, or only by the explicit grant in §3?
3. Which session does the launched process land in, and does MT5 function there?

---

## 10. Approval requested

Approval to perform **§1 only**, verified by **§6**, reversible by **§7**, with the service left
**installed and stopped**.

Not requested here, and not to be inferred: starting the service, enabling any task, triggering any task,
launching MT5, or materialising any runtime.

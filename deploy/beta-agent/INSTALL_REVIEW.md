# Beta Provisioning Agent — Pre-Installation Review (CVM-Inc-3 B3)

**Status: REVIEW ONLY — do NOT install yet. Adversarial verification found blocking gaps: install must
NOT proceed until the pre-start deliverables in §0 land as reviewed code AND Nuno resolves the runtime-identity
decision (§17).** This document is produced *before* any B3 installation. The first installation (§16)
**installs only**, does not auto-start, verifies binary/checksum/permissions/firewall/service-config, then
**waits for approval** before the first manual start.

## Host facts (verified read-only, 2026-07-21)
| | |
|---|---|
| Host | `WIN-RD8VDS93DK7` @ `100.79.101.19` (Tailscale), Windows Server 2025 Datacenter |
| CPU / RAM | 8 logical CPU / 32,758 MB total, **~10.4 GB free now** |
| Disk `C:` | **458 GB free** |
| Python | system **3.11.9** (+ 3.13.14); bundled `C:\GuvFX\python311.exe`. Agent is **stdlib-only** (`http.server`, `sqlite3`) — no pip deps. A **service harness** (pywin32 or nssm) is an added dependency — see §0/§6 |
| Must NOT touch | bridge **`:8788`** (PID 1856, Nuno's), backtest agent `:8787`, RDP `3389`, autologon **Administrator / Session 1** + `GuvFX_Autostart`/`GuvFX_SignalBridge` tasks, **Nuno's terminal (Session 3)**, `guvfx_u_{1,6,7}` + `C:\GuvFX\accounts\{1,6,7}`, golden `C:\GuvFX\golden\mt5` |
| Agent port `:8791` | **free** (no collision with 8787/8788/3389) |
| Service `GuvFXBetaAgent` | **does not exist** (no name collision) |
| Local accounts | `guvfx_u_{1,6,7}` (non-admin runtime identities), `guvfx-rdp` (admin), `WDAGUtilityAccount`. **No dedicated agent service account exists yet.** |
| `C:\GuvFX\beta` | exists (parent of `beta\accounts` + `beta\tombstones`) |
| Only client | the GuvFX backend / control plane at `100.119.23.29` (Tailscale) |
| **Pre-existing firewall rules** | **NOT yet enumerated.** The bridge listens on `0.0.0.0:8788` and is reachable on the tailnet, and `:8787` listens — so ≥1 enabled inbound allow rule exists. Whether any is program-scoped to `python.exe` (which would also authorise the agent's interpreter on `:8791` from *every* tailnet peer) is **unverified** and is a §0 pre-start check |

---

## 0. Verification outcome — blocking pre-start deliverables

A 5-lens adversarial verification (least-privilege, network-isolation, estate-non-interference,
rollback/uninstall-safety, install-only-gate) produced **9 MUST_FIX** and 22 SHOULD_FIX/NIT findings.
The MUST_FIX items are **design/code gaps, not documentation nits** — the shipped B2 artefact cannot
perform what §1–§16 assert. The install-only gate **cannot grant start-approval while these are open.**
Each is a reviewed-code deliverable (implement → tests → adversarial review → CI green → merge) *before*
first start; none may be closed by granting the service admin / `SeDebugPrivilege`.

| # | Blocking item | Why it blocks | Lands in |
|---|---|---|---|
| B-1 | **Cross-identity terminate is impossible.** A non-admin service cannot get `PROCESS_TERMINATE` on a process owned by a *different* identity (`guvfx_u_beta`) without `SeDebugPrivilege`/admin — both denied. `op_impls.stop()` calls a bare `stop_pid()` across identities. | STOP/TOMBSTONE as drawn cannot run under least-privilege. | §2, §13; `op_impls.stop`, `win_ops.stop_pid` |
| B-2 | **Runtime OS identity / cross-tenant isolation.** A single shared `guvfx_u_beta` for all beta runtimes lets any beta tenant `OpenProcess`/read-memory/read-files of every other — a regression vs the estate's per-account `guvfx_u_<id>`. | Multi-tenant beta host loses isolation. | §2, §3, §13; **Nuno decision §17** |
| B-3 | **win_ops box primitives are stubs.** `find_runtime_process`, `stop_pid`, `launch_runtime`, `copy_golden` all `raise …not_available_off_box`. Every non-interference guarantee rests on unwritten code. | The image-beneath-canonical safety can't be trusted until the primitive that reports the image path exists + is proven. | §6, §13, §16; `win_ops.py` |
| B-4 | **Session-0→interactive launch surface is undesigned in code.** `launch_runtime` is a stub; no reviewed artefact shows how a *fixed* task launches a per-UUID terminal without the service supplying an arbitrary target. This is the one privileged bridge into an interactive desktop. | An unspecified/steerable launch target could be pointed outside the beta tree or into Nuno's session. | §2, §6, §16 |
| B-5 | **No Windows service harness.** `agent.py` is a bare `serve_forever()` script — no SCM control handler. Raw `sc create` binPath → SCM start fails **error 1053**. `sc qc` verifies fields, not that the binary is a valid service. | "Service configuration verified" is hollow; first start would fail or be unmanaged. | §6, §15, §16; new hashed wrapper |
| B-6 | **Update/stop drain does not exist.** The idempotency record is written *after* the op returns, so "store shows no in-flight op" is vacuously true even mid-`MATERIALISE`/`TOMBSTONE`. No SCM stop handler. | `sc stop` during an update can kill a mutating op mid-flight. | §8; durable op-marker + stop handler |
| B-7 | **Integrity manifest covers 4 of 8 modules.** `IMPL_MODULES` omits `agent.py`, `config.py` (the bind-guard), `stores.py`, `manifest.py`. A tampered/stale `config.py` binding `0.0.0.0`, or a stale `agent.py` adding a route, **passes** `validate.py`. `__pycache__/*.pyc` unhashed. Manifest is co-located + unsigned. | The checksum sign-off does not cover the files that decide public exposure. | §7-manifest, §8, §16.7; `manifest.py` |
| B-8 | **Firewall never verified vs pre-existing rules.** The new rule only *adds* an allow scoped to the backend; a pre-existing program-scoped `python.exe`/`Any` allow (very common on this box) would authorise `:8791` from every tailnet peer, bypassing it. No `DefaultInboundAction=Block` assertion; no real reachability probe. | Network isolation may be an assertion, not a fact. | §5, §16.7 |
| B-9 | **Live bind not pinned to the single interface.** `config._is_private_mgmt_address` accepts *any* loopback/RFC-1918/CGNAT address (`validate.py` allows `127.0.0.1`, `10.0.0.5`). Policy says bind **only** `100.79.101.19`. A loopback/alt-NIC bind passes the guard and side-steps the interface-scoped firewall. | Reach to `/provision` is wider than the stated single management address. | §4, §16.7; `config.py` live path |

**SHOULD_FIX / NIT** (fold into the same B3-prep increment; detailed in the sections): pre-auth
resource-exhaustion cap (§12), per-`<uuid>` ACL + no golden-DACL write (§3), Tailscale ACL as a second
isolation layer (§4), atomic all-or-nothing update + `state.sqlite`/logs moved out of the copy target (§8/§11),
uninstall completeness — drain running runtimes, remove task/account/right/ACL (§9), virtual-account stable
SID + unreadable-owner-tag-is-fatal (§10), `sc qfailure` recovery check (§7), firewall introspection commands
(§16.7), interpreter-path + account-form defaults in `install_service.ps1` (§6/§16), secret-store vs machine
env-var reconciliation (§1/§11), firewall `-Profile` scope (§5), outbound + bind-port guard (§4).

---

## 1. Windows service account
A **new dedicated least-privilege identity**, created at install:
- **Preferred: the Windows virtual service account `NT SERVICE\GuvFXBetaAgent`** — auto-managed, **no
  password to store**, and a **name-derived stable SID that survives service delete+recreate** (this closes
  the rollback→reinstall orphaned-SID hazard: retained runtime-dir ACLs and `.guvfx_owner` tags still resolve
  after a reinstall). Member of no privileged group.
- **Fallback:** a dedicated local `svc_beta_agent`, non-admin, long random password held **only** in the
  Windows secret store. If used, reinstall must re-apply ACLs to retained runtime dirs and treat an unreadable
  owner tag as a **hard error, never "free"**.

The service runs **non-interactively (Session 0)** and never holds interactive-desktop rights.

**Corrected — cross-identity limit (B-1).** Whatever account the service runs under, it **cannot** terminate
or launch a process owned by a *different* identity without admin/`SeDebugPrivilege`. So the interactive MT5
**launch** and the **terminate** cannot be direct service actions — both are delegated to a mechanism running
*as the runtime identity* (§2/§6/§13). The virtual account is preferred for the service's own lifecycle; it
does **not** by itself enable cross-identity process control (and must not — that is the point).

**Secret store (SHOULD_FIX).** `config.load_config` currently reads `BETA_AGENT_KEYRING` from a machine env
var — that is **not** a protected store (any admin/any process can read it). Before first start, either
implement real retrieval (DPAPI / Credential Manager / LSA secret) or drop the "Windows secret store"
wording and document the env-var exposure, scope it to the service account, and add key rotation. The keyring
is **not** provisioned until the post-approval start, to minimise the exposure window.

## 2. Required Windows privileges
- **`SeServiceLogonRight`** ("Log on as a service") — the only right the service account needs.
- **Explicitly NOT granted:** `SeDebugPrivilege`, `SeTcbPrivilege`, `SeAssignPrimaryTokenPrivilege`,
  `SeImpersonate*`, local Administrator, RDP logon.
- **START (B-4).** A Session-0 service cannot create an interactive terminal. START triggers **one fixed,
  pre-registered, on-demand-only launch task** whose `RunAs` is the **runtime identity** and whose action is
  **created once at install by the human admin** — never registered dynamically by the service. The task's
  action derives its target **solely** from the agent-owned canonical layout (`…\beta\accounts\<uuid>\terminal`)
  and must be immutable to both the service identity and anything writable from the beta tree. The service is
  granted **Execute only** on that task. In the broker-independent proof scope there is **no broker login**, so
  the launched runtime is headless and started **without broker credentials** (nothing to hold or leak).
  *This mechanism must exist as reviewed code before first start (B-3/B-4); today `launch_runtime` is a stub.*
- **STOP/TOMBSTONE (B-1).** The image-beneath-canonical **decision** stays in `op_impls`, but the privileged
  **terminate** is **delegated to a same-identity (runtime-identity) mechanism** that receives only the
  agent-vetted PID + canonical path — the service never calls `PROCESS_TERMINATE` across an identity boundary.
  `op_impls.stop`/`tombstone` and `win_ops.stop_pid` must be made consistent with this (they currently imply a
  direct cross-identity kill). **This must not be "fixed" by granting the service `SeDebugPrivilege` or admin.**

## 3. NTFS permissions
Least-privilege, scoped strictly to the beta tree:
- `C:\GuvFX\beta\accounts\` — **service account: Modify** (create/read/write/move within); **no** rights above `…\beta\`.
- `C:\GuvFX\beta\tombstones\` — service account: **Modify** (TOMBSTONE move-in target).
- `C:\GuvFX\beta\agent\` (code) and `C:\GuvFX\beta\agent-state\` (state DB + logs, §11) — service account:
  **Modify**; Administrators: Full; **no** other principal.
- **Runtime-identity grant (B-2, corrected).** The runtime identity gets Read&Execute **only inside its own
  `…\beta\accounts\<uuid>\` folder, applied at MATERIALISE — never at the `accounts\` root.** Under the
  per-runtime-identity model each `<uuid>` is granted to *its own* identity, so no beta tenant can read/modify
  another's files. (Granting a shared identity at the tree root is the regression B-2 forbids.)
- **Golden — no DACL write (SHOULD_FIX).** Do **not** grant any ACE on `C:\GuvFX\golden\mt5` (granting an ACE
  is itself a write to a must-not-touch object). Instead copy the checksummed golden image **once** into a
  beta-owned staging dir (`…\beta\golden-stage\`, owned by the service account) and MATERIALISE from there, so
  the service needs **no** access to Nuno's golden tree. If a direct read grant is ever retained, use a minimal
  non-destructive ACE (never `/reset` or `/inheritance:r`) and snapshot golden's DACL before/after.
- **Explicit no-grant** on `C:\GuvFX\accounts\`, `C:\GuvFX\terminals\`, the bridge scripts,
  `C:\Program Files\MetaTrader*`, and everything outside `…\beta\`.

## 4. Network permissions
- The agent **binds only** to `100.119.23.29`'s target — the private Tailscale address `100.79.101.19:8791`.
  **Live-bind pin (B-9):** the running service must assert the resolved bind host **equals `100.79.101.19`
  exactly**, not merely "some private address." `config._is_private_mgmt_address`'s broad accept (loopback /
  RFC-1918 / CGNAT) is retained **only** for offline `validate.py`; the live path uses the exact-address
  allowlist compare. §16.7 asserts the resolved `BETA_AGENT_BIND_HOST == 100.79.101.19` at install.
- **Outbound:** none required; the agent is a pure responder and never touches `:8788`/`:8787`. Optional
  defense-in-depth: an outbound block for the service identity. Reject `BETA_AGENT_BIND_PORT ∈ {8787,8788,3389}`
  at config load so the port can never be pointed at Nuno's services by fat-finger.
- **Second isolation layer (SHOULD_FIX).** `assert_private_bind` proves the bind is private but does **not**
  pin the socket to the management peer — Tailscale is a flat mesh, so `:8791` is reachable at L3 by every
  tailnet node, with **only** the one Windows firewall rule constraining it. Add a **Tailscale ACL**: permit
  `dst 100.79.101.19:8791` only from the backend node, deny all other src — so isolation is
  Tailscale-ACL **AND** host-firewall, not a single point of failure. Record in the runbook that
  disabling/resetting the Windows Firewall on this host is a **Red action** (it de-isolates the agent on the
  box that also runs Nuno's live Session-3 terminal + `:8788` bridge).

## 5. Firewall rules
One **inbound allow**, default-deny otherwise — **scoped to the Tailscale interface's profile, not `Any`:**
- `New-NetFirewallRule -DisplayName "GuvFX-Beta-Agent-In" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8791 -LocalAddress 100.79.101.19 -RemoteAddress 100.119.23.29 -Profile <Tailscale-NIC profile, typically Private/Domain>` (not `-Profile Any`, which would also open the Public profile — contradicting the policy).
- The bridge's `:8788` / agent's `:8787` rules are **left exactly as-is**.
- **Pre-existing-rule check (B-8, blocking).** Before install, enumerate every enabled inbound allow rule and
  its filters — `Get-NetFirewallRule -Enabled True -Direction Inbound -Action Allow` piped to
  `Get-NetFirewallApplicationFilter` / `Get-NetFirewallPortFilter` / `Get-NetFirewallAddressFilter` — and confirm
  **none** match: (a) `Program` = the agent's `python.exe` or `Any`, (b) `LocalPort` 8791 or a covering range,
  from any `RemoteAddress` other than `100.119.23.29`. Assert the profile on the Tailscale interface has
  **`DefaultInboundAction = Block`**. If a broad `python.exe`/`Any` program rule exists, add a **higher-priority
  Block** for `:8791` from any source ≠ backend (or narrow the existing rule) **before** the service is ever
  started.
- **Reachability probe = post-approval, post-first-start** (not install-only — the service is stopped, so there
  is no listener to probe): from a **second** tailnet peer, TCP-connect `100.79.101.19:8791` → must be
  refused/time out; from the backend → success only after start.

## 6. Startup mechanism
A **Windows Service** (`GuvFXBetaAgent`), **`Start = demand` (manual)** for the first install — **not** `auto`.
- **Service harness (B-5, blocking).** `agent.py` today is a bare `serve_forever()` with no SCM control
  handler, so a raw `sc create` binPath pointing at `python agent.py` fails SCM start (**error 1053**). Before
  first start it must be wrapped in a **real Windows service** — either pywin32 `win32serviceutil.ServiceFramework`
  or a reviewed `nssm.exe` — **checked into the hashed artefacts**, with `§16.4` binPath pointing at the wrapper.
  Add an **offline unit test** of the ServiceFramework class (no listener bound).
- On start it loads config from the secret store, asserts the **exact-address** private bind (§4), verifies
  **full-bundle** integrity (§7-manifest / B-7; refuses to start on drift), and serves `POST /provision` only.
- **Drain-aware stop (B-6).** The SCM stop handler must **refuse/delay** stop while any per-runtime mutation
  lock / durable op-marker is held, up to a timeout — so `sc stop` during an update cannot kill a mutating op
  mid-flight.
- It does **not** modify autologon, register in `GuvFX_Autostart`, or create a boot dependency on the bridge.
- **Interactive MT5 launch** is the fixed pre-registered task (§2), **registered at the post-approval
  start-enablement phase, not during install-only** (an interactive-execution primitive is more surface than a
  stopped-service install needs, and the stopped service can't trigger it anyway).

## 7. Service recovery policy
First install: recovery **disabled** (manual start, no auto-restart) so nothing brings it up without approval.
**Verify with `sc qfailure` (SHOULD_FIX)** — `sc qc` does **not** display failure/recovery actions, so the
"recovery disabled" property must be checked explicitly and assert no restart/run-program/reboot actions. After
the proof + approval: **first failure → restart after 60 s; second → 120 s; subsequent → no action**; never
"restart the computer", never "run a program". A crash never affects the bridge, terminal, or a running runtime
(separate processes/tasks).

**Full-bundle integrity manifest (B-7).** `manifest.IMPL_MODULES` must be expanded to **every executable
module** — add `agent.py`, `config.py`, `stores.py`, `manifest.py` — regenerate `manifest.json`, and add an
**authenticity** step that hashes the deployed tree against known-good commit (per-file hashes / `git archive`),
not only the co-located unsigned manifest. Copy only `.py` + `manifest.json` (exclude `__pycache__`) and run
the interpreter with `-B` so bytecode can't diverge from hashed source.

## 8. Update procedure
1. Land the change in Git → tests → adversarial review → CI green → merge (governance).
2. Regenerate `manifest.json` (full-bundle, §7) in the merged commit.
3. **Drain (B-6):** signal stop; the SCM handler blocks until no durable op-marker is held (a marker is written
   *before* every mutating op and cleared on completion — the current "store shows none" check is vacuous).
4. **Atomic swap, not copy-over-live (SHOULD_FIX):** copy the merged `deploy/beta-agent/` into a **fresh temp
   dir**, run `validate.py` **there**, then atomically rename/swap into place — never a non-atomic in-place copy
   (a partial copy could run new op logic against a stale handler/store/bind-guard).
5. **Preserve state (SHOULD_FIX):** `state.sqlite` + logs live under `…\beta\agent-state\` (§11), **outside**
   the code-copy target, so a mirror/`/MIR` copy can never wipe burned nonces (replay protection), idempotency
   records, or audit. Document the exact non-mirroring copy command.
6. Start; `NEGOTIATE` reports the new agent/protocol/manifest versions; the backend's `assert_compatible` gates.

The agent **never self-updates** through its API. Rollback = re-copy the previous merged commit (tagged snapshot).

## 9. Uninstall procedure
`uninstall.ps1` (must be brought in line with this — it currently only stops+deletes the service and removes the
firewall rule):
1. **Drain running runtimes first (SHOULD_FIX):** STOP (not delete) every provisioned beta runtime **via the
   agent** — enumerate them from the durable store — so uninstall never strands live terminals with no remaining
   STOP/TOMBSTONE path.
2. `sc stop` + `sc delete GuvFXBetaAgent`.
3. Remove the firewall rule `GuvFX-Beta-Agent-In` and the **launch task(s)** (tracked by a fixed name prefix →
   `Unregister-ScheduledTask`).
4. Revoke `SeServiceLogonRight`, remove the beta-tree ACL grants, and **remove/disable the service account and
   the beta runtime identity/identities** — so no standing principal keeps access to retained beta data.
5. **Runtime + tombstone directories are RETAINED** (never deleted) for audit.

Nuno's terminal, bridge, Session 3, `:8788`, autologon and startup tasks are untouched.

## 10. Rollback procedure
- **Config/agent rollback:** drain → stop → re-copy the previous tagged `deploy/beta-agent/` (atomic swap, §8)
  → `validate.py` → leave stopped, awaiting approval.
- **Full removal:** the §9 uninstall.
- **Reinstall integrity:** use the **virtual service account** (stable SID, §1) so retained-dir ACLs and owner
  tags still resolve after a reinstall; an unreadable owner tag is a **hard error, never "free."**
- **Backend rollback** (independent): the beta path is dark by default (`BETA_RUNTIMES_ENABLED` off); disabling
  the flag stops all provisioning regardless of the agent. Backend image rollback tag: `rollback-preCvmInc3ABc`.
- No rollback step ever touches the bridge, terminal, Session 3 or `:8788`.

## 11. Logging location
- Agent app log: **`C:\GuvFX\beta\agent-state\logs\agent.log`** (rotating), owned by the service account +
  Administrators only. **Request bodies, paths, nonces and secrets are never logged.**
- Durable evidence (nonces, idempotency, completed-op records, op-markers):
  **`C:\GuvFX\beta\agent-state\state.sqlite`.**
- Both live under `…\agent-state\`, **separate from the code dir** (`…\agent\`) so updates/rollbacks (§8) never
  clobber them.
- Windows service start/stop + crash events in the System/Application event log under the service name.
- **Separate** from Nuno's `C:\GuvFX\*.log` — no shared log files.

## 12. Maximum expected CPU/RAM
- **Agent process:** a single idle-until-called stdlib HTTP server → **~30–60 MB RAM**, **<1% CPU** at rest.
- **Each beta MT5 runtime** (separate process): **~137 MB RAM** (6-runtime coexistence measurement), near-idle CPU.
- First proof = **one** runtime → agent + 1 runtime ≈ **~200 MB** against ~10.4 GB free.
- **Pre-auth resource-exhaustion cap (SHOULD_FIX, required for a shared live host).** `do_POST` currently reads
  `Content-Length` bytes with **no upper bound** *before* any HMAC/nonce check, and `ThreadingHTTPServer`
  spawns an unbounded thread per connection with **no socket timeout** — so authentication does **not** gate
  resource use. If `:8791` ever becomes reachable by a non-backend peer (the realistic firewall/ACL failure
  modes), a single unauthenticated request with a huge `Content-Length`, or a connection flood, could exhaust
  the ~10.4 GB free RAM / thread budget on the box that runs Nuno's live terminal + bridge. Fix: reject
  `Content-Length` over a small fixed bound (the signed request is a few KB) **before** reading the body
  (413/denied), add a per-connection socket timeout, and cap concurrent connections.

## 13. Process ownership
- **Agent service** → the least-privilege service identity (§1), Session 0.
- **Beta MT5 runtime** → launched via the fixed task **under a dedicated non-admin runtime identity** (§2/§17).
  **Session correction (SHOULD_FIX):** a non-admin identity that logs on gets its **own** session — it is **not**
  Session 1 (Nuno's Administrator autologon session hosting `GuvFX_Autostart`/`GuvFX_SignalBridge`). The beta
  runtime must **never** share Nuno's desktop / window station / input queue / clipboard. (§6 said "Session 1" —
  that was wrong for a separate identity and is corrected here.)
- **Terminate is delegated (B-1):** STOP/TOMBSTONE act on a PID **only** if its image is verified beneath the
  owned canonical path, and the actual kill runs **as the runtime identity**, never the service reaching across
  an identity boundary — and never by exe name (both Nuno's operator terminal and a beta runtime are
  `terminal64.exe`; only the full **image path** distinguishes them, which `find_runtime_process` must return —
  B-3).
- **Nuno's terminal** (Session 3) and **bridge** (PID 1856) are owned by different identities/sessions and are
  never enumerated as beta-owned.

## 14. Interaction with the existing bridge
**None, by construction.**
- Different port (`:8791` vs `:8788`), different bind (private-only vs `0.0.0.0`), different process, identity,
  and install dir.
- The agent never opens/reads/references/connects to `:8788`; there is no protocol field that can carry a port,
  and the code contains no reference to 8788.
- No shared files, no shared task, no boot ordering. The bridge's startup tasks and Session 1 autostart are not
  modified. The agent provisions **new** headless runtimes under `…\beta\accounts\` only.

## 15. Why a Windows Service is preferable to an on-demand scheduled task
- **Bounded, always-consistent identity + rights:** one fixed least-privilege account with an explicit
  ACL/right set, vs a per-invocation task that is easier to misconfigure/escalate.
- **Single controlled listener + lifecycle:** the backend needs a durable responder to negotiate + drive a
  multi-step job (MATERIALISE→START→VERIFY…); a service gives one long-lived process with a clean drain-aware
  stop, health, and recovery policy, and holds the private socket + durable replay/idempotency state + the
  one-mutating-op lock coherently. A per-request task cannot.
- **Auditability + control:** `sc` state, event-log lifecycle, and a single manual start make the
  "install-only, start-on-approval" gate trivial. (The *interactive MT5 launch* still uses a task — the right
  tool for the one interactive action — but the control plane is the service.)
- **Caveat (B-5):** this rests on a real service harness, which must be added + hashed before first start.

## 16. First installation procedure (INSTALL ONLY — no start)
**Precondition:** every §0 blocking item (B-1…B-9) is merged reviewed code, and §17 runtime identity is chosen.
Run on the box as Administrator. **Every step is install/verify only; nothing is started.**
1. Copy the exact **merged** `deploy/beta-agent/` to `C:\GuvFX\beta\agent\`; state DB + logs directory
   `C:\GuvFX\beta\agent-state\`. Do not overwrite anything outside `…\beta\`.
2. Create the least-privilege service account/identity (§1) + the dedicated runtime identity (§17); grant the
   service **only** `SeServiceLogonRight`.
3. Apply the scoped NTFS ACLs (§3). Stage the checksummed golden copy into `…\beta\golden-stage\` (no golden
   DACL write). Do **not** provision the signing keyring yet (deferred to the approved start, §1).
4. Create the service **stopped, `start=demand`**, binPath pointing at the **service wrapper** (§6/B-5), obj =
   the service identity. Recovery **disabled**.
5. Add the single scoped firewall rule (§5) **after** the pre-existing-rule enumeration (B-8) passes.
6. **Do NOT register the interactive launch task** (deferred to the post-approval start-enablement phase, §6).
7. **Verify (no start):**
   - **binary** — bundle present at the install path; interpreter **import smoke test** (no listener):
     `C:\GuvFX\python311.exe -B -c "import sqlite3, http.server, sys; sys.path.insert(0, r'C:\GuvFX\beta\agent'); import config, manifest"`.
   - **checksum** — `python -B validate.py` passes: **full-bundle** manifest match (all executable modules, B-7)
     **and** authenticity vs the merged commit **and** the bind-guard refuses public binds. (Reword the old
     "all modules" claim to name exactly what is covered.)
   - **permissions** — `icacls` shows the scoped ACLs, **no** grant outside `…\beta\`, **no** ACE added to
     golden; the service account is in no privileged group.
   - **firewall** — `Get-NetFirewallRule -DisplayName GuvFX-Beta-Agent-In | Get-NetFirewallAddressFilter`
     (assert `RemoteAddress==100.119.23.29`, `LocalAddress==100.79.101.19`) and `| Get-NetFirewallPortFilter`
     (`LocalPort==8791`, TCP), `Action=Allow`, `Enabled=True`; **plus** the B-8 enumeration showing no broader
     rule opens `:8791`. (Live external reachability is a **post-start** step, §5.)
   - **bind pin** — assert resolved `BETA_AGENT_BIND_HOST == 100.79.101.19` (config read, no listener; B-9).
   - **service configuration** — `sc qc GuvFXBetaAgent` (`start=demand`, least-priv identity, wrapper binPath,
     state **STOPPED**) **and** `sc qfailure GuvFXBetaAgent` (no restart/run-program/reboot actions; §7).
8. Confirm bridge `:8788` (PID 1856), Session 3, autologon and startup tasks are **unchanged** (read-only re-check).
9. **STOP. Await explicit approval before the first manual `sc start`.**

No auto-start, no reboot, no autologon change, no Session-3/terminal/bridge/`:8788` interaction at any step.

---

## 17. Open decisions for Nuno
1. **Beta runtime OS identity (drives B-1/B-2/B-3).**
   - **Recommended — dedicated non-admin identity per runtime** (e.g. `guvfx_u_beta_<uuid>`): matches the
     estate's per-account isolation, gives real cross-tenant separation, and each `<uuid>` folder is ACL'd to
     its own identity. Terminate/launch are delegated to that identity's task.
   - **First-proof-only — a single shared `guvfx_u_beta`:** acceptable **for the one-runtime proof if
     explicitly recorded that multi-tenant isolation is NOT yet provided**, and any **second** runtime is gated
     on the per-identity model.
   - **Rejected — Administrator Session 1:** the beta runtime would share Nuno's live interactive desktop and,
     as a same-identity (Administrator) process, only image-path containment would separate a beta kill from the
     bridge/operator terminal. Not recommended.
   In **all** non-admin cases, terminate is a delegated runtime-identity action (B-1) — not a service admin grant.
2. **Service hosting mechanism (B-5):** pywin32 `ServiceFramework` (no extra binary, adds a pip dep to a
   currently stdlib-only box) **vs** a reviewed, hashed `nssm.exe` (extra binary, no pip dep). Recommend
   pywin32 unless you prefer to avoid adding Python packages to the box.
3. **Tailscale ACL (§4):** approve adding the backend-only ACL for `100.79.101.19:8791` as a second isolation
   layer (recommended — removes the single-firewall-rule SPOF).

## Recommended next action (bounded)
**Do not install.** Run a single **B3-PREP reviewed-code increment** — implement → tests → adversarial review →
CI green → merge — that closes B-1…B-9 plus the SHOULD_FIX items (service harness, delegated terminate/launch,
per-runtime identity + per-`<uuid>` ACL, full-bundle manifest + authenticity, drain marker + stop handler,
atomic-swap update, state-out-of-copy-target, exact-bind pin, resource caps, uninstall completeness, firewall
pre-enumeration), **contingent on Nuno's §17 decisions.** Only after that increment merges does the §16
install-only step run, followed by the approval-to-start gate. No box changes until then; all recon to date was
read-only.

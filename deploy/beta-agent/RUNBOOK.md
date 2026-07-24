# Beta Provisioning Agent — Operational Runbook (CVM-Inc-3 B2 / B3P-1 / B3P-2)

A small, private-network Windows service that performs **only** allowlisted, UUID-scoped beta-runtime
provisioning operations for the GuvFX backend, over a signed, replay-resistant protocol. It **never**
accepts a command, script, path, argument, executable or environment value from the network.

> **Still a dark artefact.** Reviewed, CI-tested and merged; **nothing here has ever executed on a Windows
> host.** The controlled install is gated behind the Install Authorisation packet
> (`docs/B3P2_INSTALL_AUTHORISATION_PACKET.md`), the first service start behind a further approval, and the
> bounded MT5 viability trial behind that.

## Execution model (B3P-2 — read this first)
A runtime occupies one **pre-provisioned slot**: a fixed non-admin identity `guvfx_b_slot<n>`, a fixed
directory `C:\GuvFX\beta\slots\<n>\terminal`, and two fixed scheduled tasks the agent may only
**trigger** — `GuvFXBetaRuntime-<n>` and `GuvFXBetaRuntimeStop-<n>`. `(slot, generation)` identifies one
immutable occupancy.

The agent creates no OS object, holds no runtime credential, and has **no method** to kill a process, launch
one, delete a directory or write an ACL. Those absences are the security property.

Two semantics are load-bearing: a trigger being accepted is **not** evidence MT5 started (only observed
process-birth evidence completes a launch), and STOP succeeds only when the process is **ABSENT** — never
merely "termination requested".

## What it exposes
`POST /provision` only. Operations: `NEGOTIATE` (read-only handshake), `MATERIALISE`, `START`, `VERIFY`,
`STOP`, `TOMBSTONE`. No other route. `TOMBSTONE` **moves** the slot dir to
`C:\GuvFX\beta\tombstones\<slot>\<occupancy_id>\` — it never deletes. Permanent deletion is out of scope.

**Launch gate.** `START` refuses unless the installed launch task matches its **approved definition**
field-for-field (`approved_tasks.json`, written by `install_pool.ps1`). Drift, a changed principal or run
level, a lost `/portable`, a disabled task or an absent task all block — and nothing is triggered. The agent
never repairs a task.

## Security boundaries (enforced in code)
- Binds only to a private/Tailscale address; **startup fails** on `0.0.0.0`/public (`config.assert_private_bind`).
  The **live** service pins the bind to the EXACT expected management address and refuses a port in
  `{8787, 8788, 3389}` (`config.assert_exact_bind` / `load_config`, verification B-9).
- **Pre-auth resource limits** (verification): an oversize `Content-Length` is refused (413) BEFORE the body
  is read; each connection has a socket timeout; concurrent connections are capped — an unauthenticated peer
  cannot exhaust the host RAM/thread budget.
- Every request: signature (HMAC, constant-time), bounded skew, short expiry, single-use nonce (durable),
  `key_id` rotation. Canonical path derived **locally** from the UUID; containment beneath
  `C:\GuvFX\beta\accounts`; reparse/symlink/junction escape refused; production/Nuno paths + port 8788
  structurally excluded (never referenced).
- Process attribution is scoped by the slot's **runtime identity** (owner SID), then by image **path** —
  never by image name. A materialised slot contains `terminal64.exe`, the same name as the operator's
  production terminal, so a name-based match could not tell them apart. `START` observes before triggering
  and refuses on anything that is not a proven ABSENT, so it can never launch a second terminal.
- One mutating op per runtime + a global mutation cap → `BUSY`. Replay + completed-op evidence survive
  restart (SQLite). Mutating ops refused if **any** executable module's checksum ≠ approved manifest
  (full-bundle integrity, verification B-7 — `agent.py`/`config.py`/`stores.py`/`service.py` are now covered).
- **Drain-aware stop** (verification B-6): `AgentServer.stop` waits for in-flight mutating ops to finish
  (bounded by `BETA_AGENT_DRAIN_TIMEOUT_S`) before shutting the socket, so `sc stop` never kills a mutating
  op mid-flight.
- Responses carry only an allowlist of fields (no creds/env/cmdlines/exceptions/path listings).

## Config (env, service-scoped — never Git)
See `config.example.json` (no secrets). Beyond the B2 set, the **slot-pool model requires** — and refuses to
start without — `BETA_AGENT_EXECUTION_MODEL=slot_pool`, `BETA_AGENT_SLOT_POOL_SIZE` ≥ 1, all three of
`BETA_AGENT_GOLDEN_DIR` / `_DIGEST` / `_MANIFEST_VERSION`, `BETA_AGENT_APPROVED_TASKS`, and
`BETA_AGENT_DRAIN_TIMEOUT_S` **> 30** (it must exceed the settle window; the old default of 20 is refused).
`BETA_AGENT_SLOTS_ROOT` is refused unless it equals the fixed namespace — slot paths come from code, not
configuration.

**Note:** a machine env var is not a protected secret store — scope the keyring to the service account and
rotate it (DPAPI retrieval is a follow-up).

## Service host (pywin32)
`service.py` wraps `AgentServer` in a `win32serviceutil.ServiceFramework` so the SCM can start/stop it as a
real service (a raw `python agent.py` binPath would fail SCM start, error 1053). All logic lives in the
off-box-tested `AgentServer`; `service.py` is a thin Windows-only delegate.

## Offline validation (safe anywhere — no server, no side effects)
```
python -B validate.py --expect-manifest-sha256 <reviewed-commit manifest.json sha256>
```
Proves the bind-guard refuses public binds, the exact-bind pin accepts only the management address, the full
bundle matches the manifest, and — with `--expect-manifest-sha256` — that `manifest.json` itself equals the
reviewed commit (so matched tampering of an impl file + its manifest entry is still caught).

## Install (controlled; INSTALL-ONLY first, no start)
**Every script is dry-run by default. Run PLAN, read it, get approval, then `-Apply`. The PLAN output is
installation evidence.** Full order, timings, rollback points and stop conditions:
`docs/B3P2_INSTALL_AUTHORISATION_PACKET.md`.

1. **Operator** — stage the golden MT5 image into `C:\GuvFX\beta\golden\`, clean of per-instance state
   (`config\accounts.dat`, `config\servers.dat`, `bases\`, `logs\`, `MQL5\Logs\`, `MQL5\Profiles\`).
   Add `.guvfx_golden_manifest` (the approved image version) and `.guvfx_portable`. Record the tree digest.
2. **Operator** — `install_pool.ps1`: creates the four non-admin identities, grants `SeBatchLogonRight`,
   creates the slot/tombstone directories and per-slot ACLs, registers the **8 tasks disabled with no
   triggers**, and writes `approved_tasks.json` by reading each registration back through the same COM
   interface the agent uses. **Prompts for passwords as SecureString — they are never parameters.**
3. **Operator** — place the pinned WinSW wrapper (`WinSW.NET4.exe` v2.12.0, SHA-256
   `923111c7…f4cb66`) at `C:\GuvFX\beta\winsw-src\WinSW.NET4.exe`, and stage the agent bundle to
   `C:\GuvFX\beta\agent\` **including the `winsw\` subdir** (`GuvFXBetaAgent.xml`). Introducing a new
   executable to the production host is operator-gated; `install_service.ps1` **refuses** any WinSW binary
   whose hash does not match the pin, and hard-fails if the `winsw\` config is missing.
4. `install_service.ps1`: the service host is a **WinSW wrapper**, not pywin32 (see
   `docs/B3P_SERVICE_HARNESS_COMPARISON.md` and the 2026-07-24 STOP). It validates the interpreter by PE
   metadata; validates the XML *contract* (the `<executable>` equals the validated `-Python`, recovery is a
   single `action=none`, `stoptimeout` exceeds `BETA_AGENT_DRAIN_TIMEOUT_S`); sets scoped ACLs (Modify on
   state/tombstones/slots — the agent stages and moves runtimes itself; ReadAndExecute on the golden image,
   its own code, the WinSW dir **and the venv**); registers a **manual-start** service as the
   `NT SERVICE\GuvFXBetaAgent` virtual account with **recovery disabled** and **no start**; then MEASURES
   (before/after) that no pywin32 DLL was written to System32 or the base interpreter. It refuses to run
   until the pool exists.
5. `firewall.ps1`: scoped **Block (all-except-backend) + Allow (backend)** on the agent port, verified
   numerically, **without** changing the machine-wide profile default (the bridge's `:8788` is untouched).
6. Add the **Tailscale ACL** (backend node → `100.79.101.19:8791` only) as a second isolation layer.
7. Verify per the install review §7: identities, ACLs, task definitions and digests, service
   (`sc qc` + `sc qfailure` — STOPPED, demand, correct `NT SERVICE\GuvFXBetaAgent` identity, WinSW wrapper
   binary, no recovery), the measured no-global-DLL result, firewall, bundle checksums, approvals; and
   confirm the estate is byte-identical — production task XML digests, Nuno's terminal PID **and creation
   FILETIME**, `:8787`/`:8788` still bound by the same processes, autologon and uptime unchanged.
   **STOP and await approval.**
8. After approval: provision the keyring, start **once**, confirm `NEGOTIATE`, then the read-only
   observation probe. The bounded MT5 viability trial follows only if the probe succeeds.

## Rollback
`uninstall.ps1` (dry-run, then `-Apply`) — revoke the service ACL grants (**including the WinSW dir and the
venv**), **WinSW-uninstall** then `sc.exe delete` the service, remove the staged WinSW wrapper dir
(`agent-winsw`), remove the firewall rule, **both** task families (launch and stop, so no stored credential
is orphaned), the per-slot grants, and `SeBatchLogonRight`. Identities are **disabled, not deleted**, unless
`-RemoveIdentities` is passed: deletion orphans anything they own and destroys attribution for retained
tombstone evidence. Slot dirs, tombstones and `agent-state\` (the evidence chain, incl. WinSW's captured
child logs) are retained. Nuno's terminal, bridge,
Session 3 and port 8788 are never touched. Disabling/resetting the Windows Firewall on this host is a **Red
action** (it de-isolates :8791). Backend rollback: `BETA_RUNTIMES_ENABLED` off disables all beta provisioning
regardless of the agent.

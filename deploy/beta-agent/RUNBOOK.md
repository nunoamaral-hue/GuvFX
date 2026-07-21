# Beta Provisioning Agent — Operational Runbook (CVM-Inc-3 B2 / B3P-1)

A small, private-network Windows service that performs **only** allowlisted, UUID-scoped beta-runtime
provisioning operations for the GuvFX backend, over a signed, replay-resistant protocol. It **never**
accepts a command, script, path, argument, executable or environment value from the network.

> **Still a dark artefact (B2 → B3P-1).** This directory is reviewed, CI-tested and merged. It may be
> *copied* to the Windows host after merge, but it is **NOT** installed/started; no firewall rule, scheduled
> task, autologon change, reboot, or any touch of Session 3 / port 8788. B3P-1 hardens the service (harness,
> drain, full-bundle integrity, exact-bind pin, resource caps, network scripts); **B3P-2** adds the
> per-runtime identity + launch/terminate; the **controlled install (INSTALL_REVIEW.md §16)** and the
> end-to-end proof are separately gated behind Nuno's approval.

## What it exposes
`POST /provision` only. Operations: `NEGOTIATE` (read-only handshake), `MATERIALISE`, `START`, `VERIFY`,
`STOP`, `TOMBSTONE`. No other route. `TOMBSTONE` **moves** the runtime dir to
`C:\GuvFX\beta\tombstones\<uuid>\<timestamp>\` — it never deletes. Permanent deletion is out of scope.

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
- `STOP`/`TOMBSTONE` act on a PID **only** if its process image is beneath the owned canonical path
  (never by exe name). `START` is idempotent (never a second terminal). Ownership tag conflict is refused.
  *(The per-runtime identity + delegated launch/terminate that back these are the B3P-2 increment.)*
- One mutating op per runtime + a global mutation cap → `BUSY`. Replay + completed-op evidence survive
  restart (SQLite). Mutating ops refused if **any** executable module's checksum ≠ approved manifest
  (full-bundle integrity, verification B-7 — `agent.py`/`config.py`/`stores.py`/`service.py` are now covered).
- **Drain-aware stop** (verification B-6): `AgentServer.stop` waits for in-flight mutating ops to finish
  (bounded by `BETA_AGENT_DRAIN_TIMEOUT_S`) before shutting the socket, so `sc stop` never kills a mutating
  op mid-flight.
- Responses carry only an allowlist of fields (no creds/env/cmdlines/exceptions/path listings).

## Config (env, service-scoped — never Git)
`BETA_AGENT_BIND_HOST`, `BETA_AGENT_EXPECTED_BIND_HOST`, `BETA_AGENT_BIND_PORT`, `BETA_AGENT_KEYRING`
(JSON `{key_id: secret}`), `BETA_AGENT_KEY_ID`, `BETA_AGENT_ROOT`, `BETA_AGENT_TOMBSTONE`,
`BETA_AGENT_STATE_DIR` (holds `state.sqlite` + `logs/`, **separate from the code dir**), `BETA_AGENT_MANIFEST`,
`BETA_AGENT_MAX_BODY_BYTES`, `BETA_AGENT_MAX_CONNECTIONS`, `BETA_AGENT_REQUEST_TIMEOUT_S`,
`BETA_AGENT_DRAIN_TIMEOUT_S`. See `config.example.json` (no secrets). **Note:** a machine env var is not a
protected secret store — scope the keyring to the service account and rotate it (DPAPI retrieval is a follow-up).

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

## B3 install (controlled; INSTALL-ONLY first, no start)
1. Provision the virtual service account `NT SERVICE\GuvFXBetaAgent` (no password) + install pywin32 into the
   agent interpreter. Provision the signing keyring **only at the post-approval start**.
2. `install_service.ps1` (dry-run, then `-Apply`): scoped ACLs, pywin32 service `start=demand`, recovery
   disabled, **no start**; then `firewall.ps1` (dry-run, then `-Apply`): pre-existing-rule gate +
   `DefaultInboundAction=Block` assertion + single backend-scoped allow on the Tailscale profile.
3. Add the **Tailscale ACL** (backend node → `100.79.101.19:8791` only) as a second isolation layer.
4. Verify with `sc qc` + `sc qfailure` (STOPPED, manual, correct identity, no recovery), `icacls`, and the
   firewall introspection commands; confirm `:8788`/Session 3/autologon unchanged; **STOP and await approval**.
5. After approval: start; confirm `NEGOTIATE` reports the expected protocol/agent/manifest/ops; probe :8791
   reachability from a non-backend tailnet peer (must be refused). Running one broker-independent runtime
   end-to-end is the B3P-2 increment (per-runtime identity + launch).

## Rollback
`uninstall.ps1` (dry-run, then `-Apply`) — stop + remove the service, its firewall rule, its ACL grants and
any launch tasks. Runtime + tombstone directories are retained (no deletion). Nuno's terminal, bridge,
Session 3 and port 8788 are never touched. Disabling/resetting the Windows Firewall on this host is a **Red
action** (it de-isolates :8791). Backend rollback: `BETA_RUNTIMES_ENABLED` off disables all beta provisioning
regardless of the agent.

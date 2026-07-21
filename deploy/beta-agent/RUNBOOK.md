# Beta Provisioning Agent — Operational Runbook (CVM-Inc-3 B2)

A small, private-network Windows service that performs **only** allowlisted, UUID-scoped beta-runtime
provisioning operations for the GuvFX backend, over a signed, replay-resistant protocol. It **never**
accepts a command, script, path, argument, executable or environment value from the network.

> **B2 = dark artefact only.** This directory is reviewed, CI-tested and merged as B2. It may be *copied*
> to the Windows host after merge, but B2 does **NOT** install/start the service, add firewall rules,
> create scheduled tasks, change autologon, reboot, or touch Session 3 or port 8788. **B3** performs the
> controlled install + end-to-end proof.

## What it exposes
`POST /provision` only. Operations: `NEGOTIATE` (read-only handshake), `MATERIALISE`, `START`, `VERIFY`,
`STOP`, `TOMBSTONE`. No other route. `TOMBSTONE` **moves** the runtime dir to
`C:\GuvFX\beta\tombstones\<uuid>\<timestamp>\` — it never deletes. Permanent deletion is out of scope.

## Security boundaries (enforced in code)
- Binds only to a private/Tailscale address; **startup fails** on `0.0.0.0`/public (`config.assert_private_bind`).
- Every request: signature (HMAC, constant-time), bounded skew, short expiry, single-use nonce (durable),
  `key_id` rotation. Canonical path derived **locally** from the UUID; containment beneath
  `C:\GuvFX\beta\accounts`; reparse/symlink/junction escape refused; production/Nuno paths + port 8788
  structurally excluded (never referenced).
- `STOP`/`TOMBSTONE` act on a PID **only** if its process image is beneath the owned canonical path
  (never by exe name). `START` is idempotent (never a second terminal). Ownership tag conflict is refused.
- One mutating op per runtime + a global mutation cap → `BUSY`. Replay + completed-op evidence survive
  restart (SQLite). Mutating ops refused if the on-disk implementation checksum ≠ approved manifest.
- Responses carry only an allowlist of fields (no creds/env/cmdlines/exceptions/path listings).

## Config (env, via the Windows secret store — never Git)
`BETA_AGENT_BIND_HOST`, `BETA_AGENT_BIND_PORT`, `BETA_AGENT_KEYRING` (JSON `{key_id: secret}`),
`BETA_AGENT_KEY_ID`, `BETA_AGENT_ROOT`, `BETA_AGENT_TOMBSTONE`, `BETA_AGENT_STATE_DB`,
`BETA_AGENT_MANIFEST`. See `config.example.json` (no secrets).

## Offline validation (safe anywhere — no server, no side effects)
```
python validate.py
```
Proves the bind-guard refuses public binds and the manifest matches the on-disk implementation.

## B3 install (controlled; not part of B2)
1. Provision the least-privilege service account + secrets (Windows secret store).
2. `install_service.ps1` (min identity), `firewall.ps1` (control-plane source only, private interface).
3. Start; confirm `NEGOTIATE` reports the expected protocol/agent/manifest/ops; run one broker-independent
   runtime end-to-end via the worker.

## Rollback
`uninstall.ps1` — stop + remove the service and firewall rule. Runtime + tombstone directories are
retained (no deletion). Nuno's terminal, bridge, Session 3 and port 8788 are never touched.

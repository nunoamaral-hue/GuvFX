# GuvFX Hosted Signed Executor (Stream 7C) — DARK, not deployed

The runnable host end of the Stream 5 signed provisioning transport. It is the process that lets the backend's
`SignedHostExecutor` actually run `prepare_hosted_slot` on a Windows host: an authenticated HTTP listener that
hands each signed request to `host_agent_dispatch.dispatch`, which verifies it, refuses Customer Zero, derives
identity/paths from `account_id`, maps the allow-listed operation to exactly one reviewed `.ps1` primitive, runs
it, and returns a signed response.

**Posture:** this bundle is a **complete, reviewable repository deliverable**. It is **not deployed**. Deployment,
secret provisioning, and the live disposable-host certification are a separate, Sponsor-gated packet
(`docs/operations/hosted-workspace/HOSTED_EXECUTOR_DEPLOY_RUNBOOK.md`). Execution stays DARK
(`HOSTED_HOST_EXECUTOR_ENABLED` unset; the backend never contacts a host until it is armed).

## What it cannot do (no-RCE contract, ADR-0036/0037)

There is no route, header, or field through which a command, PowerShell string, executable path, filesystem
path, username, or task definition can be submitted. The wire carries only `operation` (fixed allow-list) +
`account_id` (int) + typed scalar `params` + optionally a sealed Windows password. A primitive name resolves to
exactly one reviewed `.ps1`; execution is a fixed argument vector (never a shell string); the password is written
to the child's stdin, never argv.

Most operations are **account-scoped**: `account_id > 0`, from which the host derives the slot identity and every
path, and the reserved-identity (Customer-Zero) guard applies. The **account-independent** operations
(`host_protocol.ACCOUNT_INDEPENDENT_OPERATIONS` — `STAGE_CATALOGUE_ARTEFACT`, `ATTEST_CATALOGUE_ARTEFACT`, ADR-0068)
act on the **shared catalogue root** `C:\GuvFX\catalogue\versions\<label>\<broker>\servers.dat`, not a slot: they
carry the sentinel `account_id == 0`, derive no slot, and the reserved-id guard does not apply (there is no account).
`STAGE` carries the approved bytes as a signed `payload` (bound via `payload_digest`, piped to the `.ps1` stdin as
base64 — never argv), host-verified against `expected_sha256`/`size_bytes`; `ATTEST` is read-only (SHA/size, or the
version's staged inventory). The destination is host-derived from the validated `broker`/`label` slugs and confined
under the catalogue root (no traversal, no reparse-point escape).

## Files

| File | Role |
|---|---|
| `daemon.py` | HTTP listener (`POST /hosted/provision`, `GET /hosted/health`) + lifecycle (exclusive bind, bounded connections, drain-on-stop, crash→non-zero exit) |
| `daemon_config.py` | RULE-3 config: own HMAC keyring + envelope private keyring, exact-bind pin, forbidden ports, fail-closed on any missing/placeholder secret |
| `nonce_store.py` | durable single-use SQLite nonce store (replay protection) |
| `primitive_runner.py` | primitive→`.ps1` contract table, ParseFile gate (RULE 9), fixed-argv subprocess, password→stdin |
| `envelope_open.py` | opens the sealed Windows password with the host private key (ADR-0027; AAD byte-identical to the seal side) |
| `lib/broker_cred_envelope.py` | vendored Django-free envelope crypto (byte-identical to `deploy/beta-agent/`; drift-guarded in tests) |
| `winsw/*.xml` | WinSW service configs (Dark install-only + Supervised target) |
| `install_service.ps1` | the single sanctioned installer (WinSW hash-pin, XML contract, ParseFile-gate primitives, `sc config obj=` identity — **LocalSystem** by default per ADR-0040; NT SERVICE virtual account still selectable via `-RunAsUser`, which then also grants `SeServiceLogonRight` — install-only verify, rollback) |
| `stage-manifest.json` | authoritative source→host staging map (test-validated) |

The runner's `host_protocol` + `host_agent_dispatch` are the **single source of truth in
`backend/hosted_workspace/`** (Django-tested); the installer stages them into `lib/hosted_workspace/` on the host,
and under CI the tests import the real backend modules. Nothing is duplicated in the repo.

## Configuration — by reference only (RULE 3)

All secrets are provisioned as **machine environment variables** via the approved Windows secret mechanism
before first start; they are never in this bundle, the WinSW XML, or the logs. The daemon **fails closed** if any
required value is missing or looks like a placeholder. See `config.example.env` for the full contract. Summary:

- `HOSTED_EXECUTOR_KEYRING` (JSON `{key_id: secret}`) + `HOSTED_EXECUTOR_KEY_ID` — the executor's **own** HMAC
  keyring (distinct from `BETA_AGENT_KEYRING`; never a fall-back to another service's secret).
- `HOSTED_EXECUTOR_ENC_PRIVKEYS` (JSON `{key_id: b64 x25519 private key}`) — the envelope private keyring, a
  distinct scope, used only to open the sealed Windows password. The backend holds the matching public key.
- `HOSTED_EXECUTOR_BIND_HOST` — the exact private/Tailscale management address to bind (the daemon refuses a
  wildcard/public/loopback/alternate-NIC bind). `HOSTED_EXECUTOR_BIND_PORT` defaults to `8790` (the ports 8791,
  8788, 8787, 3389 are refused).
- Optional: `HOSTED_EXECUTOR_SCRIPTS_DIR`, `HOSTED_EXECUTOR_STATE_DIR`, `HOSTED_EXECUTOR_RESERVED_ACCOUNT_IDS`
  (Customer Zero `{1}` is a hard floor regardless), resource/timeout limits.

## Rotation

Add a new `key_id` to `HOSTED_EXECUTOR_KEYRING` on both the backend and the host (verify accepts any known key),
cut over `HOSTED_EXECUTOR_KEY_ID`, then retire the old key. Envelope keys rotate the same way via
`HOSTED_EXECUTOR_ENC_*`. Update `docs/SECRET_INVENTORY.md` (RULE 4).

## Primitives added by ADR-0049 (LIVE bridge provisioning integrator)

Two account-scoped primitives (mirror `activate_tenant_bridge`, NOT the account-independent catalogue ops):

- `Activate-GuvfxTenantBridgeLive.ps1` (`ACTIVATE_TENANT_BRIDGE_LIVE_AUTHORIZED`) — writes the backend-rendered
  LIVE env (`MT5_ALLOW_LIVE=1`, carried in the signed payload, base64 via STDIN) to `C:\GuvFX\tenants\<id>\`,
  restarts ONLY `GuvFX_TenantBridge_<id>`, attests. The `.ps1` decides no policy; it re-validates allow-listed
  keys + the live posture and writes what the backend rendered.
- `Restore-GuvfxTenantBridgeExecutionOff.ps1` (`RESTORE_TENANT_BRIDGE_EXECUTION_OFF`) — the risk-reducing
  counterpart: writes the execution-OFF env (refuses any residual `MT5_ALLOW_LIVE`), restarts ONLY that tenant,
  attests OFF. Safe/idempotent with no valid authorization and the LIVE arm flags OFF.

Both are DARK: `COPIER_LIVE_EXECUTE_ENABLED` + `HOSTED_LIVE_EXECUTION_ENABLED` default OFF, the signed executor
is DARK-gated, and no authorization exists until a human runs `provision_authorized_live_copier_bridge --commit`.

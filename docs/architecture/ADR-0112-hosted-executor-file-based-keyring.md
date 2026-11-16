# ADR-0112 - Hosted signed-executor reads its HMAC keyring from an ACL-protected file (SCM env-cache fix)

- **Status:** Accepted (repository-only; additive; migration-free; default behaviour backward-compatible; no
  production deploy, Windows-host mutation, service restart, reboot or keyring mutation by this packet).
- **Packet:** GFX-HOSTED-EXECUTOR-FILE-BASED-KEYRING-144.
- **Date:** 2026-11-16.
- **Builds on (never replaces):** ADR-0037 (the signed host executor + `HOSTED_EXECUTOR_KEYRING`/`_KEY_ID`),
  ADR-0039/0040 (the WinSW service + privilege model), and the pkt-124 value-blind rotation tooling
  (`rotation_ring_op.py`, `Rotate-GuvfxRingMachineVar.ps1`). The signed protocol (`host_protocol.py`), the
  operation allow-list, Customer-Zero refusal and every execution gate are unchanged.

## Context

The `GuvFXHostedExecutor` Windows service loads its HMAC signing keyring from its **process environment**
(`daemon_config.load_config` -> `os.environ`); the WinSW child inherits the environment block the Service
Control Manager provides. **The SCM caches the system environment block at boot.** On a host that has not been
rebooted since a machine-variable keyring rotation, a `Restart-Service` therefore inherits the **stale cached
block**, not the updated machine variable - so the restarted verifier keeps checking signatures against the
**retired** key.

This defect was diagnosed in production (GFX-143): after the 2026-11-15 `HOSTED_EXECUTOR_KEYRING` rotation to
active id `hx-20261115`, the host machine variable and the backend keyring were both correctly aligned on
`hx-20261115` (confirmed value-blind), yet the signed `RESTORE_TENANT_BRIDGE_EXECUTION_OFF` transport to the six
monitoring bridges failed `unknown_key_id`. The host had `LastBootUpTime = 2026-09-30` - six weeks before the
rotation - with no per-service `Environment` registry override, so the running verifier held the pre-rotation
key. A `Restart-Service` could not have fixed it; only a host reboot (high impact: the shared Customer-Zero
host) or a per-service registry secret (a new secret location) would.

## Decision

Make the keyring source a **file the operator rewrites atomically**, removing the boot/SCM coupling entirely.

1. **File-first loader.** `daemon_config` reads the HMAC keyring from one fixed, ACL-protected JSON file -
   `HOSTED_EXECUTOR_KEYRING_FILE` or `DEFAULT_KEYRING_FILE`
   (`C:\GuvFX\hosted\secrets\hosted_executor_keyring.json`), shaped
   `{"keyring": {key_id: secret}, "key_id": "<active>"}` - with explicit, fail-closed parsing and validation
   (shared with the env loader via `_validate_keyring_map`): non-empty JSON object, string key ids -> non-empty
   string secrets, no placeholder text, active id present and a key in the map.
2. **Env loader kept only as a documented migration fallback.** If the file is **absent**, the daemon falls back
   to the historical process-environment loader so a host not yet file-provisioned keeps working. A file that is
   **present but malformed / over-permissive / unreadable / invalid FAILS CLOSED** and never silently falls back
   to a possibly-stale env (a present-but-bad secret file is a security condition, not an absence). `load_config`
   records the non-secret `keyring_source` ('file' | 'env').
3. **Value-blind operator.** `Provision-GuvfxHostedExecutorKeyringFile.ps1` copies the **existing machine
   variables** `HOSTED_EXECUTOR_KEYRING`/`_KEY_ID` into the file via an atomic temp -> restrict-ACL -> write ->
   value-blind read-back -> rename. It never prints, hashes, logs, returns, places in argv, or persists either
   value anywhere but the one file; its output is only safe metadata (`ok`, `file_present`, `acl_verified`,
   `key_count`, `active_present`, `correlation_id`, `at`). It refuses under active transcription/tracing, refuses
   every test seam outside the self-test signal, supports `-Verify` (read-only) and `-Remove`, and supports
   rotation (re-provisioning over an existing file).
4. **File ACL.** Inheritance disabled; **only `SYSTEM` (Full), `Administrators` (Full) and the exact service
   principal (`NT SERVICE\GuvFXHostedExecutor`, Read)** are granted; no `Users`/`Everyone`/`Authenticated Users`.
   The provisioner both sets and independently verifies this (`-Verify`).

## Permission validation across platforms

The daemon's over-permissive refusal is cross-platform: on POSIX any group/other bit is a refusal (the file must
be owner-only, exercised by the backend tests on Linux CI); on Windows the POSIX mode bits are not a real ACL
(Python reports NTFS files as mode 0o666), so enforcement is **deferred to the NTFS ACL**, which the operator
provisioner sets and independently verifies. The authoritative Windows protection is therefore the NTFS ACL plus
the provisioner's `-Verify`, not a POSIX-mode check.

## Rotation and rollback (operator, value-blind - Sponsor-gated; NOT performed by this packet)

- **Rotation:** rotate the machine variable with the existing value-blind rotator (`Rotate-GuvfxRingMachineVar.ps1`
  Add/Activate/Retire + the VPS `rotation_ring_op.py` mirror), then re-run
  `Provision-GuvfxHostedExecutorKeyringFile.ps1` to atomically rewrite the file, then `Restart-Service
  GuvFXHostedExecutor`. **No reboot** is required - the file read at startup reflects the new keyring.
- **Rollback:** re-provision from the reverted machine variable, or `-Remove` the file (the daemon then falls back
  to the machine env on the next start). Both are value-blind and leave no weakly-protected secret on disk.

## Consequences / out of scope (Sponsor-gated - HARD STOP for this packet)

This PR delivers the loader, the operator and the tests/docs, repository-only. Making the live host use the file
still requires, as separate governed steps: run the provisioner over RDP to write the file, `Restart-Service
GuvFXHostedExecutor`, and confirm `keyring_source == 'file'`. The existing machine-variable path remains valid
until every host is file-provisioned; `SECRET_INVENTORY.md` records the new file location and the updated
rotation/rollback procedure.

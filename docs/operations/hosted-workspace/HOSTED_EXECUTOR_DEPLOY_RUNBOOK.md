# Hosted Signed Executor — deployment + disposable-host certification runbook

**Status:** the daemon is repository-complete + reviewed + tested, **DARK, not deployed** (ADR-0039). This
runbook is the **separate, Sponsor-gated packet** that deploys it and certifies it on a disposable host. Running
it is a Windows-host mutation and requires explicit Sponsor approval.

**Golden rule:** the shared prod host `100.79.101.19` (which also runs Customer Zero + the beta agent + the trade
bridge) must **never** be failure-injected. Customer Zero (`account #1` / `guvfx_u_1` / `C:\GuvFX\accounts\1`)
must never be provisioned. Execution stays DARK (`HOSTED_HOST_EXECUTOR_ENABLED` unset) until the disposable cert
below passes.

## Pre-flight (repository-side, do before touching a host)

1. **Two known residuals to resolve or accept (ADR-0039):**
   - **Client read timeout vs `MATERIALISE_RUNTIME`.** `host_executor._http_transport` posts with a 30s timeout;
     copying the ~378 MB golden runtime can exceed it → the client reports `host_unavailable` while the host
     keeps working. Before a real materialise, EITHER raise the client timeout for `MATERIALISE_RUNTIME` and add
     poll-not-repost, OR certify the non-materialise ops first and materialise under a manual longer timeout.
   - **`VERIFY_SLOT` is unimplemented on this host build** (fails closed). It is not on the `prepare_hosted_slot`
     path, so the cert does not depend on it; do not add it to the flow.
2. Confirm `make check` is green at the commit being deployed and the daemon tests pass
   (`hosted_workspace.tests_host_*`).

## Secrets (by REFERENCE only — never values in Git/Notion/logs; RULE 3/4)

Provision as **machine environment variables** on the host via the approved Windows secret mechanism, and the
matching public/HMAC halves in the backend environment. Enumerate from `docs/SECRET_INVENTORY.md` first; update
it after.

| Name | Where | Purpose |
|---|---|---|
| `HOSTED_EXECUTOR_KEYRING` (JSON) + `HOSTED_EXECUTOR_KEY_ID` | host **and** backend | the executor's own HMAC keyring (distinct from `BETA_AGENT_KEYRING`) |
| `HOSTED_EXECUTOR_ENC_PRIVKEYS` (JSON) | **host only** | envelope private keyring (opens the sealed Windows password) |
| `HOSTED_EXECUTOR_ENC_PUBKEYS` + `HOSTED_EXECUTOR_ENC_KEY_ID` | **backend only** | envelope public key (backend seals) |
| `HOSTED_EXECUTOR_BASE_URL` | backend | the host's Tailscale origin (`http://<host>:8790`) |
| `HOSTED_EXECUTOR_BIND_HOST` / `_EXPECTED_BIND_HOST` | host | the exact private interface to bind |

## Deploy (install-only; RULE 1 — supported service, never Start-Process over SSH)

1. **Stage** the bundle + primitives to the host per `deploy/hosted-executor/stage-manifest.json`:
   the daemon `.py`, `lib/broker_cred_envelope.py`, `lib/hosted_workspace/{__init__,host_protocol,host_agent_dispatch}.py`
   (from `backend/hosted_workspace/`), the reviewed `.ps1` primitives (from
   `backend/terminal_provisioning/windows/`) into the scripts dir, and the WinSW XMLs. Create the dedicated
   `executor-venv` with `cryptography` installed.
2. **ParseFile-validate** every staged `.ps1` on the target Windows PowerShell (RULE 9) — the installer does this
   too, and the daemon re-gates at startup.
3. `install_service.ps1 -InstallProfile Supervised` (dry-run) → review the plan → `-Apply`. It hash-pins WinSW,
   validates the XML contract, assigns `LocalSystem` (ADR-0040 privilege model; `-RunAsUser
   "NT SERVICE\GuvFXHostedExecutor"` selects the least-privilege account, which then also gets
   `SeServiceLogonRight`), verifies install-only (STOPPED), and rolls back on any failure.
4. Provision the `HOSTED_EXECUTOR_*` secrets (above). Do **not** set `HOSTED_HOST_EXECUTOR_ENABLED` yet.
5. **First-start gate** (Sponsor): start the service; confirm `GET /hosted/health` is `ok` from the backend over
   Tailscale, the log shows the exclusive bind, and the process is the venv python under WinSW.

## Disposable-host certification

1. **Select a disposable slot** — a non-Customer-Zero `account_id` with no broker creds, no login, no trading
   history, no CZ relationship. Positively demonstrate disposability BEFORE mutation (Windows account / profile /
   runtime / RemoteApp / Guacamole absence; DB identity, slot, node, execution state).
2. **Capture the Customer-Zero BEFORE fingerprint** (read-only): PID/session, runtime checksum, RemoteApp alias,
   singleton, AppLocker effective-policy hash, node. Confirm execution DARK + ASN #7/#8 AUTO_SHADOW + zero
   execution authority. (See `cert-stream7/cz-host-BEFORE.json` for the Stream 7B baseline format.)
3. **Run the real engine** — arm `HOSTED_HOST_EXECUTOR_ENABLED` + `HOSTED_SLOT_PREP_ENABLED` +
   `HOSTED_PERSISTENT_MT5_ENABLED` for the disposable account only; run `prepare_hosted_slot`. Do NOT hand-repair
   failures (unless fixing a primitive) — certify the ENGINE. Verify on the host: the user exists (non-admin,
   Remote Desktop Users); the workspace dir; **G5 ACL exact** (SYSTEM + Administrators + the user only,
   inheritance broken, SID-typed read-back); golden runtime; AutoTrading keys present (capability only, no
   order); RemoteApp exactly the per-account alias `/portable`; single-session; AppLocker tenant merge (audit);
   DB state agrees with reality; no duplicate runtime; no broad ACL; no desktop publication.
4. **RULE-11 controls** — positive: the disposable identity can access ONLY its runtime; negative: another
   `guvfx_u_*` cannot read the slot, `BUILTIN\Users`/`Everyone`/`Authenticated Users` are absent, Customer Zero
   is refused, a wrong username/path/account-id is refused, a duplicate op is harmless, an unknown op is refused,
   and no shell/command is expressible. Show a positive AND a negative control for every "absent" claim.
5. **Idempotency** — re-run the same request → no duplicate user/profile/RemoteApp/task, no ACL drift, no
   regression, no CZ impact → emit `HOSTED_SLOT_PROVISIONING_IDEMPOTENCY_CERTIFIED`.
6. **Customer-Zero STOP-check** — compare CZ BEFORE vs AFTER: same account/identity/runtime/RemoteApp/broker
   session/singleton/AppLocker/node, execution DARK, ASN #7/#8 AUTO_SHADOW, no order, no position mutation. **Any
   drift → STOP.**

Emit `SIGNED_HOST_EXECUTOR_HOST_CERTIFIED`, `G5_WORKSPACE_ACL_HOST_CERTIFIED`,
`HOSTED_SLOT_PROVISIONING_CERTIFIED` only on evidence. **Never** emit `AUTONOMOUS_ONBOARDING_CERTIFIED` (needs the
end-to-end fresh-user signup).

## Emergency stop / rollback

- **Dark it:** unset `HOSTED_HOST_EXECUTOR_ENABLED` (the executor → dark → no host contact) and `Stop-Service
  GuvFXHostedExecutor` (drains in-flight work first).
- **Clear the disposable slot:** run the reviewed rollback/remove modes only —
  `Set-GuvfxWorkspaceAcl -Mode Rollback`, `Set-GuvfxRemoteApp -Mode Remove`, `Set-GuvfxObserver -Mode Remove`,
  `Set-GuvfxAppLockerTenant -Mode Remove` — never a blanket delete, never against Customer Zero.
- **Remove the service:** `install_service.ps1` rollback path, or `GuvFXHostedExecutor.exe uninstall` (verified).

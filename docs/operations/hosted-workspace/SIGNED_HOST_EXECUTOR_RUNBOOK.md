# Signed Host Executor — architecture, operations & host-certification runbook (Stream 5)

**Status:** repository-complete, **DARK**, unarmed. See ADR-0037.
**Golden rule:** the shared prod agent (`100.79.101.19`, which also runs Customer Zero) must **never** be
failure-injected. Customer Zero (`account #1` / `guvfx_u_1` / `C:\GuvFX\accounts\1`) must never be provisioned.

## Architecture

```
Django prepare_hosted_slot
   → SignedHostExecutor            (host_executor.py)   confine + Customer-Zero refusal (layer 1); seal password
   → signed request                (host_protocol.py)   HMAC + nonce + skew/expiry + key_id; account_id only
   → HTTPS POST /hosted/provision  (Tailscale peer)
   → host dispatcher               (host_agent_dispatch.py)  verify + Customer-Zero refusal (layer 2);
                                                             DERIVE identity/paths from account_id; params allow-list
   → ONE reviewed primitive        (terminal_provisioning/windows/*.ps1)  fixed args, ParseFile-gated
   → signed response               (host_protocol.py)   backend verifies (ACL read-back is authenticated)
```
No command / script / path / username is expressible on the wire. The host derives everything from `account_id`.

## Operation allow-list (each → exactly one reviewed primitive)

| Operation | Primitive | Credentialed |
|---|---|---|
| PROVISION_IDENTITY | `Provision-GuvfxAccount.ps1` | yes (sealed Windows password) |
| APPLY_WORKSPACE_ACL / ROLLBACK_WORKSPACE_ACL | `Set-GuvfxWorkspaceAcl.ps1` | no |
| MATERIALISE_RUNTIME | `Populate-GuvfxViewerRuntime.ps1` (hosted variant) | no |
| APPLY_AUTOTRADING_CONFIG | `Set-GuvfxAutoTradingConfig.ps1` (capability only) | no |
| ENSURE_RDP_MEMBERSHIP | `Grant-GuvfxRdpAccess.ps1` | no |
| ENSURE_SINGLE_SESSION | `Set-GuvfxSingleSession.ps1` | no |
| ENSURE_REMOTEAPP | `Set-GuvfxRemoteApp.ps1` | no |
| PREPARE_OBSERVER | `Set-GuvfxObserver.ps1` | no |
| APPLY_APPLOCKER_AUDIT | `Set-GuvfxAppLocker.ps1 -Deploy` (AuditOnly) | no |
| VERIFY_SLOT | read-only re-verify | no |

## Authentication & secrets (by REFERENCE only — never values here)

- **HMAC keyring** — `HOSTED_EXECUTOR_KEYRING` (JSON `{key_id: secret}`), active `HOSTED_EXECUTOR_KEY_ID`. The
  host dispatcher holds the SAME keyring locally (RULE 3 — its own secret, distinct from `BETA_AGENT_KEYRING`).
- **Envelope keys** (Windows password sealing, ADR-0027) — backend holds the **public** key
  `HOSTED_EXECUTOR_ENC_PUBKEYS` + `HOSTED_EXECUTOR_ENC_KEY_ID`; the host holds the matching **private** key.
- **Endpoint** — `HOSTED_EXECUTOR_BASE_URL` (the host's Tailscale origin).
- All loaded from the deployment environment; never hard-coded, never logged. Rotation = add a new `key_id` to
  both keyrings (verify accepts any known key), cut over `HOSTED_EXECUTOR_KEY_ID`, then retire the old key from
  the inventory (`docs/SECRET_INVENTORY.md`).

## Deployment (Nuno / host-cert — RULE 1)

1. Stage the dispatcher + primitives on the host from Git (reviewable); **ParseFile-validate every `.ps1`** on
   the target Windows PowerShell before first run (RULE 9).
2. Run the dispatcher as a **supported service** (scheduled task / service) — never `Start-Process` over SSH.
3. Provision the keyring + envelope keys + base_url in the backend + host environments (by reference).
4. Do NOT set `HOSTED_HOST_EXECUTOR_ENABLED` until the disposable certification below passes.

## Disposable host certification (Phases 10–16 — Nuno-gated)

1. **Select a disposable slot** (Phase 10): a non-Customer-Zero `account_id` with no broker creds, no login, no
   trading history, no CZ relationship. Positively demonstrate disposability BEFORE mutation — capture Windows
   account / profile / runtime / RemoteApp / Guacamole absence, DB identity, slot, node, execution state.
2. **Pre-mutation safety** (Phase 11): fresh backup; rollback commands; host health; **capture the Customer-Zero
   BEFORE fingerprint** (PID/session, runtime checksum, RemoteApp, singleton, AppLocker, node); confirm execution
   DARK + ASN #7/#8 AUTO_SHADOW + zero execution authority.
3. **Run the real engine** (Phase 12): arm the flag for the disposable account only, run `prepare_hosted_slot`.
   Do NOT hand-repair failures (unless fixing the primitive) — certify the ENGINE. Verify: user exists, non-admin,
   Remote Desktop Users; workspace dir; **G5 ACL exact** (SYSTEM+Administrators+user, inheritance broken);
   golden runtime; AutoTrading keys present; RemoteApp exactly `terminal64 /portable`; observer task; single-session;
   AppLocker AuditOnly; DB state agrees with reality; no duplicate runtime; no broad ACL; no desktop publication.
4. **RULE 11 controls** (Phase 13): positive — the disposable identity can access ONLY its runtime; negative —
   another `guvfx_u_*` cannot read the slot; BUILTIN\Users / Everyone / Authenticated Users absent; reserved CZ
   refused; wrong username/path/account-id refused; duplicate op harmless; unknown op refused; arbitrary
   script/shell impossible.
5. **Re-run** (Phase 14): same request again → no duplicate user/profile/RemoteApp/task, no ACL drift, no
   regression, no CZ impact → emit `HOSTED_SLOT_PROVISIONING_IDEMPOTENCY_CERTIFIED`.
6. **Failure/rollback** (Phase 15): use a SAFE disposable-path failure injection ONLY (never on CZ). Prove fail
   closed + no false advance + observable reason + retry-after-correction + rollback. Skip if no safe seam.
7. **Customer-Zero STOP-check** (Phase 16): compare CZ BEFORE vs AFTER — same account/identity/runtime/RemoteApp/
   broker session/singleton/AppLocker/node, execution DARK, ASN #7/#8 AUTO_SHADOW, no order, no position mutation.
   **Any drift → STOP.**

## Failure handling & manual emergency recovery

- Any signed step that fails leaves canonical state at PROVISIONING (no false advance); ACL failure rolls back
  to the DACL snapshot; the workspace is re-driven idempotently.
- Emergency stop: unset `HOSTED_HOST_EXECUTOR_ENABLED` (executor → dark → no host contact) and stop the host
  dispatcher service. To clear a disposable slot, run the reviewed rollback/remove modes (`Set-GuvfxWorkspaceAcl
  -Rollback`, `Set-GuvfxRemoteApp -Remove`, `Set-GuvfxObserver -Remove`) — never a blanket delete.

## Multi-tenant safety (RESOLVED by Stream 6 / ADR-0038 — now safe on the shared host)

The two machine-global hazards Stream 5 flagged are closed:

- **AppLocker (`APPLY_APPLOCKER_AUDIT`)** now maps to the tenant applier `Set-GuvfxAppLockerTenant.ps1 -Mode Merge`
  (host `Set-AppLockerPolicy -Merge`), driven by the deterministic compiler `hosted_workspace.applocker_policy`.
  It ADDS only this account's SID-scoped deny fragment (account-tagged rule IDs) — it never replaces the
  machine-wide policy and never touches Customer Zero or another tenant. Rollback (`REMOVE_APPLOCKER_TENANT`)
  strips only this account's rules; removing Customer Zero is refused. The publisher-based posture is preserved
  (no writable-path executable Allow; MetaQuotes + Administrator recovery intact). The orchestrator keeps
  AppLocker DEFERRED (execution is DARK), but running it on the CZ host is now safe. The Phase 16 CZ STOP-check
  must still confirm CZ AppLocker is byte-identical.
- **RemoteApp (`ENSURE_REMOTEAPP`)** now publishes a deterministic **per-account** alias `guvfx_mt5_<id>` (Customer
  Zero keeps legacy `terminal64`), derived server-side from `account_id` via the single source
  `host_agent_dispatch.remoteapp_alias` — the delivery descriptor derives the SAME alias from `trading_account.id`,
  so multiple per-account aliases coexist without collision and no browser can choose the program. Rollback is
  `REMOVE_REMOTEAPP` (removes only this account's alias). Customer Zero's published `terminal64` is unchanged (its
  migration to the canonical `guvfx_mt5_1` alias is a separate later step).

## Markers

Emit only on evidence. Repository/logic scope (this PR): `SIGNED_HOST_EXECUTOR_CERTIFIED`,
`G5_WORKSPACE_ACL_CERTIFIED` (transport + ACL engine built, adversarially reviewed, fully tested — DARK).
Withheld until the live disposable host run: `HOSTED_SLOT_PROVISIONING_CERTIFIED`,
`HOSTED_SLOT_PROVISIONING_IDEMPOTENCY_CERTIFIED`. Never emit `AUTONOMOUS_ONBOARDING_CERTIFIED` yet (needs the
end-to-end fresh-user signup).

# Host Provisioning Engine — `prepare_hosted_slot` (Beta Readiness Stream 4)

**Status:** Phase 1 repository-complete, **DARK**, unarmed. See ADR-0036.
**Scope:** everything required to make a customer's Windows hosted slot exist **except broker login**.
**Non-goals (out of scope):** broker login, execution, AutoTrading, Telegram, identity reset, fullscreen, i18n.

This is the WS-A audit made durable: the manual steps to provision one hosted user, the automated replacement
the engine provides for each, the dependency order, and what is still manual/Sponsor-gated.

## Current manual steps → automated replacement

| # | Step (provision ONE hosted user) | Today | `prepare_hosted_slot` stage | Automatable |
|---|---|---|---|---|
| 0 | Set `TerminalNode.rdp_host` for the delivery host | Django admin / DB | preflight (reads; fails closed if unset — never auto-writes) | partial |
| 1 | Per-user DB identity record `guvfx_u_<id>` + runtime root + Fernet pw | `provision_terminal_account` | `ensure_identity_record` → `services.provision` | **exists** |
| 2 | Windows identity + folder tree (non-admin, asserts NOT Administrators) | manual SSH → `Provision-GuvfxAccount.ps1` | `materialise_identity_and_folders` (executor) | partial → engine |
| 3 | **Per-user NTFS ACL** (break inheritance; SYSTEM+Admins+user only) | **did not exist (G5 gap)** | `apply_workspace_acl` → **G5** `workspace_acl` + `Set-GuvfxWorkspaceAcl.ps1` | **new (this PR)** |
| 4 | Reconcile DB → PROVISIONED | `provision_terminal_account --mark-materialized` | `mark_materialised` (gated on host read-back, not operator word) | **exists** |
| 5 | Golden clean MT5 portable runtime into `runtime_root\terminal` | manual clean install | `populate_runtime` (executor) | partial |
| 6 | RDP grant (hard-scoped to `guvfx_u_*`) | manual → `Grant-GuvfxRdpAccess.ps1` | `grant_rdp` (executor) | partial → engine |
| 7 | Single-session (`fSingleSessionPerUser=1`) | manual → `Set-GuvfxSingleSession.ps1` | `enforce_single_session` (executor) | partial → engine |
| 8 | RD Session Host role + publish `terminal64` RemoteApp | **manual, not in repo** | `verify_remoteapp` (verification hook only) | **missing (Sponsor)** |
| 9 | AppLocker PREPARATION (AuditOnly) | manual → `Set-GuvfxAppLocker.ps1 -Deploy` | `applocker_prepare` (executor, **never -Enforce**) | partial → engine |
| 10 | Read-only observer + `GuvFX_Hosted*` tasks | **manual, not in repo** | `register_observer` (**deferred** until the host observe bridge) | **missing (increment)** |
| 11 | Advance `PROVISIONING → WAITING_FOR_LOGIN` | `allocate_workspace_node` | **gated** on `prepared=True` | **exists** |
| 12 | AppLocker AuditOnly → **Enforce** (execution enablement) | manual | **OUT OF SCOPE** (distinct Sponsor-gated op) | Sponsor |
| 13 | Customer logs their OWN broker into MT5 | customer | **EXCLUDED** (engine never receives a password) | n/a |

## Dependency order

```
node_rdp_host ─┐
identity_record ─▶ identity+folders ─▶ NTFS_ACL ─▶ mark_PROVISIONED
                          │                  └─▶ golden_runtime ─▶ remoteapp_verify ─▶ WAITING_FOR_LOGIN
                          ├─▶ rdp_grant ─▶ single_session                    ▲
                          └─▶ applocker_auditonly ──────────────────────────┘
   golden_runtime ─▶ observer(deferred)
   applocker_auditonly ─▶ (Sponsor) applocker_enforce ─▶ broker_login
```
The NTFS ACL precedes golden runtime deliberately: `accounts.dat`/config must be populated into a tree already
scoped to SYSTEM+Administrators+user, never under an inherited `BUILTIN\Users`-readable DACL.

## HostExecutor contract (the DARK seam)

`resolve_host_executor()` returns **`None`** in the repository-only phase → every host step fails closed. A real
signed executor (a future host-cert increment) implements, each returning `{"ok": bool, ...}` and receiving only
`(fixed slot identity, fixed runtime_root, rdp_host)`:

- `materialise_identity(spec, rdp_host=)` — runs `Provision-GuvfxAccount.ps1` (`spec` carries the Windows
  password over a secure channel; it is NEVER logged or returned).
- `apply_workspace_acl(plan, rdp_host=)` → `{"ok","rows","user_sid","protected"}` — runs
  `Set-GuvfxWorkspaceAcl.ps1 -Apply`; `rows`/`user_sid`/`protected` feed `verify_workspace_acl` (authoritative).
- `populate_runtime`, `grant_rdp`, `enforce_single_session`, `verify_remoteapp`, `applocker_prepare` — the
  remaining fixed-slot host actions. `register_observer` is optional (deferred).

The executor MUST run privileged host provisioning via a **supported service/task mechanism, never an
interactive SSH `Start-Process`** (RULE 1), and MUST `ParseFile()`-validate every script before first run
(RULE 9). Any host text parse (RemoteApp alias, policy) MUST carry a RULE-11 positive control.

## Remaining manual / Sponsor-gated (after this PR)

1. **Signed host-executor transport** — the missing automated host-reach for the per-user path (build + cert).
2. **RemoteApp publication** (RD Session Host role + publish/collection-bind) — no repo publisher exists; the
   engine can only *verify* exactly-one `terminal64` alias in Phase 1.
3. **Host observe bridge** — replace `_dark_observe_fn`/`register_observer` with a real observer + the three
   `GuvFX_Hosted*` tasks; until then autonomous state cannot advance past `WAITING_FOR_LOGIN`.
4. **AppLocker multi-tenant accumulation** (`-Merge`/multi-SID) — a 2nd hosted user currently replaces the 1st's
   policy (WP6B). Preparing >1 user on one host needs this; policy redesign is explicitly out of Phase 1.
5. **Golden clean-image commissioning + pinning** (RULE 10) — dedicated clean MT5 install, markers, validation.
6. **AppLocker AuditOnly → Enforce** — execution enablement, host-global, after a clean 0-event-8003 review.
7. **On-host certification + Sponsor arming** — `HOSTED_SLOT_PREP_ENABLED` produces no host effect until a real
   executor exists and the slot lifecycle is certified on a disposable host. The shared prod agent
   (`100.79.101.19`) must never be used to failure-inject.
8. **ASCII-harden the two non-chain scripts** (`Set-GuvfxKioskShell.ps1`, `Cleanup-GuvfxSessions.ps1`) — RULE 9
   follow-up; not composed by `prepare_hosted_slot` today.

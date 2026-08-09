# Workspace Delivery — Host Change Packet & Certification Runbook (ADR-0034)

**Status:** DARK. The repository subsystem (RemoteApp descriptor, owner-authorised delivery service,
delivery state model/writer, read model, DARK read-only API, telemetry, tests, adversarial review) is
**complete and merged behind two OFF flags**. This document is the dedicated **Host Change packet** — the
Windows-host work that is a genuine Sponsor/production-host boundary and is **NOT begun** in the repository
phase. It is written so the host change can be reviewed and authorised as one unit.

> **Do not begin any step in this document without explicit Sponsor authorisation.** Every step below
> changes the Windows host (installs a server role, publishes a RemoteApp, changes a licence mode, or sets
> a machine policy). None of it is required for, or performed by, the repository subsystem.

---

## 0. Why this is a STOP, not more repository work

The repository seam generates the *exact* Guacamole RDP RemoteApp payload a future host will consume. It
cannot make that payload **functional**, and making it functional is out of repository scope:

| # | Host change | Why it is a Sponsor/host boundary | Rule binding |
|---|-------------|-----------------------------------|--------------|
| H1 | Install **RD Session Host** role (± Connection Broker) | Adds a Windows *server role*; RemoteApp publishing does not exist without it. Currently `terminal_provisioning/delivery.py` and `viewer.py` both record `remote_app(_capable): False` — "RDS not installed". | PART X |
| H2 | Publish **`terminal64.exe` as a RemoteApp** on a collection, per the non-admin identity | The `||terminal64` alias resolves only against the host's RemoteApp allow-list. | PART S |
| H3 | Resolve **SPLA / RDS-CAL licensing mode** | Multi-user RDS access is a Microsoft licensing decision; cannot be purchased or waived by engineering. | PART S |
| H4 | Host **AppLocker / SRP** single-app lockdown | A machine/GPO-level policy that confines the session to MT5 only. | PART X |
| H5 | **TX-1 NTFS-ACL** on the per-user runtime tree | `Provision-GuvfxAccount.ps1` creates `C:\GuvFX\accounts\<id>` with `New-Item` and **no ACL step** (grep of TX-1 scripts for `icacls\|Set-Acl\|FileSystemAccessRule` is empty) — the tree is not yet locked to `guvfx_u_<id>`. | PART U / RULE 11 |

**Host isolation:** all of the below is performed on a **disposable certification host**, never on the
shared production MT5 box (`100.79.101.19`) and never against the shared trade bridge (`:8788`) or the
shared validation agent. The golden runtime originates from a **dedicated clean install** (RULE 10 — never
promote a production terminal).

---

## 1. Pre-flight (read-only, no host change)

1. Confirm both flags are OFF in the target environment: `HOSTED_PERSISTENT_MT5_ENABLED`,
   `HOSTED_MT5_REMOTEAPP_ENABLED`. The delivery API must 404 and `authorize_workspace_delivery` must return
   `DA_SUBSYSTEM_DISABLED` with zero DB queries.
2. Confirm `GUAC_BASE_URL` and `GUAC_JSON_SECRET_KEY_HEX` are present (delivery needs them; their absence
   is the `DA_GUAC_UNCONFIGURED` fail-closed path, not an error).
3. Capture a baseline of the disposable host: OS build, installed roles (`Get-WindowsFeature`), existing
   RemoteApp collections (expected: none), and the current ACL of `C:\GuvFX\accounts\<id>` via `icacls`
   (**machine-readable**; RULE 11 — verify raw output, test a positive control).
4. Verify the golden manifest pin (`.guvfx_golden_manifest`) and that the proposed golden image shows no
   broker account configured / no runtime state (RULE 10).

## 2. H5 — NTFS-ACL on the per-user runtime (do this first; smallest blast radius)

The per-user runtime must be readable/traversable only by `guvfx_u_<id>` (and SYSTEM/Administrators).

- New/edited PowerShell artefact (e.g. `Set-GuvfxRuntimeAcl.ps1`): **ASCII-only**, validated with
  `[System.Management.Automation.Language.Parser]::ParseFile()` on the target host BEFORE first execution
  (RULE 9). No em-dashes, no smart quotes.
- Grant model: remove inherited broad ACEs; grant `guvfx_u_<id>` the minimum on `C:\GuvFX\accounts\<id>`;
  keep SYSTEM + Administrators. Never grant other `guvfx_u_*` identities.
- **Read-back both directions** (RULE 11): assert the target ACE IS present (positive control) AND that a
  known-absent identity is NOT present (negative control). Record raw `icacls` output.
- Reversible: capture the pre-change ACL; the rollback restores it verbatim.

## 3. H1 — Install RD Session Host (disposable host only)

- Install via the supported mechanism only (Server Manager / `Install-WindowsFeature RDS-RD-Server`), never
  an interactive-SSH `Start-Process` (RULE 1 — a session-bound process dies with the session).
- Reboot as required; re-capture `Get-WindowsFeature` evidence.
- **Blast radius:** this converts the host into an RDS host. On the disposable host this is acceptable and
  reversible (uninstall the role). It must **never** run on the production MT5 box.

## 4. H3 — Licensing mode (Sponsor decision, recorded before H2)

- Decide and record the licensing posture for the technical test window (RDS per-user/per-device CAL grace,
  or SPLA). Engineering does not purchase; the Sponsor authorises the mode. Record the decision + expiry.
- If the licensing decision is not available, **stop here** — the host is left with H5 done, H1 optionally
  done, and no RemoteApp published. That is a clean partial state.

## 5. H2 — Publish MT5 as a RemoteApp

- Create a session collection; publish **only** `terminal64.exe` (path
  `C:\GuvFX\accounts\<id>\terminal\terminal64.exe`, args `/portable`) as a RemoteApp aliased so the
  Guacamole `remote-app=||terminal64` resolves. No other app on the allow-list.
- Bind the collection to the non-admin identity `guvfx_u_<id>` only.
- Evidence: the published-RemoteApp list (exactly one entry), and a `remote-app` connection test through
  Guacamole using a descriptor minted by `authorize_workspace_delivery` (flags temporarily ON in the
  disposable environment ONLY).

## 6. H4 — AppLocker / SRP single-app lockdown

- Author the machine policy confining the RDS session to `terminal64.exe` (deny cmd/powershell/explorer as
  a shell escape). Any PowerShell tooling is ASCII-only + AST-validated (RULE 9).
- Verify: from inside the delivered RemoteApp, attempting to launch a second app is denied.

## 7. Certification checklist (disposable host, flags ON in that env only)

- [ ] `authorize_workspace_delivery(owner, ws)` returns `DA_OK` with a signed `embed_url`.
- [ ] The RemoteApp opens **MT5 only** as a seamless window (no desktop, no Start menu, no Explorer).
- [ ] Disconnect → reconnect lands on the **same persistent Windows session** (stable `mt5-workspace-<uuid>`
      conn id + stable `guvfx_u_<id>` identity); `record_remoteapp_disconnected` retained the session.
- [ ] Drive/clipboard/printer redirection all denied (descriptor asserts this; verify on the wire).
- [ ] A second user's descriptor for this workspace is refused (`DA_NOT_OWNER`) — owner binding holds live.
- [ ] **Wrong-workspace attempt:** the owner requesting a foreign / random UUID is refused
      (`DA_NOT_OWNER` / `DA_WORKSPACE_MISSING`) with NO descriptor minted — delivery is keyed on the
      unguessable `workspace_uuid`, never an enumerable id.
- [ ] **Concurrent opens:** two simultaneous browser opens for the same workspace converge on the SAME
      persistent Windows session (stable conn id) — no parallel independent environment is created.
- [ ] AppLocker denies any non-MT5 app inside the session.
- [ ] NTFS-ACL: `guvfx_u_<id>` can read its own runtime; a different `guvfx_u_*` cannot (read-back proof).
- [ ] No credential in any log, telemetry row, API response, or the returned descriptor.
- [ ] **Production blast-radius baseline:** prod bridge PIDs / ports (`:8788`/`:8791`) / `GuvFXBetaAgent`
      captured BEFORE and proven UNCHANGED AFTER; only the disposable host + disposable workspace are touched.

## 8. Rollback

Each host change is individually reversible and captured before mutation: restore the pre-change NTFS ACL;
unpublish the RemoteApp / remove the collection; uninstall the RDS role; revert the AppLocker policy; set
both flags OFF. The repository subsystem returns to fully DARK with no residue.

## 9. Explicit boundary

This packet is **authorisation-required** end to end. The repository phase is done; nothing here is
started. Present this document to the Sponsor as the single host-change decision for Workspace Delivery.

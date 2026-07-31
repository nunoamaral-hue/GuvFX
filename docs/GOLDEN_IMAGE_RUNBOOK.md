# Golden Image Runbook — Trusted Beta baseline MT5

**Purpose.** The single, repeatable, **version-agnostic** procedure for creating, validating, pinning,
verifying, archiving, rolling back, recovering and refreshing the beta **golden** MT5 image — the dedicated
immutable source that `MATERIALISE` copies into every slot. All engineering knowledge lives here; the Sponsor
executes only the one interactive install step where licensing/installer interactivity prevents automation, and
**future rebuilds require no undocumented engineering knowledge.**

**Version policy (Sponsor decision (a), 2026-07-30).** The runbook does **not** hard-code an MT5 build. You
install from the official source, **record whatever build the installer produces**, validate it, digest it,
archive it, and promote it. **That recorded build becomes the canonical Golden version** and the immutable
Trusted-Beta baseline **until the next intentionally authorised Golden refresh** (see the Refresh Procedure).

---

## Golden Integrity Rules  *(the invariants; a violation invalidates the Golden)*

**Governing objective (Sponsor decision, 2026-07-30): the Golden must never become OPERATIONALLY USED.** The
concern is preserving an operationally pristine *tree*, not preventing every possible process launch. What
disqualifies a Golden is **operational state written into the tree** (or an in-place change to it), not the bare
fact that a process momentarily started and wrote its runtime state elsewhere (e.g. to `%APPDATA%`). Concretely,
the Golden must **NEVER**:

1. **Become operationally used** — the tree must carry **no** operational-use artefact: no saved broker account
   (`config\accounts.dat`), no settings-written-on-exit (`common.ini`/`terminal.ini`), no data-folder redirect
   (`origin.txt`), no compiled-EA cache (`MQL5\experts.dat`), no broker-named `bases\` directory, no logs.
   *(Installer-shipped reference data is NOT operational use — e.g. `config\servers.dat`, a public broker-server
   list the installer ships from build 5.0.0.6073, is written before any launch and is accepted; see the
   validator note.)*
2. **Be logged into** — no broker account, credentials, or server login are ever entered against it.
3. **Be updated in place** — LiveUpdate is disabled; a new build is a *refresh* (fresh install → new archive →
   new pin), never an in-place update of the existing Golden.
4. **Be used by Strategy Tester** — the Golden's `metatester64.exe` is never launched or registered as a Strategy
   Tester Agent; strategy/back-testing uses a **separate** install.
5. **Be referenced by runtime infrastructure** — no service, scheduled task, firewall rule, or (long-lived)
   process references the Golden path, **except** the beta agent's read-only `MATERIALISE` copy.

*Why these are load-bearing:* on 2026-07-27 the Golden was interactively launched → MT5 **LiveUpdated its tester
binaries in place** → it became the registered **Strategy Tester Agent** → its **digest drifted** off the pin.
The disqualifier was the in-place update + tester use (rules 3–4) that mutated the tree — **not** the bare launch.
Verification (Procedure 4) asserts these as an **operationally-pristine proof** (no operational-use artefact in
the tree; no long-lived process/firewall reference; digest matches the pin).

---

## Roles

- **Sponsor (once per build):** Procedure 1 steps 1–3 — the interactive MetaQuotes/IS6 install (licensing +
  interactive installer). Nothing else requires the Sponsor.
- **GuvFX/Claude:** Procedures 2–7 — validation, digest, verification, archive, promotion ACLs, rollback,
  recovery, refresh — all scripted/documented and reproducible.

## Permanent SOP inputs (canonical — do not rely on memory)
- **Approved MT5 installer source:** the **official MetaTrader 5 installer from MetaQuotes** —
  `https://www.metatrader5.com/en/download` (direct stub
  `https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe`). The Golden is broker-login-free
  and must connect to any broker at the per-slot login stage, so it uses an **open MetaQuotes build, never a
  broker-locked white-label build**. (Sponsor-ratified source; the version is whatever the installer produces —
  version-agnostic.)
- **Approved host access:** interactive GUI via RDP to `100.79.101.19:3389` (Tailscale, Administrator) or
  Guacamole `guac.guvfx.com/guacamole/`; engineering CLI via `ssh administrator@100.79.101.19` (Tailscale,
  key-based). Credentials/keys live in the **secret store**, never in this runbook.

---

## The versioned build artifact — archive the tree, not the installer

**Archive the validated clean install *tree*, never the web installer.**

- The IS6/MetaQuotes installer is a **web/stub** installer: at run time it **downloads the latest build**.
  Re-running it later yields whatever is current then — it pins nothing (this is the very mechanism that drifts a
  Golden). **Do not archive the installer** as the baseline.
- The **reproducible artifact** is the **validated, digest-pinned Golden tree**, captured once as an immutable
  archive. Rebuild = **restore + verify digest**, byte-identical, **no re-install → no re-drift.**

**Archive contents (the immutable Trusted-Beta baseline):**
1. the **validated Golden tree** (compressed, e.g. `golden-mt5-<build>-<digest12>.zip`);
2. the **tree digest** (`BETA_AGENT_GOLDEN_DIGEST`);
3. the **manifest version** (`BETA_AGENT_GOLDEN_MANIFEST_VERSION` = the recorded build);
4. the **archive's SHA-256**;
5. the **build number** (recorded, not pre-chosen).

**Storage (data rule — no large/binary artefacts in Git):** the archive lives **outside Git** on the NAS/DR
store (Synology Tailscale peer; `C:\_dr_backup`). **Git holds the small text identity** — digest, manifest
version, build number, archive location + the archive's SHA-256. Git holds the *pin*; the store holds the
*bytes*. **Licensing:** internal DR archival of your own licensed install (no redistribution) is normally fine —
confirm against the IS6/MetaQuotes licence before archiving.

**Manual archive handoff — approved fallback (adopted 2026-07-30).** When the archive destination (the permanent
NAS/DR store) requires credentials that are **intentionally not provisioned on the production host** (least
privilege), do **NOT** provision NAS credentials onto the host and do **NOT** substitute another archive
location. Instead: (1) create the archive + a metadata sidecar in a **transient** host handoff dir
(`C:\GuvFX\golden\_handoff\`); (2) record the archive SHA-256, size, MT5 build, canonical digest, manifest
version, file count; (3) hand the archive + metadata to the Sponsor, who places them into the permanent NAS/DR
location **manually**; (4) the handoff dir is transient (not an archive location) and is cleared after the
Sponsor confirms the NAS copy. The NAS/DR store remains the sole authoritative archive location; the host copy
is a delivery artefact only.

---

## Procedure 1 — Golden Creation  *(Sponsor executes steps 1–3 once)*

1. On an **isolated staging path** — NOT under the estate, NOT the live Golden path
   (e.g. `C:\GuvFX\golden\stage-<UTC>\`) — run the IS6/MetaQuotes MT5 installer into a **new empty folder**.
2. **Do NOT launch the terminal. Do NOT log into any broker. Do NOT attach any EA.** (Launching triggers
   LiveUpdate and writes runtime state — both violate the Integrity Rules.)
3. **Disable LiveUpdate** for that install; leave it un-launched.
4. *(GuvFX)* **Record the build:** `(Get-Item <stage>\terminal64.exe).VersionInfo.FileVersion`. This recorded
   value is the canonical Golden version — it is not chosen in advance.
5. *(GuvFX)* Create the two markers — **one file each**, write nothing else:
   - `.guvfx_golden_manifest` containing the exact recorded build string;
   - `.guvfx_portable` — empty file.
6. *(GuvFX)* Confirm a fresh never-run install is clean: **absent** `config\accounts.dat`, `config\servers.dat`,
   `config\common.ini`, `config\terminal.ini`, `origin.txt`, `MQL5\experts.dat`; `bases\` contains only
   `Default` (a fresh install ships the `Default` tree — expected, not "used"); `MQL5\` absent at root is normal
   (non-portable; `/portable` creates it in the slot at first run).

## Procedure 2 — Validation  *(read-only, RULE 10)*

```
install_pool.ps1 -ValidateGoldenOnly -GoldenDir <stage path>
```
Read-only by construction. Expected — the 8 `ok` lines: `terminal64.exe present` · `no MQL5 (non-portable…)` ·
`marker: .guvfx_golden_manifest present` · `marker: .guvfx_portable present` · `version: terminal64.exe <build>
matches the pinned build` · `clean: bases\ holds only the shipped Default tree` · `provenance: N scanned file(s)
contain no path from another runtime or user profile` · `golden image validated`.

**The load-bearing check is provenance** — it reads file **contents** for foreign absolute paths
(`C:\GuvFX\terminals\…`, `C:\Users\…`), catching a tree copied from a per-account runtime (this caught the
earlier `5833` tree: 66 foreign paths in `MQL5\experts.dat`). Must be **0 foreign paths**. **Any failure aborts
before PLAN and is never waived.**

## Procedure 3 — Digest Generation

Canonical algorithm — reproduced byte-for-byte by both `install_pool.ps1` and `win_slot_ops.tree_digest`
(`win_slot_ops.py:191-203`), so agent and installer always agree:
- per file: `line = "<relpath>|<size_bytes>|<sha256_hex_lower>\n"`, `<relpath>` normalised = `replace('/','\')`,
  strip trailing `\`, **lowercased**;
- sort lines by normalised relpath (**ordinal**);
- digest = `SHA-256(UTF-8 concatenation of all lines)`.

Produce + read it (non-mutating): `install_pool.ps1 -VerifyOnly -GoldenDir <path>` prints
`golden: <N> files, tree digest <hex>`. **Record into config:** `BETA_AGENT_GOLDEN_DIGEST = <hex>`,
`BETA_AGENT_GOLDEN_MANIFEST_VERSION = <recorded build>`. Installer reminder: *"a difference means the image
changed — STOP."*

## Procedure 4 — Verification  *(post-promotion, read-only)*

After the validated tree is at the Golden path and `install_pool.ps1 -Apply` has set Golden ACLs (inheritance
broken; only Administrators + SYSTEM Full; each `guvfx_b_slot1..4` and the service SID ReadAndExecute-only; **no
write-class ACE for any non-admin principal**):
```
install_pool.ps1 -VerifyOnly -GoldenDir <golden path>
```
Assert: digest **==** the recorded pin · Golden ACL as above (`AreAccessRulesProtected=True`). Plus the
**operationally-pristine proof** (the Integrity Rules): **no operational-use artefact in the tree**
(`config\accounts.dat`, `common.ini`, `terminal.ini`, `origin.txt`, `MQL5\experts.dat`, a broker-named `bases\`
dir, logs) · no long-lived process runs from the Golden path · no firewall rule references any `<golden>\*.exe`.
*(Installer-shipped `config\servers.dat` is accepted, not a violation.)* Only the observed digest match **and**
operationally-pristine proof mark the Golden ready for `MATERIALISE`.

## Procedure 5 — Rollback  *(pre/post promotion)*

- **Before** promoting, **rename** the current Golden dir (`<golden>` → `<golden>.bak-<UTC>`) rather than delete;
  keep the prior config pin and its archive.
- **To roll back:** restore the renamed dir → revert `BETA_AGENT_GOLDEN_DIGEST` + `..._MANIFEST_VERSION` to the
  prior values → Procedure 4. The agent reads the Golden read-only, so rollback is a directory swap + config
  revert; **no estate impact**, no slot data touched.

## Procedure 6 — Recovery  *(reproducible rebuild from the archive)*

- **From the archived artifact (authoritative, byte-identical):** restore `golden-mt5-<build>-<digest12>.zip`
  from the DR/NAS store (verify the archive's SHA-256 first) to the Golden path → Procedure 3 to reproduce the
  digest → confirm `== BETA_AGENT_GOLDEN_DIGEST` → apply Golden ACLs (`install_pool.ps1 -Apply`) → Procedure 4.
  **No re-install → no re-drift.** This is the standard future rebuild path.
- **Only if the archive is lost:** perform a **Golden Refresh** (below) — a fresh install yields a new build and
  a new pin.

## Golden Refresh Procedure  *(intentional, authorised MT5 upgrades — never ad hoc)*

A refresh replaces the canonical Golden with a newer validated build. It is a **Sponsor-authorised event**
(triggered by a build EOL, a security/broker requirement, or a programme decision) and follows the **same
lifecycle** — never by launching or updating the existing Golden in place.

1. **Sponsor authorises** the refresh and records the reason.
2. **Create** a new Golden from a fresh official install at a new staging path (Procedure 1) — the new build is
   recorded, not chosen.
3. **Validate → Digest → Archive** the new tree (Procedures 2, 3, and the archive-contents list).
4. **Retain** the current Golden + its archive (Procedure 5 rename) for rollback.
5. **Promote** the new tree to the Golden path; **re-pin** `BETA_AGENT_GOLDEN_DIGEST` +
   `..._MANIFEST_VERSION`; apply Golden ACLs.
6. **Verify** (Procedure 4): digest match + Golden ACL + inert proof.
7. **Co-deploy:** because the agent's stage-copy pre-check compares the configured digest, re-stage/restart the
   agent as needed so the running agent's expected digest matches the new pin. Existing slots continue on their
   already-materialised copy until re-provisioned.
8. **Record** the refresh in `evidence/beta-agent-phase3-cert/` (old→new build, old→new digest, reason,
   archives). The superseded Golden's archive is kept (never deleted) for audit + rollback.

---

## Evidence to capture on every Golden build/refresh  *(append to `evidence/beta-agent-phase3-cert/`)*
Recorded build · file count · `-ValidateGoldenOnly` output (8 ok lines, 0 foreign paths) · tree digest ·
`-VerifyOnly` digest-match + Golden ACL read-back · inert proof (no process/firewall/launch) · archive location
+ archive SHA-256. A Golden is "ready for `MATERIALISE`" only when all are captured.

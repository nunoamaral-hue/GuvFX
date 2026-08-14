# STREAM 10E — Hosted Workspace W^X Host Behavioural Certification Runbook

> **Authority / scope.** Authoritative repository deliverable for the on-host behavioural certification that emits
> `REMOTEAPP_ISOLATION_CERTIFIED` (ADR-0041 / ADR-0043). Authored DARK — **no host was contacted to produce it.**
> The certification is executed on a **separate disposable host**, never the Customer Zero production host
> (Sponsor decision, 2026-08-14). This runbook is the numbered procedure; the PowerShell payloads it drives live in
> `backend/terminal_provisioning/windows/` and `.../windows/escape_battery/`.

> **Two environments (do not conflate).**
> - **Certification environment** = a **disposable Windows host** that mirrors the production configuration. The
>   full escape battery, AuditOnly→Enforce, and all failure-injection happen **here**.
> - **Deployment environment** = the Customer Zero production host `100.79.101.19`. It is touched **only** after the
>   disposable host achieves a genuine `REMOTEAPP_ISOLATION_CERTIFIED`, and then only via the CZ before/after
>   procedure (§9). The escape battery is **never** run against CZ.

> **Standing rules honoured.** RULE 9 (ASCII-only PowerShell; `ParseFile()` before first execution); RULE 11
> (positive + negative controls before trusting a negative; verify raw bytes / machine-readable output); RULE 1
> (never start a long-running production service from an interactive SSH session); never failure-inject the shared
> prod host; no secrets in logs; `HOSTED_WX_ISOLATION_ENABLED` and `REMOTEAPP_ISOLATION_CERTIFIED` stay OFF/WITHHELD
> until the criteria in §10 are actually met.

---

## 0. Disposable certification host — provisioning spec (mirror of production)

The disposable host must match the production isolation-relevant configuration so a pass here is meaningful:

| Item | Requirement |
|------|-------------|
| OS / role | Same Windows Server build + RDS Session Host role as production |
| Golden MT5 | The **pinned golden** runtime (`.guvfx_golden_manifest`), vetted-empty (RULE 10) — a **clean install**, never a promoted production terminal |
| Tenant identity | A disposable `guvfx_u_<N>` non-admin local user (pick an N unused in prod, e.g. 90) + its runtime tree `C:\GuvFX\accounts\<N>` |
| G5v2 NTFS ACL | Applied via `Set-GuvfxWorkspaceAclV2.ps1 -Mode Apply` (root RX; Modify only on data subdirs; Deny-write on code dirs + `common.ini`) |
| Base AppLocker | `guvfx-hosted-{auditonly,enforce}.xml` deployed via `Set-GuvfxAppLocker.ps1` (deny-by-default allow model, ADR-0042 + the MetaEditor `BinaryName` pin) |
| Per-tenant W^X | The Exe `Deny(*)` fragment (`tenant_wx_deny_fragment`) + the **soak-derived** Dll `Deny(*)` fragment (§5) via `Set-GuvfxAppLockerTenant.ps1 -Mode MergeWx` |
| `AllowDllImport` | `config\common.ini [Experts] AllowDllImport=0`, made tenant-immutable by the G5v2 `common.ini` Deny-write |
| RemoteApp | `terminal64.exe` published RemoteApp-only for the tenant (no desktop shell) |
| Broker demo account | A **disposable demo** account (operator-supplied) for the MT5 positive controls — see §11 |

> The certified DARK repository artefacts are deployed to the disposable host unchanged; the host session is the
> only thing new.

---

## 1. Pre-flight validation (RULE 9 gate) — do FIRST, before any execution

1.1 On the disposable host, **`ParseFile()`-validate every PowerShell artefact** before it is ever run (RULE 9 —
source review is not a substitute for the real parser). Any parse error aborts before PLAN:

```powershell
$ps = @(
  'Set-GuvfxWorkspaceAclV2.ps1','Set-GuvfxAppLockerTenant.ps1','Set-GuvfxAppLocker.ps1','Test-GuvfxGoldenMql.ps1',
  'escape_battery\Invoke-GuvfxEscapeBattery.ps1','escape_battery\Get-GuvfxCertEvidence.ps1',
  'escape_battery\Get-GuvfxIsolationFingerprint.ps1'
)
$base = 'C:\GuvFX\_stage\terminal_provisioning\windows'
$fail = @()
foreach ($f in $ps) { $t=$null; $e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile((Join-Path $base $f),[ref]$t,[ref]$e); if ($e -and $e.Count){ $fail += "$f : $($e[0].Message)" } }
if ($fail.Count) { $fail; throw 'PARSEFILE_FAILED' } else { 'parsefile_ok' }
```

1.2 Confirm the AppLocker audit channels are enabled + readable (RULE 11 mechanism control):
`Get-WinEvent -ListLog 'Microsoft-Windows-AppLocker/EXE and DLL','Microsoft-Windows-AppLocker/MSI and Script'`
must show `IsEnabled = True`. Enable + confirm records exist before trusting any "0 blocks".

1.3 Confirm `AppIDSvc` (Application Identity) is Running (start type is set via the registry key
`HKLM\SYSTEM\CurrentControlSet\Services\AppIDSvc\Start`; it is a protected service — see APPLOCKER_HARDENING.md).

---

## 2. Reference fingerprint (disposable host) + Customer Zero BEFORE fingerprint

2.1 Capture the disposable host's intended-state fingerprint (reference):
`Get-GuvfxIsolationFingerprint.ps1 -RuntimeRoot C:\GuvFX\accounts\90 -SessionUser guvfx_u_90 -Label reference -OutFile C:\GuvFX\_cert\ref.json`

2.2 **Customer Zero BEFORE fingerprint** — captured on the PRODUCTION host, **read-only**, as the immutable baseline
the eventual roll-out (§9) must match. Capturing a fingerprint performs no mutation:
`Get-GuvfxIsolationFingerprint.ps1 -RuntimeRoot C:\GuvFX\accounts\1 -SessionUser guvfx_u_1 -Label before -OutFile C:\GuvFX\_cert\cz_before.json`
Store `cz_before.json` (and its SHA256) as evidence. **Do not proceed to any CZ mutation in this packet.**

---

## 3. Soak under AuditOnly — capture the real workload + writable-dir ground truth

3.1 Deploy the base policy in **AuditOnly** (`Set-GuvfxAppLocker.ps1 -Mode Deploy -StateDir C:\GuvFX\_applocker_10e`)
and merge the tenant Exe W^X `Deny(*)` (`Set-GuvfxAppLockerTenant.ps1 -Mode MergeWx -FragmentPath <exe_wx.xml>`).

3.2 **Operator step (§11):** in the tenant RemoteApp session, log MT5 into the **disposable demo account** and use it
normally for several minutes (charts, Market Watch, menus/dialogs, reconnect). This generates the real allow surface
(`8002`) and any `8003` would-block for the certified customer journey.

3.3 Enumerate the host's **user-writable OS-DLL subdirs** — the ground-truth exclusion set for the Dll exceptions
(RULE 11; this is why the Dll exception set is soak-derived, not guessed):
`accesschk.exe -w -u Users C:\Windows C:\Windows\System32` (and SysWOW64). Record every writable subdir.

3.4 Review every `8003`: classify **required** (add a minimal allow rule + re-review) vs **unexpected** (confirms
confinement). Enforce only when legitimate would-be-blocks for the certified journey = **zero**.

---

## 4. Golden vetted-empty gate

`Test-GuvfxGoldenMql.ps1 -RuntimeRoot C:\GuvFX\accounts\90` must return `ok=true, reason=vetted_empty,
positive_control=passed`. A non-`vetted_empty` result, or `positive_control != passed`, **aborts** (the golden could
seed a native-import EA). This is fail-closed by construction (missing code dir, unenumerable subtree, or a failed
runtime positive control all → non-clean).

---

## 5. Build + apply the per-tenant Dll W^X Deny (reducible-half closure — HARD precondition)

5.1 From the §3.3 soak, construct the **non-writable RX DLL-load exception set**: the OS DLL directories MINUS every
user-writable subdir found (`%WINDIR%\WinSxS\*`, the specific non-writable `%SYSTEM32%` set, `%PROGRAMFILES%\*`,
and the specific **non-writable** terminal RX code dirs, e.g. `...\TERMINAL\MQL5\Libraries\*`). **Never** include a
bare `\*`, a tenant-writable dir, or a wildcard that *covers* one (`...\TERMINAL\*` is rejected).

5.2 Produce the fragment from the backend single source of truth (on a workstation, DARK): call
`applocker_policy.tenant_wx_dll_deny_fragment(<N>, <sid>, <soak_exceptions>)`. It fail-closes on empty / wildcard /
covers-writable exceptions. Validate with `assert_wx_dll_deny_invariants(...)`.

5.3 Apply it: `Set-GuvfxAppLockerTenant.ps1 -Mode MergeWx -FragmentPath <dll_wx.xml>` (the applier accepts an Exe or
Dll W^X fragment and validates it is a single tenant-SID `Deny(*)` with a non-empty, non-`*` exception set).

> The Dll W^X Deny closing the signed-DLL-from-writable case is a **hard precondition** for cert (ADR-0043) — it
> must be **applied** and shown blocking (§6 case `signed_dll_comhijack_from_writable`), never rubber-stamped.

---

## 6. Enforce + the complete 8004 escape battery

6.1 Switch to **Enforce** (`Set-GuvfxAppLocker.ps1 -Mode Deploy -Enforce -StateDir C:\GuvFX\_applocker_10e`).
Immediately verify the positive controls: the tenant MT5 still works (§11), Administrator recovery intact.

6.2 **Tenant-context attempts** — in the tenant session run:
`Invoke-GuvfxEscapeBattery.ps1 -AccountId 90 -HostedUser guvfx_u_90 -RuntimeRoot C:\GuvFX\accounts\90
 -GoldenTerminal64 C:\GuvFX\accounts\90\terminal\terminal64.exe -WorkDir C:\GuvFX\accounts\90\terminal\MQL5\Files\_escape
 -OutFile C:\GuvFX\_cert\tenant_attempts.json`

6.3 **Admin-context authoritative verdict** (run DETACHED — `Get-WinEvent` can hang over SSH-stdin):
`Get-GuvfxCertEvidence.ps1 -AccountId 90 -HostedUser guvfx_u_90 -RuntimeRoot C:\GuvFX\accounts\90 -Mode Enforce
 -SinceMinutes 30 -TenantAttemptsJson C:\GuvFX\_cert\tenant_attempts.json -EvidenceDir C:\GuvFX\_cert\evidence`

### The battery — every case, and its expected result (do NOT shorten)

| # | Case | Attack | MUST show (Enforce) | Control |
|---|------|--------|---------------------|---------|
| 1 | `portable_copy_v5` | copy signed `terminal64` + `/portable` `AllowDllImport=1` + `#import` EA to a writable dir, launch | **Exe 8004 block** on the copy | Exe W^X `Deny(*)` |
| 2 | `metaeditor` | launch `metaeditor64.exe` (MetaQuotes-signed) | **Exe 8004 block** (or absent from golden) | `BinaryName` pin |
| 3 | `writable_exe` | copy a benign EXE to a writable dir, launch | **Exe 8004 block** | Exe W^X `Deny(*)` |
| 4 | `writable_script` (ps1/vbs/bat/js) | drop a script, invoke its host | **interpreter Exe-denied** / Script 8007 | deny-by-default Script + no interpreter allow |
| 5 | `unsigned_dll_sideload` | plant an unsigned DLL, force a load | **Dll 8004 block** | publisher-only Dll |
| 6 | `signed_dll_comhijack_from_writable` | plant a genuinely-signed DLL in a writable dir + HKCU COM `InprocServer32` hijack, trigger load | **Dll 8004 block** by the per-tenant Dll `Deny(*)` | §5 reducible-half closure — **HARD** |
| 7 | `common_ini_mutation` | write `AllowDllImport=1` into `config\common.ini` | **NTFS write DENIED** | G5v2 `common.ini` Deny-write |
| 8 | `import_native_exec` (operator) | an EA `#import "kernel32.dll"` under a running MT5 | **no native exec** (`AllowDllImport=0`) | immutable ceiling + vetted-empty golden |
| 9 | `restart_persistence` | reboot the host / re-login; re-run §2.1 + §5 read-backs | **isolation state unchanged** | durable ACL + policy |
| PC | `mt5_normal_positive_control` (operator) | tenant uses MT5 normally | **MT5 works** (RULE-11 positive) | proves the controls do not break the product |
| PC | measurement control | AppLocker channel captured ≥1 `8002/8005` allow for the tenant SID | **measurement_proven=true** | RULE-11: a clean negative is only trusted if the channel is live |

> RULE-11 negative-control per case: each escape tool is **also** run once against an **unprotected** scratch identity
> (no W^X) and shown to SUCCEED — proving a "blocked" result is the control acting, not a broken payload.

---

## 7. Expected pass/fail criteria (authoritative — from `escape_evidence.json`)

The cert **PASSES** iff **all** hold (the evidence collector enforces this — it will not emit `PASS` otherwise):
- `measurement_proven = true` (≥1 `8002/8005` allow for the tenant SID — else `MEASUREMENT_UNPROVEN`, hard fail);
- **the full battery actually ran and blocked (no silent shortening):** `missing_required = []` (every required case
  present — else `INCOMPLETE_BATTERY`), `counts.no_artefact = 0` (no plant failure — else `PLANT_FAILED`), and every
  required case is decisively `PASS_BLOCKED` (`undecided_required = []`). An un-run or absent `tenant_attempts.json`
  is a hard fail (`*_NO_BATTERY`), so "zero escapes" can never masquerade as PASS;
- `counts.fail_escaped = 0` (no escape produced an allow event on its exact artefact path);
- `counts.inconclusive = 0` under Enforce (every attempted escape produced a decisive block/allow event on its exact
  path — a `writable_script` case that shows `INCONCLUSIVE` because the block keyed on the interpreter EXE rather than
  the script is confirmed by the operator from the interpreter `8004`);
- `state.allowdllimport = "0"` (ceiling EXPLICITLY set — an absent/empty key does NOT pass — else `FAIL_ALLOWDLLIMPORT`);
- the operator cases 8 + PC (MT5 normal, `#import` no-exec) pass by operator observation;
- case 9 restart fingerprint equals the pre-restart fingerprint (`fingerprint_sha256` match).

Any `FAIL_ESCAPED`, `MEASUREMENT_UNPROVEN`, `INCOMPLETE_BATTERY`, `PLANT_FAILED`, `INCONCLUSIVE`, `FAIL_ALLOWDLLIMPORT`,
or a `*_NO_BATTERY` load error = **cert FAIL** → §8 decision tree. The evidence collector correlates each escape by
its **exact full artefact path** (not a leaf substring), so a legitimate binary sharing a name — e.g. the golden RX
`terminal64.exe` vs the copied-escape `terminal64.exe` in the WorkDir — can never be cross-attributed.

---

## 8. Rollback procedure + decision tree

**Rollback primitives (all reversible, all read-back-verified):**
- AppLocker: `Set-GuvfxAppLocker.ps1 -Mode Rollback -StateDir C:\GuvFX\_applocker_10e` (restores the captured baseline — no blind clear).
- Per-tenant fragments: `Set-GuvfxAppLockerTenant.ps1 -Mode Remove -AccountId <N> -HostedUser guvfx_u_<N>` (strips only that account's `4d54` rules; refuses Customer Zero).
- G5v2 NTFS ACL: `Set-GuvfxWorkspaceAclV2.ps1 -Mode Rollback -SnapshotPath <snap>` (restores every mutated path's SDDL).

```
                       ┌── escape_evidence.overall ──┐
                       │                              │
             PASS ─────┤                              ├───── FAIL_ESCAPED
              │        │                              │        │
   record evidence,    │                    a control did not block:
   proceed to §10      │                    (a) is the control APPLIED? re-check §5/§6.1 read-backs
                       │                    (b) fix the control in-repo (branch, review, re-merge DARK)
   MEASUREMENT_UNPROVEN │                    (c) redeploy to the disposable host, re-run the battery
     (channel dead) ───┤                    NEVER weaken/shorten the battery to make it pass.
       enable audit    │
       channel, re-run │
                       │
      INCONCLUSIVE ────┘  (no decisive event) -> raise -SinceMinutes / confirm the attempt actually ran as the
                          tenant SID / confirm Enforce (not AuditOnly); re-run. Do NOT pass on absence of evidence.
```

If at any point CZ (production) were somehow affected (it must not be, since the battery runs only on the disposable
host): **STOP**, roll back on CZ, and re-capture the CZ AFTER fingerprint (§9) — a mismatch vs `cz_before.json` is a
Customer-Zero-drift incident.

---

## 9. Customer Zero before/after verification (deployment environment — only after §10 PASS)

This section is executed **only** after the disposable host has genuinely certified (§10), when rolling the certified
policy to production, and is a **Nuno-gated** live-host action (§11):
1. `cz_before.json` from §2.2 is the baseline (captured read-only, no mutation).
2. Apply the **certified** artefacts to CZ (`guvfx_u_1`) with per-op read-back at each step; instant rollback (§8) on any legitimate MT5 break.
3. Capture `Get-GuvfxIsolationFingerprint.ps1 ... -Label after -OutFile cz_after.json`.
4. **Compare:** the isolation-relevant `fingerprint_sha256` must reflect the *intended* new W^X state, while CZ's
   `terminal64_sha256` / `terminal64_version` and the live MT5 `session_process_set` remain intact (MT5 not disturbed).
   A regression in CZ's terminal, session, or an unexpected policy delta = **Customer-Zero-drift STOP**.

---

## 10. Final certification checklist (emit `REMOTEAPP_ISOLATION_CERTIFIED` only when ALL are ticked)

- [ ] All PowerShell artefacts `ParseFile()`-clean on the host (§1.1).
- [ ] AppLocker audit channels enabled + a positive control (`8002/8005`) captured (§1.2, §7 `measurement_proven`).
- [ ] Golden gate `vetted_empty` + `positive_control=passed` (§4).
- [ ] G5v2 ACL applied + `verify_workspace_acl_v2` OK; `common.ini` Deny-write present (§0).
- [ ] Base allow model Enforced; MetaEditor `BinaryName` pin verified against the host's real embedded name (RULE 11).
- [ ] Per-tenant Exe W^X `Deny(*)` applied + read-back (§3.1).
- [ ] Per-tenant **Dll** W^X `Deny(*)` applied from a **soak-derived** exception set + read-back (§5) — reducible-half closed.
- [ ] `escape_evidence.overall = PASS`: `fail_escaped=0`, `inconclusive=0`, `measurement_proven=true` (§6, §7).
- [ ] Case 6 `signed_dll_comhijack_from_writable` shown **blocked** by the Dll `Deny(*)` (hard precondition).
- [ ] Operator cases 8 + PC pass (MT5 normal; `#import` no native exec) (§11).
- [ ] Case 9 restart persistence: fingerprint unchanged after reboot (§6, §7).
- [ ] Rollback rehearsed + verified on the disposable host (§8).
- [ ] Evidence manifest (`escape_evidence.json`) archived with SHA256; deviations recorded (RULE 7 — no overstated completion).

Only then: set `HOSTED_REMOTEAPP_ISOLATION_CERTIFIED=1` (NO-FAKE-READY), and continue the ADR-0041 chain
(`HOSTED_OBSERVATION_CERTIFIED → WORKSPACE_READY → AUTONOMOUS_ONBOARDING_CERTIFIED → FIRST_UNASSISTED_USER_CERTIFIED`).

---

## 11. Operator (Nuno-only) manual actions — the things automation cannot do

These are the only steps that require a human; everything else in §1–§10 is scripted:

1. **Provision the disposable certification host** (or approve its provisioning) mirroring production (§0).
2. **Enter the disposable demo broker credentials into MT5** in the tenant RemoteApp session (§3.2, §6, §8) — an
   automated agent must never enter broker credentials. Use a **disposable demo** account, never a live/signal account.
3. **Authorize + perform the Enforce flip** and the escape-battery run on the disposable host (§6.1) — a supported
   mechanism, not an interactive-SSH long-running service (RULE 1).
4. **Observe the two operator cases** (case 8 `#import` no-exec; PC MT5-normal) and record the result.
5. **Authorize the eventual production roll-out** to Customer Zero (§9) — a separate, live-host, Nuno-gated decision
   taken only after a genuine disposable-host PASS.

No step in this runbook authorizes an order or a live trade; execution stays DARK throughout.

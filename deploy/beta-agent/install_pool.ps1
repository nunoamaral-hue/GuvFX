# CVM-Inc-3 B3P-2 - provision the pre-provisioned SLOT POOL: identities, rights, directories, ACLs, tasks.
# DARK ARTEFACT: RUN ONLY on the host, as Administrator, AFTER the Install Authorisation gate.
# INSTALL-ONLY: it creates objects and stops. It does NOT start the service, does NOT TRIGGER any task, does
# NOT launch MT5, does NOT stage a runtime into a slot, and never touches Session 3 / the prod terminal / the
# bridge (:8788) / :8787 / autologon / any GuvFX_* task. ON-DEMAND MODEL (ADR 0017): the eight beta tasks are
# registered ENABLED but TRIGGERLESS - enabled is not scheduled, so nothing starts them automatically; the only
# thing that ever runs one is a signed, authorised agent request. Nothing runs during or after install.
#
# Dry-run by default. Pass -Apply to perform the provisioning.
#
# Addresses install-only review findings F1 (pool paths + ACLs), F2 (identities, rights, tasks, golden) and
# supplies the approved-task definitions the agent's launch gate (F3) asserts against.
#
# PASSWORDS ARE NOT PARAMETERS. They are prompted for interactively as SecureString, so they never appear in
# a command line, a process listing, a shell history, a transcript or a scheduled-task argument. There is
# deliberately no switch to supply them non-interactively.
param(
  [int]$PoolSize            = 4,
  [string]$SlotsRoot        = "C:\GuvFX\beta\slots",
  [string]$TombstonesRoot   = "C:\GuvFX\beta\tombstones",
  [string]$GoldenDir        = "C:\GuvFX\beta\golden",
  [string]$IdentityPrefix   = "guvfx_b_slot",
  [string]$LaunchPrefix     = "GuvFXBetaRuntime-",
  [string]$StopPrefix       = "GuvFXBetaRuntimeStop-",
  [string]$ApprovedTasksOut = "C:\GuvFX\beta\agent-state\approved_tasks.json",
  # ADR-0016: the launch wrapper lives in an admin-only-writable directory OUTSIDE every slot tree, so a slot
  # can never rewrite its own launcher. Slots (and only slots) get ReadAndExecute on it.
  [string]$LauncherDir      = "C:\GuvFX\beta\launcher",
  [string]$LaunchWrapper    = "slot_launch.ps1",
  [switch]$Apply,
  # Re-run the VERIFY block ALONE against an already-provisioned pool. Creates nothing, registers nothing,
  # writes no ACL, and never prompts for a password. It exists because the mutating half of -Apply can
  # succeed while VERIFY aborts: re-running -Apply to re-verify would re-register all eight tasks and
  # re-prompt the operator for four passwords to redo work that was already correct.
  [switch]$VerifyOnly,
  # Run ONLY the golden-image validation and exit. Read-only by construction: it reaches no identity,
  # right, ACL, task or directory-creation step.
  [switch]$ValidateGoldenOnly,
  # TSV: apply ONLY the task-ACL grant (service read+execute on the eight beta tasks) and exit. Admin-only,
  # so it needs NO slot password and registers/re-registers NOTHING - it exists precisely because the grant
  # is a credential-free SD change that should not force a full four-password -Apply just to (re)apply it.
  [switch]$GrantTaskAccessOnly,
  # ON-DEMAND MODEL (ADR 0017): move the eight already-registered beta tasks from install-only Disabled to the
  # ENABLED-but-triggerless resting state, and exit. Enable-ScheduledTask is an admin-only state change that
  # needs NO slot password and re-registers NOTHING, so - like -GrantTaskAccessOnly - it must not force a full
  # four-password -Apply. It refuses to enable anything that is not exactly a reviewed beta task (right
  # principal, RunLevel Limited, zero triggers), and read-back-verifies Enabled + zero triggers.
  [switch]$EnableTasksOnly
)
$ErrorActionPreference = "Stop"
# The reviewed launch wrapper's pinned SHA-256 (lowercase). tests_install_artefacts.py asserts this equals the
# hash of deploy/beta-agent/slot_launch.ps1, so it can never drift from the file; the install refuses to stage
# a wrapper whose hash differs, before AND after the copy (ADR-0016; WinSW hash-pin pattern).
$LaunchWrapperSha256 = "c18f5c3cf71e1b4afe4567c4bc98808db8a26f613dea646e97c823f7c9b83261"
if ($Apply -and $VerifyOnly) { throw "refusing: -Apply and -VerifyOnly are mutually exclusive" }
if ($GrantTaskAccessOnly -and ($Apply -or $VerifyOnly -or $ValidateGoldenOnly -or $EnableTasksOnly)) {
  throw "refusing: -GrantTaskAccessOnly is a standalone mode (no -Apply/-VerifyOnly/-ValidateGoldenOnly/-EnableTasksOnly)"
}
if ($EnableTasksOnly -and ($Apply -or $VerifyOnly -or $ValidateGoldenOnly -or $GrantTaskAccessOnly)) {
  throw "refusing: -EnableTasksOnly is a standalone mode (no -Apply/-VerifyOnly/-ValidateGoldenOnly/-GrantTaskAccessOnly)"
}
#: May this run CHANGE the host? -VerifyOnly must never mutate.
$Mutate = [bool]$Apply
#: May this run ASSERT against a provisioned host? Both -Apply and -VerifyOnly verify; PLAN cannot.
$Check  = ([bool]$Apply) -or ([bool]$VerifyOnly)
function Step($m) { Write-Host "==> $m" }
function DoIt($desc, [scriptblock]$block) {
  if ($Mutate)         { Step "APPLY: $desc"; & $block }
  elseif ($VerifyOnly) { Step "SKIP:  $desc (VerifyOnly - nothing is changed)" }
  else                 { Step "PLAN:  $desc" }
}

function Get-GuvfxCount {
  <# Count a value that may be $null, a scalar, or a collection.

     `@($null).Count` is 1 in PowerShell, not 0 - @() wraps the null into a one-element array. Every
     "how many of these are there" check written as `@($x).Count` therefore reads ABSENT as ONE.

     This is not hypothetical. `if (@($task.Triggers).Count -gt 0)` aborted the credentialed APPLY on
     2026-07-23 with "task 'GuvFXBetaRuntime-1' has a trigger; expected on-demand only". The task had no
     trigger - COM reported Triggers.Count 0, the registered XML contained `<Triggers />`, and schtasks
     said "On demand only". Get-ScheduledTask returns $null for .Triggers on a trigger-less task, and the
     check counted that null as one. It could never have passed for ANY trigger-less task. #>
  param($Value)
  if ($null -eq $Value) { return 0 }
  return @($Value).Count
}

# -- 0. Refusals. These are the estate objects this script must never come near. Checked BEFORE anything.
$ForbiddenTasks = @("GuvFX_Autostart","GuvFX_SignalBridge","GuvFX_BridgeWatchdog","GuvFX_LaunchMT5","GFX_LaunchIS6")
$ForbiddenPaths = @("C:\GuvFX\accounts","C:\GuvFX\terminals")
foreach ($p in @($SlotsRoot, $TombstonesRoot, $GoldenDir)) {
  foreach ($f in $ForbiddenPaths) {
    if ($p -like "$f*") { throw "refusing: '$p' is inside the operator's estate ('$f')" }
  }
}
# Beta-owned objects must live in the beta namespace. The golden image is different in kind: it is a
# READ-ONLY INPUT that the agent only ever reads, so it may live under C:\GuvFX\golden\ as well. It is
# still refused anywhere outside those two roots, and RULE 10 still forbids the production install.
foreach ($p in @($SlotsRoot, $TombstonesRoot, $ApprovedTasksOut, $LauncherDir)) {
  if ($p -notlike "C:\GuvFX\beta\*") { throw "refusing: '$p' is outside C:\GuvFX\beta\" }
}
# $ApprovedTasksOut had no refusal at all, yet it is the target of this script's most destructive file
# primitive: inheritance is stripped and the DACL is rewritten. Pointed at an estate path it would have
# stripped that path's inherited access. It is now held to the same namespace as every other write target.
if (($GoldenDir -notlike "C:\GuvFX\beta\*") -and ($GoldenDir -notlike "C:\GuvFX\golden\*")) {
  throw "refusing: golden image '$GoldenDir' is outside C:\GuvFX\beta\ and C:\GuvFX\golden\"
}
if ($LaunchPrefix -notlike "GuvFXBetaRuntime*" -or $StopPrefix -notlike "GuvFXBetaRuntime*") {
  throw "refusing: task prefixes must be in the beta task namespace"
}
if ($IdentityPrefix -ne "guvfx_b_slot") {
  throw "refusing: identity prefix must match win_primitives.RUNTIME_IDENTITY_PREFIX"
}
Write-Host "ok   namespace refusals pass (estate paths, estate tasks, identity + task prefixes)"

# Capture the estate BEFORE anything is created, so the post-APPLY comparison has a real "before" rather
# than asserting that nothing changed by printing nothing. Read-only.
$EstateBefore = @{}
foreach ($t in $ForbiddenTasks) {
  $e = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
  if ($e) { $EstateBefore[$t] = @{ state = [string]$e.State; principal = [string]$e.Principal.UserId } }
}
Write-Host ("ok   estate captured before any mutation (" + $EstateBefore.Count + " of " +
            $ForbiddenTasks.Count + " estate tasks present)")

# -- 1. Preconditions: the golden image must be a DEDICATED CLEAN INSTALL (permanent RULE 10).
#
#      The production MT5 installation must NEVER become the beta golden source. It carries the operator's
#      broker credentials in config\accounts.dat and its whole trading history in bases\ - promoting it
#      would copy a live login into every beta slot. The checks below are written to REFUSE such an image
#      rather than to trust that nobody would do it.
function Test-GoldenImage {
  <# Distinguishes SHIPPED artefacts from EVIDENCE OF USE.

     A freshly installed MetaTrader 5 already contains sample Expert Advisors (Advisors, Examples,
     Free Robots), default chart profiles and templates under both Profiles\ and MQL5\Profiles\, Sounds\,
     and empty config\ and MQL5\Logs\ directories. Rejecting those would reject every clean install ever
     made. What actually proves a terminal was RUN - and what actually carries risk into a slot - is the
     FILES a run leaves behind: a saved broker login, a downloaded server list, settings written on exit,
     downloaded history, logs. So every check below is on a FILE, not on the presence of a directory. #>
  param([Parameter(Mandatory)][string]$Path)
  $fail = @(); $ok = @(); $note = @()

  # (a) expected directory structure
  # terminal64.exe is the ONLY hard structural requirement. MQL5\ is deliberately NOT required: a fresh
  # non-portable install keeps its data directory under %APPDATA%\MetaQuotes\Terminal\<hash>\, so the
  # install tree legitimately has no MQL5 at all, and a /portable launch creates one inside the slot on
  # first run. Requiring it rejected a genuine installer output.
  if (Test-Path (Join-Path $Path "terminal64.exe")) { $ok += "structure: terminal64.exe present" }
  else { $fail += "structure: required entry 'terminal64.exe' is missing" }
  if (Test-Path (Join-Path $Path "MQL5")) { $ok += "structure: MQL5 present (portable-style tree)" }
  else { $ok += "structure: no MQL5 (non-portable install; /portable creates it in the slot at first run)" }
  foreach ($marker in @(".guvfx_golden_manifest", ".guvfx_portable")) {
    if (Test-Path (Join-Path $Path $marker)) { $ok += "marker: $marker present" }
    else { $fail += "marker: required GuvFX marker '$marker' is missing (operator-created, one file)" }
  }

  # (b) expected MT5 version/build, pinned by the manifest - the same string the agent compares against
  #     BETA_AGENT_GOLDEN_MANIFEST_VERSION.
  $manifestPath = Join-Path $Path ".guvfx_golden_manifest"
  $exe = Join-Path $Path "terminal64.exe"
  if ((Test-Path $manifestPath) -and (Test-Path $exe)) {
    $pinned = ((Get-Content $manifestPath -Raw) -replace "\s+$", "").Trim()
    $actual = (Get-Item $exe).VersionInfo.FileVersion
    $actual = if ($actual) { $actual.Trim() } else { "" }
    if (-not $pinned) { $fail += "version: .guvfx_golden_manifest is empty; it must pin the MT5 build" }
    elseif ($actual -ne $pinned) { $fail += "version: terminal64.exe is '$actual' but the manifest pins '$pinned'" }
    else { $ok += "version: terminal64.exe $actual matches the pinned build" }
  } elseif (Test-Path $exe) {
    $note += "version: terminal64.exe reports $((Get-Item $exe).VersionInfo.FileVersion) (no manifest to compare against yet)"
  }

  # (c/d/e/f) evidence of previous use - FILES ONLY. Each entry names what it proves.
  $dirtyFiles = [ordered]@{
    "config\accounts.dat"  = "a saved broker account (runtime-created)"
    "config\servers.dat"   = "a downloaded broker server list (runtime-created)"
    "config\common.ini"    = "terminal settings written on exit (runtime-created)"
    "config\terminal.ini"  = "terminal settings written on exit (runtime-created)"
    "origin.txt"           = "a data-folder redirect marker (runtime-created)"
    "MQL5\experts.dat"     = "the compiled-expert metadata cache (runtime-created)"
  }
  foreach ($rel in $dirtyFiles.Keys) {
    if (Test-Path (Join-Path $Path $rel) -PathType Leaf) {
      $fail += "previous use: '$rel' present - $($dirtyFiles[$rel])"
    }
  }
  # Directories that SHIP EMPTY: only their CONTENTS prove a run.
  $dirtyDirs = [ordered]@{
    "config\certificates" = "per-user certificates (runtime-created)"
    "logs"                = "terminal logs (runtime-created)"
    "MQL5\Logs"           = "MQL5 logs (runtime-created)"
    "MQL5\Presets"        = "saved EA input presets (operator-created)"
  }
  foreach ($rel in $dirtyDirs.Keys) {
    $d = Join-Path $Path $rel
    if (Test-Path $d) {
      $n = @(Get-ChildItem $d -Recurse -File -Force -ErrorAction SilentlyContinue).Count
      if ($n -gt 0) { $fail += "previous use: '$rel' contains $n file(s) - $($dirtyDirs[$rel])" }
      else { $ok += "clean: '$rel' present but empty (ships empty)" }
    }
  }

  # bases\ SHIPS POPULATED: Bases\Default carries MetaQuotes demo history for four pairs, 527 welcome
  # messages and the symbol definitions - 537 files written within two seconds of install. Only a
  # BROKER-NAMED subdirectory beside Default proves the terminal ever connected to a broker.
  $bases = Join-Path $Path "bases"
  if (Test-Path $bases) {
    $brokerDirs = @(Get-ChildItem $bases -Directory -Force -EA SilentlyContinue |
                    Where-Object { $_.Name -ne "Default" })
    if ($brokerDirs.Count -gt 0) {
      foreach ($d in $brokerDirs) {
        $fail += "previous use: 'bases\$($d.Name)' is a broker-named data directory - the terminal connected to a broker (runtime-created)"
      }
    } else {
      $n = @(Get-ChildItem $bases -Recurse -File -Force -EA SilentlyContinue).Count
      $ok += "clean: bases\ holds only the shipped Default tree ($n installer files, no broker directory)"
    }
  }

  # (g) attached EA configuration. MetaQuotes SHIPS sample EAs; only strategies outside the shipped set
  #     indicate an attached or user-supplied strategy.
  $shipped = @("Advisors", "Examples", "Free Robots")
  $experts = Join-Path $Path "MQL5\Experts"
  if (Test-Path $experts) {
    $custom = @(Get-ChildItem $experts -Recurse -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Extension -in ".ex5", ".mq5" } |
                Where-Object {
                  $rel = $_.FullName.Substring($experts.Length).TrimStart("\")
                  $top = ($rel -split "\\")[0]
                  ($rel -eq $_.Name) -or ($shipped -notcontains $top)
                })
    if ($custom.Count -gt 0) {
      $fail += "previous use: MQL5\Experts holds $($custom.Count) NON-SHIPPED strategy file(s) - the golden image must carry no strategy (operator-created)"
      $custom | Select-Object -First 5 | ForEach-Object {
        $fail += "              -> $($_.FullName.Substring($Path.Length + 1))"
      }
    } else {
      $shippedCount = @(Get-ChildItem $experts -Recurse -File -Force -EA SilentlyContinue |
                        Where-Object { $_.Extension -in ".ex5", ".mq5" }).Count
      $ok += "EA: $shippedCount MetaQuotes sample strategies present, none outside the shipped set"
    }
  }

  # (i) FOREIGN PROVENANCE - scan file CONTENTS, not just filenames.
  #     This check exists because the previous candidate golden image passed every filename-based check
  #     while MQL5\experts.dat held 66 absolute paths rooted at another runtime's directory
  #     (C:\GuvFX\terminals\account_001\instance\...). The tree had been copied from a live per-account
  #     runtime and then cleaned; the one file that gave it away survived because nothing in its NAME
  #     suggested it held paths. Checking only for artefacts we thought to name proves the weaker claim.
  $foreign = @(
    @{ Pattern = "C:\\GuvFX\\terminals"; Means = "a per-account runtime directory" },
    @{ Pattern = "C:\\GuvFX\\accounts";  Means = "a legacy per-account runtime directory" },
    @{ Pattern = "C:\\GuvFX\\beta\\slots"; Means = "a beta slot directory" },
    @{ Pattern = "C:\\Users\\";          Means = "a user profile directory" }
  )
  $skipExt = @(".exe", ".dll", ".ico", ".bmp", ".wav", ".png", ".jpg", ".gif", ".mq5", ".mqh")
  $scanned = 0
  $hits = @()
  Get-ChildItem $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
    Where-Object { ($skipExt -notcontains $_.Extension.ToLower()) -and ($_.Length -le 4MB) } |
    ForEach-Object {
      $scanned++
      $b = [IO.File]::ReadAllBytes($_.FullName)
      $scanText = [Text.Encoding]::ASCII.GetString($b) + "`n" + [Text.Encoding]::Unicode.GetString($b)
      foreach ($fp in $foreign) {
        if ($scanText -match [regex]::Escape($fp.Pattern)) {
          $hits += "$($_.FullName.Substring($Path.Length + 1)) references $($fp.Means)"
        }
      }
    }
  if ($hits.Count -gt 0) {
    $fail += "foreign provenance: $($hits.Count) file(s) contain paths belonging to another installation -"
    $fail += "                    this tree was COPIED from an existing runtime, not installed fresh (RULE 10)"
    $hits | Select-Object -First 8 | ForEach-Object { $fail += "              -> $_" }
  } else {
    $ok += "provenance: $scanned scanned file(s) contain no path from another runtime or user profile"
  }

  # (The config\ mtime heuristic that used to live here has been removed. It compared the directory
  # mtime against terminal64.exe, i.e. INSTALL time against VENDOR BUILD time, so it fired on every
  # genuine fresh install. The provenance content scan above answers the same question properly.)

  return [pscustomobject]@{ Passed = ($fail.Count -eq 0); Failures = $fail; Checks = $ok; Notes = $note }
}

# The credential-free task modes provision no runtime and never read the golden image, so they must NOT
# require a staged golden. Golden validation (RULE 10) gates only the runtime-provisioning paths (-Apply /
# -VerifyOnly / -ValidateGoldenOnly), which are mutually exclusive with -GrantTaskAccessOnly/-EnableTasksOnly.
if ($GrantTaskAccessOnly -or $EnableTasksOnly) {
  Write-Host "note credential-free task mode: golden-image validation skipped (no runtime is provisioned and the golden image is never read)"
} else {
  if (-not (Test-Path $GoldenDir)) {
    throw "golden image not staged at $GoldenDir - commission a DEDICATED CLEAN MT5 install (RULE 10); the production terminal must never be promoted"
  }
  Step "validate golden image (RULE 10: dedicated clean install, never the production terminal)"
  $golden = Test-GoldenImage -Path $GoldenDir
  foreach ($c in $golden.Checks) { Write-Host "ok   $c" }
  foreach ($f in $golden.Failures) { Write-Host "FAIL $f" }
  foreach ($n in $golden.Notes) { Write-Host "note $n" }
  if (-not $golden.Passed) {
    if ($ValidateGoldenOnly) {
      Write-Host ""
      Write-Host "ValidateGoldenOnly: validation FAILED. Nothing was created or modified."
      return
    }
    throw "golden image validation FAILED ($($golden.Failures.Count) problem(s)) - aborting before PLAN. A dedicated clean install is required; never promote the production MT5 installation."
  }
  foreach ($n in $golden.Notes) { Write-Host "note $n" }
  Write-Host "ok   golden image validated: clean, versioned, correctly structured"
  if ($ValidateGoldenOnly) {
    Write-Host ""
    Write-Host "ValidateGoldenOnly: stopping here. Nothing else was inspected, created or modified."
    return
  }
}

# -- 1a. LSA interop. Loaded BEFORE identities are created (see the self-test below).
#       SeBatchLogonRight is granted via the LSA policy API, NOT secedit.
#
#      WHY THIS IS NOT secedit. On this host the right IS explicitly assigned in local security policy:
#
#          SeBatchLogonRight = *S-1-5-32-544,*S-1-5-32-551,*S-1-5-32-559
#          (Administrators, Backup Operators, Performance Log Users - the Windows defaults)
#
#      CORRECTION, 2026-07-23: the 2026-07-22 baseline recorded this right as ABSENT, and that reading was
#      wrong. It was a false negative in the CAPTURE, not a fact about the host - see
#      evidence/b3p2-install/baseline_2026-07-22.md. The wrong premise does not change the decision, and
#      the true state argues for it harder: secedit /configure writes a COMPLETE assignment line, so
#      granting our four SIDs means reconstructing those three default principals from a template and
#      hoping the reconstruction is exact. Get one wrong and batch logon is silently revoked machine-wide
#      from whoever held it. LsaAddAccountRights adds ONE right to ONE account and touches nothing else:
#      no line is rewritten, and this script never has to know, infer or recreate the defaults - which is
#      also what the Phase 2 stop-condition decision required of it.
#
#      Properties: only SeBatchLogonRight; only an approved beta-slot SID; every other right and every
#      other principal untouched; fails closed on any LSA error; idempotent.
if (-not ('GuvfxLsa' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GuvfxLsa {
  [StructLayout(LayoutKind.Sequential)]
  public struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
  [StructLayout(LayoutKind.Sequential)]
  public struct LSA_OBJECT_ATTRIBUTES {
    public int Length; public IntPtr RootDirectory; public IntPtr ObjectName;
    public int Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern uint LsaOpenPolicy(IntPtr SystemName, ref LSA_OBJECT_ATTRIBUTES oa, uint access, out IntPtr handle);
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern uint LsaAddAccountRights(IntPtr handle, byte[] sid, LSA_UNICODE_STRING[] rights, uint count);
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern uint LsaRemoveAccountRights(IntPtr handle, byte[] sid,
    [MarshalAs(UnmanagedType.U1)] bool allRights, LSA_UNICODE_STRING[] rights, uint count);
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern uint LsaEnumerateAccountRights(IntPtr handle, byte[] sid, out IntPtr rights, out uint count);
  [DllImport("advapi32.dll")] public static extern uint LsaClose(IntPtr handle);
  [DllImport("advapi32.dll")] public static extern uint LsaFreeMemory(IntPtr buffer);
  [DllImport("advapi32.dll")] public static extern int LsaNtStatusToWinError(uint status);
}
'@ -ErrorAction Stop
}   # guarded: PLAN then APPLY in one console must not die on 'type already exists'

# The ONLY right this script may touch. Any other value is a bug, not a parameter.
$GuvfxRight = "SeBatchLogonRight"
$LSA_READ  = 0x00000801   # POLICY_VIEW_LOCAL_INFORMATION | POLICY_LOOKUP_NAMES
$LSA_WRITE = 0x00000811   # POLICY_CREATE_ACCOUNT          | POLICY_LOOKUP_NAMES
# PowerShell parses an 8-hex-digit literal as Int32, so 0xC0000034 WRAPS to -1073741772 while the LSA
# return is UInt32 3221225524 - the comparison would be False for ever, turning the benign "this account
# holds no rights" status into a hard failure. [uint32]0xC0000034 does not help: the literal has already
# wrapped, and the cast then throws. The decimal form is the only one that survives.
$STATUS_OBJECT_NAME_NOT_FOUND = [uint32]3221225524   # 0xC0000034 STATUS_OBJECT_NAME_NOT_FOUND

function Get-ApprovedSlotSidBytes {
  <# Resolve an account name to SID BYTES, refusing anything outside the beta-slot namespace.
     Taking a NAME and validating it - rather than accepting a SID - is what makes it impossible for this
     function to act on Administrators, SYSTEM, or any other principal. #>
  param([Parameter(Mandatory)][string]$AccountName)
  if ($AccountName -notmatch '^guvfx_b_slot[1-9][0-9]*$') {
    throw "refusing user-right operation on '$AccountName': outside the beta-slot identity namespace"
  }
  $u = Get-LocalUser -Name $AccountName -ErrorAction Stop
  $sid = $u.SID
  $bytes = New-Object byte[] $sid.BinaryLength
  $sid.GetBinaryForm($bytes, 0)
  return @{ Bytes = $bytes; Value = $sid.Value }
}

function Add-GuvfxUsersMembership {
  <# Idempotent 'Users' membership for one approved beta-slot account. Separate from creation because the
     two are not atomic: a run interrupted between New-LocalUser and the group add leaves an account in no
     group at all, and the re-run path must repair that rather than skip it. #>
  param([Parameter(Mandatory)][string]$AccountName)
  if ($AccountName -notmatch "^guvfx_b_slot[1-9][0-9]*$") {
    throw "refusing group membership for '$AccountName': outside the beta-slot identity namespace"
  }
  $already = @(Get-LocalGroupMember -Group "Users" -ErrorAction SilentlyContinue |
               Where-Object { $_.Name -like "*\$AccountName" })
  if ($already.Count -gt 0) { return }
  Add-LocalGroupMember -Group "Users" -Member $AccountName -ErrorAction Stop
}

function Invoke-GuvfxIcacls {
  <# icacls is a NATIVE command, so $ErrorActionPreference = "Stop" does not apply to it and a failed ACL
     is silent. Six of the eight calls in this script were previously unchecked, which meant a slot could
     be left with the wrong access while the run printed "ok" and continued to the next step. Every call
     goes through here so that cannot happen. #>
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string[]]$Arguments)
  # 2>&1 makes PS 5.1 wrap native stderr in ErrorRecords, which $ErrorActionPreference = "Stop" turns into
  # a TERMINATING error - so the descriptive throw below would never be reached and the operator would see
  # a bare icacls line instead of which grant on which path failed.
  $prev = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { $out = & icacls $Path @Arguments 2>&1 } finally { $ErrorActionPreference = $prev }
  if ($LASTEXITCODE -ne 0) {
    throw ("icacls " + ($Arguments -join " ") + " failed on '$Path' (exit $LASTEXITCODE): " + ($out -join "; "))
  }
}

function Get-GuvfxServiceSidValue {
  <# The beta agent's virtual service-account SID, computed from the FIXED name via sc.exe showsid. A service
     SID is DERIVED from the name, so it is a stable value before the service exists (install_pool runs before
     install_service). Read-only. Used to pin the grantee in the launch task's wrapper argument (ADR-0016) so
     the wrapper grants query access to exactly NT SERVICE\GuvFXBetaAgent and nothing else. #>
  param([Parameter(Mandatory)][string]$ServiceName)
  if ($ServiceName -ne "GuvFXBetaAgent") { throw "refusing service SID lookup for '$ServiceName'" }
  $shown = & sc.exe showsid $ServiceName 2>&1
  $match = $shown | Select-String -Pattern "SERVICE SID:\s*(S-1-5-80-\S+)"
  if (-not $match) { throw "could not compute the service SID for '$ServiceName' (sc.exe showsid gave no SERVICE SID)" }
  $v = $match.Matches.Groups[1].Value
  if ($v -notmatch "^S-1-5-80-\d+-\d+-\d+-\d+-\d+$") { throw "refusing: '$v' is not a service SID (expected the S-1-5-80- namespace)" }
  return $v
}

#: Task Scheduler READ+EXECUTE (open + read definition + run). GENERIC_READ(0x120089)|FILE_EXECUTE(0x20).
#: Deliberately NOT write/delete/change-permissions - the service may find and run its own tasks, nothing more.
$GuvfxTaskReadRunMask = 0x1200a9

function Grant-GuvfxServiceTaskAccess {
  <# Grant the beta service SID READ+EXECUTE on ONE beta task (TSV, 2026-07-25). The service looks its task
     up by exact name and needs task-level read to open it; it must NOT get folder-list, write, delete or
     change-permissions. Idempotent: any existing ACE for this SID is removed before the single grant is
     added, so re-running -Apply cannot accumulate ACEs. Uses RawSecurityDescriptor rather than SDDL string
     surgery so the ACE mask/order is exact and the other ACEs are preserved unchanged. #>
  param([Parameter(Mandatory)][string]$TaskName, [Parameter(Mandatory)][string]$ServiceSid)
  if ($ServiceSid -notmatch "^S-1-5-80-\d+-\d+-\d+-\d+-\d+$") { throw "refusing task grant to non-service SID '$ServiceSid'" }
  if ($TaskName -notmatch "^GuvFXBetaRuntime(Stop)?-[1-9][0-9]*$") { throw "refusing task grant to non-beta task '$TaskName'" }
  $svc = New-Object -ComObject Schedule.Service; $svc.Connect()
  $t = $svc.GetFolder("\").GetTask($TaskName)
  $sd = New-Object System.Security.AccessControl.RawSecurityDescriptor($t.GetSecurityDescriptor(7))
  $sid = New-Object System.Security.Principal.SecurityIdentifier($ServiceSid)
  for ($i = $sd.DiscretionaryAcl.Count - 1; $i -ge 0; $i--) {
    if ($sd.DiscretionaryAcl[$i].SecurityIdentifier -eq $sid) { $sd.DiscretionaryAcl.RemoveAce($i) }
  }
  $ace = New-Object System.Security.AccessControl.CommonAce(
      [System.Security.AccessControl.AceFlags]::None,
      [System.Security.AccessControl.AceQualifier]::AccessAllowed,
      $GuvfxTaskReadRunMask, $sid, $false, $null)
  $sd.DiscretionaryAcl.InsertAce($sd.DiscretionaryAcl.Count, $ace)
  $t.SetSecurityDescriptor($sd.GetSddlForm([System.Security.AccessControl.AccessControlSections]::All), 0)
  # Read back and assert EXACTLY this mask for the service SID and nothing broader (RULE 11).
  $back = New-Object System.Security.AccessControl.RawSecurityDescriptor(($svc.GetFolder("\").GetTask($TaskName)).GetSecurityDescriptor(7))
  $svcAces = @($back.DiscretionaryAcl | Where-Object { $_.SecurityIdentifier -eq $sid })
  if ($svcAces.Count -ne 1) { throw "task '$TaskName': expected exactly ONE service ACE, found $($svcAces.Count)" }
  if ([int]$svcAces[0].AccessMask -ne $GuvfxTaskReadRunMask) {
    throw ("task '$TaskName': service ACE mask is 0x{0:X} but read+execute 0x{1:X} was authorised - STOP" -f [int]$svcAces[0].AccessMask, $GuvfxTaskReadRunMask)
  }
  Write-Host ("evidence task_acl task=$TaskName service_sid=$ServiceSid mask=0x{0:X} result=granted" -f $GuvfxTaskReadRunMask)
}

#: Task Scheduler RUN right == FILE_EXECUTE (0x20). Under ADR 0017 a task is ENABLED at rest, so its DACL is the
#: sole start-authorisation gate; only Administrators, SYSTEM and the beta service may hold the run right.
$GuvfxTaskRunBit = 0x20

function Assert-GuvfxTaskRunAclSafe {
  <# ON-DEMAND MODEL (ADR 0017): once a task is ENABLED, nothing but its DACL stops a non-agent caller starting
     it. The boundary REQUIRES that the slot identity (and every other non-admin principal) has NO run right, so
     the only way a task runs is a signed agent request (or an administrator). Assert it directly rather than
     trust the OS-default DACL (RULE 11 / no-assumption): enumerate the FULL DACL and fail closed on ANY
     AccessAllowed ACE that carries the run bit for a SID other than Administrators, SYSTEM or the beta service.
     Read-only - it inspects, never modifies the descriptor. #>
  param([Parameter(Mandatory)][string]$TaskName, [Parameter(Mandatory)][string]$ServiceSid)
  if ($TaskName -notmatch "^GuvFXBetaRuntime(Stop)?-[1-9][0-9]*$") { throw "refusing run-ACL check for non-beta task '$TaskName'" }
  $adminSid = "S-1-5-32-544"; $systemSid = "S-1-5-18"
  $svc = New-Object -ComObject Schedule.Service; $svc.Connect()
  $sd = New-Object System.Security.AccessControl.RawSecurityDescriptor(($svc.GetFolder("\").GetTask($TaskName)).GetSecurityDescriptor(4))
  $offenders = @()
  foreach ($ace in $sd.DiscretionaryAcl) {
    if ($ace.AceQualifier.ToString() -ne "AccessAllowed") { continue }
    if (([int]$ace.AccessMask -band $GuvfxTaskRunBit) -eq 0) { continue }         # no run bit -> harmless
    $sidV = $ace.SecurityIdentifier.Value
    if ($sidV -eq $adminSid -or $sidV -eq $systemSid -or $sidV -eq $ServiceSid) { continue }   # trusted
    $offenders += ("{0} mask=0x{1:X}" -f $sidV, [int]$ace.AccessMask)
  }
  # Positive control (RULE 11): the run-bit detector MUST fire on a run-bearing mask and MUST NOT on a
  # read-only one, so a clean result means "no offending run ACE", never "the check matched nothing".
  if ((0x1200a9 -band $GuvfxTaskRunBit) -eq 0 -or (0x120089 -band $GuvfxTaskRunBit) -ne 0) {
    throw "run-bit detector self-test failed - refusing to certify '$TaskName'"
  }
  if ($offenders.Count -ne 0) {
    throw "task '$TaskName': a NON-service principal can RUN it (run-bearing ACE(s): $($offenders -join '; ')) - the slot identity and all others must have NO run right - STOP"
  }
  Write-Host "ok   ${TaskName}: only Administrators/SYSTEM/beta-service hold the RUN right (no non-service Run ACE)"
}

function Grant-GuvfxServiceRead {
  <# Grant READ on one file to the beta agent's virtual service account.

     WHY THIS IS NOT icacls. This script runs BEFORE install_service.ps1, so the service does not exist
     yet and "NT SERVICE\GuvFXBetaAgent" has no name mapping. icacls fails with 1332 - and it fails the
     same way when handed the RAW SID, because it reverse-resolves the SID to a name before applying.
     Measured on the host: exit 1332, "Successfully processed 0 files". Since icacls applies a whole
     invocation atomically, that one unresolvable principal also discarded the Administrators and SYSTEM
     grants in the same call, leaving the file with inheritance stripped and no explicit ACE.

     A service SID is DERIVED from the service name, so it exists as a value before the service does.
     sc.exe computes it, and Set-Acl binds a SecurityIdentifier directly - no name lookup anywhere. #>
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ServiceName)
  if ($ServiceName -ne "GuvFXBetaAgent") {
    throw "refusing service grant for '$ServiceName': fixed to the beta agent service account"
  }
  $shown = & sc.exe showsid $ServiceName 2>&1
  $match = $shown | Select-String -Pattern "SERVICE SID:\s*(S-1-5-80-\S+)"
  if (-not $match) { throw "could not compute the service SID for '$ServiceName' (sc.exe showsid gave no SERVICE SID)" }
  $sidValue = $match.Matches.Groups[1].Value
  if ($sidValue -notmatch "^S-1-5-80-\d+-\d+-\d+-\d+-\d+$") {
    throw "refusing: '$sidValue' is not a service SID (expected the S-1-5-80- namespace)"
  }
  $sid = New-Object System.Security.Principal.SecurityIdentifier($sidValue)
  $acl = Get-Acl -Path $Path
  $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
      $sid, [System.Security.AccessControl.FileSystemRights]::Read, "Allow")))
  Set-Acl -Path $Path -AclObject $acl
  # Read the DACL back AS SIDs. Asking for NTAccount here would re-introduce the same name lookup that
  # cannot succeed, and a post-check that cannot pass is worse than none.
  $rules = (Get-Acl -Path $Path).GetAccessRules($true, $false,
             [System.Security.Principal.SecurityIdentifier])
  $found = @($rules | Where-Object { $_.IdentityReference.Value -eq $sidValue -and $_.AccessControlType -eq "Allow" })
  if ($found.Count -eq 0) { throw "post-check failed: service SID $sidValue is not on $Path" }
  Write-Host "evidence approvals_acl service_sid=$sidValue rights=Read result=granted"
}

function Open-GuvfxLsaPolicy {
  param([uint32]$Access)
  $oa = New-Object GuvfxLsa+LSA_OBJECT_ATTRIBUTES
  $oa.Length = [Runtime.InteropServices.Marshal]::SizeOf($oa)
  $h = [IntPtr]::Zero
  $st = [GuvfxLsa]::LsaOpenPolicy([IntPtr]::Zero, [ref]$oa, $Access, [ref]$h)
  if ($st -ne 0) { throw "LsaOpenPolicy failed: NTSTATUS 0x$('{0:X8}' -f $st) (win32 $([GuvfxLsa]::LsaNtStatusToWinError($st)))" }
  return $h
}

function New-GuvfxLsaString {
  param([string]$Text)
  $u = New-Object GuvfxLsa+LSA_UNICODE_STRING
  $u.Buffer        = [Runtime.InteropServices.Marshal]::StringToHGlobalUni($Text)
  $u.Length        = [uint16]($Text.Length * 2)
  $u.MaximumLength = [uint16](($Text.Length + 1) * 2)
  return $u
}

function Get-GuvfxAccountRights {
  <# READ-ONLY. Returns the rights currently held by the account, or an empty array. #>
  param([Parameter(Mandatory)][string]$AccountName)
  $sid = Get-ApprovedSlotSidBytes -AccountName $AccountName
  $h = Open-GuvfxLsaPolicy -Access $LSA_READ
  try {
    $ptr = [IntPtr]::Zero; $count = [uint32]0
    $st = [GuvfxLsa]::LsaEnumerateAccountRights($h, $sid.Bytes, [ref]$ptr, [ref]$count)
    if ($st -eq $STATUS_OBJECT_NAME_NOT_FOUND) { return @() }
    if ($st -ne 0) { throw "LsaEnumerateAccountRights failed for $AccountName : NTSTATUS 0x$('{0:X8}' -f $st)" }
    $out = @()
    $size = [Runtime.InteropServices.Marshal]::SizeOf([type][GuvfxLsa+LSA_UNICODE_STRING])
    for ($i = 0; $i -lt $count; $i++) {
      $item = [Runtime.InteropServices.Marshal]::PtrToStructure(
                [IntPtr]($ptr.ToInt64() + ($i * $size)), [type][GuvfxLsa+LSA_UNICODE_STRING])
      $out += [Runtime.InteropServices.Marshal]::PtrToStringUni($item.Buffer, $item.Length / 2)
    }
    [void][GuvfxLsa]::LsaFreeMemory($ptr)
    return $out
  } finally { [void][GuvfxLsa]::LsaClose($h) }
}

function Grant-GuvfxBatchLogonRight {
  <# Adds SeBatchLogonRight to ONE approved beta-slot account. Idempotent: LsaAddAccountRights succeeds if
     the account already holds it, and we skip the call entirely when the read shows it present. #>
  param([Parameter(Mandatory)][string]$AccountName)
  $sid = Get-ApprovedSlotSidBytes -AccountName $AccountName
  $before = Get-GuvfxAccountRights -AccountName $AccountName
  if ($before -contains $GuvfxRight) {
    Write-Host "evidence right=$GuvfxRight sid=$($sid.Value) account=$AccountName op=add result=already_present"
    return
  }
  $h = Open-GuvfxLsaPolicy -Access $LSA_WRITE
  try {
    $arr = @(New-GuvfxLsaString -Text $GuvfxRight)
    $st = [GuvfxLsa]::LsaAddAccountRights($h, $sid.Bytes, $arr, 1)
    [Runtime.InteropServices.Marshal]::FreeHGlobal($arr[0].Buffer)
    if ($st -ne 0) {
      # Evidence BEFORE the throw: a failed user-right operation is as much an installation fact as a
      # successful one, and the transcript is the only record of it.
      Write-Host "evidence right=$GuvfxRight sid=$($sid.Value) account=$AccountName op=add result=failed ntstatus=0x$('{0:X8}' -f $st)"
      throw "LsaAddAccountRights failed for $AccountName : NTSTATUS 0x$('{0:X8}' -f $st) (win32 $([GuvfxLsa]::LsaNtStatusToWinError($st)))"
    }
  } finally { [void][GuvfxLsa]::LsaClose($h) }
  $after = Get-GuvfxAccountRights -AccountName $AccountName
  if ($after -notcontains $GuvfxRight) {
    Write-Host "evidence right=$GuvfxRight sid=$($sid.Value) account=$AccountName op=add result=postcheck_failed"
    throw "post-check failed: $AccountName still lacks $GuvfxRight"
  }
  # Every other right the account held must survive. This is the assertion secedit could never make.
  foreach ($r in $before) {
    if ($after -notcontains $r) {
      Write-Host "evidence right=$GuvfxRight sid=$($sid.Value) account=$AccountName op=add result=regression lost=$r"
      throw "user-right regression: $AccountName lost '$r'"
    }
  }
  Write-Host "evidence right=$GuvfxRight sid=$($sid.Value) account=$AccountName op=add result=granted other_rights_preserved=$($before.Count)"
}


# -- 2a. LSA interop self-test. Deliberately BEFORE any account is created: if the interop is wrong, the
#       run must abort while the host is still untouched, not after four accounts exist. Opening and
#       closing a read-only policy handle proves the type compiled, advapi32 bound, the LSA_OBJECT_ATTRIBUTES
#       layout was accepted, and a handle round-tripped - on the real host, in PLAN as well as APPLY.
#       It touches no account and modifies nothing.
Step "LSA interop self-test (read-only policy handle; no account touched)"
$probe = Open-GuvfxLsaPolicy -Access $LSA_READ
[void][GuvfxLsa]::LsaClose($probe)
Write-Host "ok   LSA interop available (LsaOpenPolicy/LsaClose round-trip succeeded)"

# TSV: apply ONLY the task-ACL grant and exit. Admin-only - no slot password, no task (re)registration, no
# directory/identity work. Reached only after the refusals + golden validation + function definitions above.
# It grants the service read+execute on the eight beta tasks (each grant is read-back-verified inside
# Grant-GuvfxServiceTaskAccess) and asserts the root task folder carries NO service ACE, then returns before
# any identity/password step. Idempotent, so it may be re-run to re-assert the grant.
if ($GrantTaskAccessOnly) {
  Step "GRANT TASK ACCESS ONLY: service read+execute on the eight beta tasks (no registration, no passwords)"
  $gtaSid = Get-GuvfxServiceSidValue "GuvFXBetaAgent"
  for ($n = 1; $n -le $PoolSize; $n++) {
    Grant-GuvfxServiceTaskAccess -TaskName "$LaunchPrefix$n" -ServiceSid $gtaSid
    Grant-GuvfxServiceTaskAccess -TaskName "$StopPrefix$n"   -ServiceSid $gtaSid
  }
  $gtaSidObj = New-Object System.Security.Principal.SecurityIdentifier($gtaSid)
  $gtaSvc = New-Object -ComObject Schedule.Service; $gtaSvc.Connect()
  $gtaRoot = New-Object System.Security.AccessControl.RawSecurityDescriptor($gtaSvc.GetFolder("\").GetSecurityDescriptor(7))
  $gtaRootSvc = @($gtaRoot.DiscretionaryAcl | Where-Object { $_.SecurityIdentifier -eq $gtaSidObj })
  if ($gtaRootSvc.Count -ne 0) { throw "root task folder carries a service ACE; the service must have NO folder-level access - STOP" }
  Write-Host "ok   root task folder: service holds NO ACE (no folder-list; exact-name lookup only)"
  Write-Host "ok   GrantTaskAccessOnly complete: eight beta tasks granted read+execute; nothing registered or started"
  return
}

# ON-DEMAND MODEL (ADR 0017): move the eight already-registered beta tasks from install-only Disabled to the
# ENABLED-but-triggerless resting state, and exit. Admin-only - Enable-ScheduledTask needs NO slot password and
# re-registers nothing. It NEVER creates, deletes, re-registers or edits a task action/principal/trigger; it
# only flips Settings.Enabled from false to true, and only for something that is already exactly a reviewed beta
# task. Reached only after the refusals + (skipped) golden validation + function definitions above.
if ($EnableTasksOnly) {
  Step "ENABLE TASKS ONLY: eight beta tasks -> ENABLED + triggerless on-demand (no registration, no passwords)"
  $etSvc = New-Object -ComObject Schedule.Service; $etSvc.Connect()
  $etServiceSid = Get-GuvfxServiceSidValue "GuvFXBetaAgent"
  for ($n = 1; $n -le $PoolSize; $n++) {
    foreach ($t in @("$LaunchPrefix$n", "$StopPrefix$n")) {
      $task = Get-ScheduledTask -TaskName $t -ErrorAction Stop
      # Refuse to enable anything that is not exactly a reviewed beta task: right slot principal + Limited run
      # level. This blocks enabling a wrongly-principalled or elevated task that happens to share the name.
      $principal = $task.Principal
      if ($principal.UserId -notlike "*$IdentityPrefix$n") {
        throw "refusing to enable '$t': principal is $($principal.UserId), not $IdentityPrefix$n"
      }
      if ($principal.RunLevel -ne "Limited") {
        throw "refusing to enable '$t': RunLevel is $($principal.RunLevel), expected Limited"
      }
      # Zero triggers is the security invariant of the on-demand model; corroborate PS against COM so one
      # library's null convention cannot decide it alone. Never enable a task that carries a trigger.
      $trigPs  = Get-GuvfxCount $task.Triggers
      $trigCom = ($etSvc.GetFolder("\").GetTask($t)).Definition.Triggers.Count
      if ($trigPs -ne $trigCom) {
        throw "refusing to enable '$t': trigger count disagrees (Get-ScheduledTask=$trigPs, COM=$trigCom)"
      }
      if ($trigCom -gt 0) { throw "refusing to enable '$t': it carries $trigCom trigger(s); on-demand requires ZERO" }
      # Enabling makes the DACL the sole start gate: BEFORE enabling, prove no non-service principal can Run it
      # (the slot identity in particular must have no run right), else a signed request is not the only caller.
      Assert-GuvfxTaskRunAclSafe -TaskName $t -ServiceSid $etServiceSid
      Enable-ScheduledTask -TaskName $t | Out-Null
      # Read back through BOTH interfaces: the flag is on, the state is no longer Disabled, and no trigger
      # appeared. A task that reports enabled but still Disabled, or gained a trigger, fails closed here.
      $after    = Get-ScheduledTask -TaskName $t -ErrorAction Stop
      $afterCom = $etSvc.GetFolder("\").GetTask($t)
      if (-not $after.Settings.Enabled) { throw "post-enable '$t' still reports Settings.Enabled=false - STOP" }
      if ($after.State -eq "Disabled")  { throw "post-enable '$t' state is still Disabled - STOP" }
      if ($afterCom.Definition.Triggers.Count -gt 0) { throw "post-enable '$t' now carries a trigger - STOP" }
      Write-Host "ok   $t ENABLED, zero triggers, principal $($principal.UserId), RunLevel Limited"
    }
  }
  Write-Host "ok   EnableTasksOnly complete: eight beta tasks Enabled + triggerless; nothing registered, edited or started"
  return
}

# -- 2. Identities. Created here because the agent cannot: it has no user-creation method and holds no
#      credential. Passwords are prompted, never parameters.
# ONE prompt per identity, confirmed by re-entry, held for that identity's whole provisioning. Prompting
# separately for the account and for each task registration invited a typo that creates a working account
# whose tasks can never log it on - a failure that would only surface at first start.
$Secrets = @{}
function Get-SlotSecret($user) {
  if ($Secrets.ContainsKey($user)) { return $Secrets[$user] }
  while ($true) {
    $a = Read-Host -AsSecureString "Password for $user (not echoed, not logged)"
    $b = Read-Host -AsSecureString "Confirm password for $user"
    # SecureStringToBSTR allocates UNMANAGED memory holding the plaintext. It must be released with
    # ZeroFreeBSTR, which overwrites the buffer before freeing it - the earlier code freed nothing, so a
    # plaintext copy of every password stayed in the process for the whole session. try/finally so a
    # comparison failure cannot skip the wipe.
    $ba = [IntPtr]::Zero; $bb = [IntPtr]::Zero
    try {
      $ba = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($a)
      $bb = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($b)
      $pa = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ba)
      $pb = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bb)
      $same  = ($pa -ceq $pb)
      $empty = ($pa.Length -eq 0)
    } finally {
      if ($ba -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ba) }
      if ($bb -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bb) }
      Remove-Variable pa, pb -ErrorAction SilentlyContinue
    }
    if ($empty) { Write-Host "     empty password rejected; try again"; continue }
    if ($same)  { $Secrets[$user] = $a; return $a }
    Write-Host "     passwords did not match; try again"
  }
}

for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  if (Get-LocalUser -Name $user -ErrorAction SilentlyContinue) {
    Write-Host "note identity '$user' already exists; leaving as-is (its password is still needed below)"
    # New-LocalUser and Add-LocalGroupMember are two operations. A run interrupted between them leaves an
    # account that exists but is in no group, and simply skipping here would never repair it. Membership is
    # therefore reconciled on the existing-account path too, and is idempotent.
    if ($Mutate) { Add-GuvfxUsersMembership -AccountName $user }
    continue
  }
  DoIt "create non-admin identity '$user' (password prompted, never a parameter)" {
    New-LocalUser -Name $user -Password (Get-SlotSecret $user) -PasswordNeverExpires -AccountNeverExpires `
                  -Description "GuvFX beta slot $n runtime identity" | Out-Null
    Add-GuvfxUsersMembership -AccountName $user
  }
}
# Assert group membership in BOTH directions: in Users, and in nothing privileged.
# This assertion previously FAILED OPEN. `Get-LocalGroupMember -ErrorAction SilentlyContinue` yields an
# empty pipeline when the cmdlet itself errors - an unresolvable SID in the group, a renamed group, an
# access denial - and an empty pipeline was read as "not a member". The one check standing between the
# sponsor's non-admin guarantee and a privileged runtime identity therefore passed loudest exactly when it
# could see least. It now fails CLOSED: an enumeration error is an error, not a pass.
if ($Check) {
  for ($n = 1; $n -le $PoolSize; $n++) {
    $user = "$IdentityPrefix$n"
    foreach ($g in @("Administrators","Remote Desktop Users","Backup Operators")) {
      try { $members = @(Get-LocalGroupMember -Group $g -ErrorAction Stop) }
      catch { throw "cannot enumerate '$g' to prove '$user' is not a member, so non-admin is UNPROVEN: $($_.Exception.Message)" }
      if ($members | Where-Object { $_.Name -like "*\$user" }) {
        throw "identity '$user' is a member of '$g' - refusing to continue"
      }
    }
    # Positive direction: it must actually BE in Users. Absent that, the account exists in no group and
    # the launch task would fail at first start, long after this window closed. (RULE 11: this loop now
    # has an assertion that can fail, not only ones that can pass.)
    try { $inUsers = @(Get-LocalGroupMember -Group "Users" -ErrorAction Stop | Where-Object { $_.Name -like "*\$user" }) }
    catch { throw "cannot enumerate 'Users' to prove '$user' is a member: $($_.Exception.Message)" }
    if ($inUsers.Count -eq 0) { throw "identity '$user' is not a member of 'Users' - refusing to continue" }
    Write-Host "ok   $user is in Users and in no privileged group"
  }
}


# -- 3. Grant SeBatchLogonRight to each slot identity.
Step "SeBatchLogonRight via the LSA policy API (adds one right to one account; no policy line is rewritten)"
for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  if ($Mutate) {
    Grant-GuvfxBatchLogonRight -AccountName $user
  } else {
    # PLAN reports the delta without modifying anything. Note honestly what it does and does not prove:
    # on a FRESH host the accounts do not exist yet, so the enumerate path cannot be exercised and only the
    # self-test above (LsaOpenPolicy/LsaClose) has entered advapi32. The enumerate marshalling is first
    # exercised on a re-run of PLAN once the accounts exist, or at APPLY - which is why the interop
    # self-test runs before any account is created, so a broken interop costs nothing.
    if (Get-LocalUser -Name $user -ErrorAction SilentlyContinue) {
      $held = Get-GuvfxAccountRights -AccountName $user
      if ($VerifyOnly) {
        # VerifyOnly ASSERTS. Printing a PLAN line here would have let a -VerifyOnly run reach the green
        # epilogue while saying nothing at all about the one user right this pool depends on.
        if ($held -notcontains $GuvfxRight) {
          throw "VERIFY: '$user' does NOT hold $GuvfxRight - the pool cannot launch - STOP"
        }
        Write-Host "ok   $user holds $GuvfxRight (and $(Get-GuvfxCount $held) right(s) total)"
      } else {
        $verb = if ($held -contains $GuvfxRight) { "already holds (no change)" } else { "WOULD ADD" }
        Write-Host "PLAN:  $user $verb $GuvfxRight; currently holds $(Get-GuvfxCount $held) right(s): $($held -join ',')"
      }
    } elseif ($VerifyOnly) {
      throw "VERIFY: identity '$user' does not exist - the pool is not provisioned - STOP"
    } else {
      Write-Host "PLAN:  $user does not exist yet; WOULD ADD $GuvfxRight after creation (enumerate path not exercised until the account exists)"
    }
  }
}

# -- 4. Directories. Slot directories MUST pre-exist: stage_copy refuses when real_path(slot dir) is null,
#      which is what stops the agent materialising into a slot nobody provisioned (robocopy would otherwise
#      create the whole chain - no identity, no ACL, no tasks).
DoIt "create slot + tombstone directories" {
  for ($n = 1; $n -le $PoolSize; $n++) {
    New-Item -ItemType Directory -Force -Path (Join-Path $SlotsRoot "$n"), (Join-Path $TombstonesRoot "$n") | Out-Null
  }
}

# -- 5. ACLs. Each slot identity gets Modify on ITS OWN slot directory and nothing else; read+execute on the
#      golden image (if a runtime could write it, one compromised slot would compromise every future slot).
#      Inheritance is broken at the beta root so nothing upstream can widen these.
DoIt "break inheritance on C:\GuvFX\beta and set explicit ACLs" {
  Invoke-GuvfxIcacls "C:\GuvFX\beta" @("/inheritance:r")
  Invoke-GuvfxIcacls "C:\GuvFX\beta" @("/grant", "*S-1-5-32-544:(OI)(CI)F", "/grant", "*S-1-5-18:(OI)(CI)F")
}
# The golden image needs its inheritance broken TOO, and it is the one directory where forgetting is not
# cosmetic. MEASURED on this host: C:\GuvFX\golden\newMT5 inherits
#     BUILTIN\Users  ReadAndExecute, AppendData, CreateFiles
#     CREATOR OWNER  GENERIC_ALL (inherit-only)
# Every slot identity is a member of Users, so without this it could CREATE files in the golden tree and
# own them outright - and every future MATERIALISE would copy them into every future slot. That is exactly
# the "one compromised slot would compromise every future slot" outcome above. `icacls /grant` is ADDITIVE:
# granting RX does not take AppendData/CreateFiles away. Only breaking inheritance does.
# The golden image lives outside C:\GuvFX\beta, so the beta-root break above cannot reach it.
DoIt "break inheritance on $GoldenDir so the slot grants below are the ONLY non-admin access" {
  Invoke-GuvfxIcacls $GoldenDir @("/inheritance:r")
  Invoke-GuvfxIcacls $GoldenDir @("/grant", "*S-1-5-32-544:(OI)(CI)F", "/grant", "*S-1-5-18:(OI)(CI)F")
}
for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  $slot = Join-Path $SlotsRoot "$n"
  DoIt "grant '$user' Modify on $slot only" {
    Invoke-GuvfxIcacls $slot @("/grant", ("{0}:(OI)(CI)M" -f $user))
  }
  DoIt "grant '$user' ReadAndExecute on $GoldenDir" {
    Invoke-GuvfxIcacls $GoldenDir @("/grant", ("{0}:(OI)(CI)RX" -f $user))
  }
}
DoIt "restrict $TombstonesRoot to Administrators + SYSTEM" {
  Invoke-GuvfxIcacls $TombstonesRoot @("/inheritance:r")
  Invoke-GuvfxIcacls $TombstonesRoot @("/grant", "*S-1-5-32-544:(OI)(CI)F", "/grant", "*S-1-5-18:(OI)(CI)F")
}

# -- 5b. Launch wrapper (ADR-0016 Option A). Stage the reviewed, hash-pinned wrapper into an admin-only-writable
#        directory OUTSIDE every slot tree, so a slot cannot rewrite its own launcher. Slots (and only slots)
#        get ReadAndExecute -- the launch task runs the wrapper AS the slot identity. The wrapper launches
#        terminal64 suspended, grants the beta-agent service query access to that ONE process object, verifies
#        it, and resumes; on failure it terminates the child and fails closed (nothing runs un-observably).
$WrapperSource = Join-Path $PSScriptRoot $LaunchWrapper
$WrapperDest   = Join-Path $LauncherDir $LaunchWrapper
# Computed from the FIXED service name; embedded (pinned) in each launch task's wrapper argument below. sc.exe
# showsid is read-only, so it is safe in every mode; the value is deterministic before the service exists.
$ServiceSidValue = Get-GuvfxServiceSidValue "GuvFXBetaAgent"
Write-Host "ok   beta-agent service SID computed for the launch grantee: $ServiceSidValue"

DoIt "verify the bundled launch wrapper matches the pinned hash BEFORE staging it" {
  if (-not (Test-Path -LiteralPath $WrapperSource -PathType Leaf)) {
    throw "launch wrapper missing from the bundle: $WrapperSource"
  }
  $srcHash = (Get-FileHash -LiteralPath $WrapperSource -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($srcHash -ne $LaunchWrapperSha256) {
    throw "launch wrapper hash mismatch (bundle=$srcHash pinned=$LaunchWrapperSha256) - refusing to stage a wrapper that is not the reviewed one"
  }
  Write-Host "ok   bundled $LaunchWrapper matches the pinned hash"
}
DoIt "create $LauncherDir and stage the launch wrapper (re-hash after copy)" {
  New-Item -ItemType Directory -Force -Path $LauncherDir | Out-Null
  Copy-Item -LiteralPath $WrapperSource -Destination $WrapperDest -Force
  $dstHash = (Get-FileHash -LiteralPath $WrapperDest -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($dstHash -ne $LaunchWrapperSha256) {
    throw "staged wrapper hash mismatch after copy (got=$dstHash) - refusing to leave a wrong wrapper in place"
  }
}
DoIt "ACL ${LauncherDir}: break inheritance, Administrators + SYSTEM Full, each slot ReadAndExecute ONLY" {
  # Same pattern as the golden image (which slots may only Read+Execute): break inheritance first, then the
  # explicit grants are the ONLY non-admin access. A slot with Modify here could rewrite the grant target or
  # the launched binary, so slots get NO write.
  Invoke-GuvfxIcacls $LauncherDir @("/inheritance:r")
  Invoke-GuvfxIcacls $LauncherDir @("/grant", "*S-1-5-32-544:(OI)(CI)F", "/grant", "*S-1-5-18:(OI)(CI)F")
  for ($n = 1; $n -le $PoolSize; $n++) {
    Invoke-GuvfxIcacls $LauncherDir @("/grant", ("{0}{1}:(OI)(CI)RX" -f $IdentityPrefix, $n))
  }
}

# -- 6. Scheduled tasks. Registered ENABLED but TRIGGERLESS (on-demand only, ADR 0017): zero triggers means
#      nothing starts them on their own; the only caller is a signed, authorised agent request. TASK_LOGON_PASSWORD stores
#      the credential in the Task Scheduler credential store - the agent never sees it and cannot read it.
#      S4U was rejected: it stores no password but grants no network access, which MT5 needs.
$Approved = @{}
for ($n = 1; $n -le $PoolSize; $n++) {
  $user   = "$IdentityPrefix$n"
  $slot   = Join-Path $SlotsRoot "$n"
  $work   = Join-Path $slot "terminal"
  $exe    = Join-Path $work "terminal64.exe"
  $launch = "$LaunchPrefix$n"
  $stop   = "$StopPrefix$n"

  foreach ($t in @($launch, $stop)) {
    if ($ForbiddenTasks -contains $t) { throw "refusing: '$t' collides with an estate task" }
  }

  DoIt "register '$launch' (ENABLED, no trigger - on-demand capability; wrapper launches /portable + grants, runs as $user)" {
    # Bound to a variable so ZeroFreeBSTR can reach it. Called inline, the pointer is unrecoverable and the
    # plaintext stays in unmanaged memory for the life of the session - the same defect fixed in
    # Get-SlotSecret, in the two places that actually hold the password longest.
    $bp = [Runtime.InteropServices.Marshal]::SecureStringToBSTR((Get-SlotSecret $user))
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bp)
    # ADR-0016: the launch action is now powershell.exe running the fixed, admin-only, hash-pinned wrapper AS
    # the slot identity. The wrapper launches this slot's own terminal64 (suspended, /portable) and grants the
    # beta-agent service query access to that ONE process object. -File (never -Command/-EncodedCommand) so the
    # code is a fixed on-disk file, not an inline string. The trailing bare /portable is an inert detection
    # marker for portable_switch_present + the approvals digest; the wrapper swallows it and hard-codes its own
    # /portable, so a task argument can never control what terminal64 runs with (no injection surface).
    $wrapArgs = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}" -TerminalPath "{1}" -WorkingDirectory "{2}" -GranteeSid {3} /portable' -f $WrapperDest, $exe, $work, $ServiceSidValue)
    $action    = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $wrapArgs -WorkingDirectory $work
    $settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
                   -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    # ON-DEMAND MODEL (ADR 0017): the task is left ENABLED but TRIGGERLESS. Enabled does NOT mean scheduled -
    # it has zero triggers, so nothing can start it automatically; the ONLY way it runs is a signed, authorised
    # agent request that opens it by exact name and calls Run(). Leaving it enabled avoids granting the service
    # any task write/modify right merely to toggle state before each invocation (New-ScheduledTaskSettingsSet
    # defaults Enabled=$true, so registration alone leaves it enabled + triggerless).
    Register-ScheduledTask -TaskName $launch -Action $action -Settings $settings `
      -User $user -Password $plain -RunLevel Limited -Force | Out-Null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bp)
    Remove-Variable plain, bp
  }
  DoIt "register '$stop' (ENABLED, no trigger - on-demand capability; terminates ONLY this slot's image)" {
    $bp = [Runtime.InteropServices.Marshal]::SecureStringToBSTR((Get-SlotSecret $user))
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bp)
    # SCOPED BY IMAGE PATH, not by image name. `taskkill /IM terminal64.exe` would match by NAME - and the
    # operator's production terminal has the SAME name, because a slot is a copy of the same MT5 image.
    # Relying on "it runs as the slot identity so it can only kill its own processes" would make a
    # not-fully-verifiable OS access-control assumption load-bearing on the one action that could stop live
    # trading. The agent's own code refuses to match a process by name for exactly this reason; the task it
    # triggers must not do what the agent is forbidden to do.
    #
    # Get-Process .Path on another account's process yields nothing readable to a non-admin, and a null
    # path can never equal this slot's path - so the filter fails safe in both directions.
    $kill = "Get-Process -Name terminal64 -ErrorAction SilentlyContinue | " +
            "Where-Object { `$_.Path -eq '$exe' } | Stop-Process -Force"
    $action   = New-ScheduledTaskAction -Execute "powershell.exe" `
                  -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"$kill`"")
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(5))
    # ON-DEMAND MODEL (ADR 0017): left ENABLED but TRIGGERLESS. Zero triggers -> nothing starts it
    # automatically; the ONLY caller is a signed, authorised agent STOP that opens it by exact name and calls
    # Run(). Registration alone leaves it enabled + triggerless; no separate enable/disable dance per invocation.
    Register-ScheduledTask -TaskName $stop -Action $action -Settings $settings `
      -User $user -Password $plain -RunLevel Limited -Force | Out-Null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bp)
    Remove-Variable plain, bp
  }

  # TSV (2026-07-25): the beta service runs as NT SERVICE\GuvFXBetaAgent and looks its ONE approved task up by
  # EXACT NAME (win_slot_ops._registered_task). Host-measured: the root task folder grants Authenticated Users
  # (the service among them) only WRITE, not list, and the tasks carry no service ACE - so the service reads
  # every present task as task_absent, blocking native STOP/TOMBSTONE. Grant the service READ+EXECUTE
  # (0x1200a9: open + read the definition + run) on THIS slot's two tasks only. NOT folder-wide, and no
  # write/delete/change-permissions - a strictly task-scoped, least-privilege grant.
  DoIt "grant the beta service read+execute on '$launch' and '$stop' (TSV: exact-name lookup needs task read)" {
    Grant-GuvfxServiceTaskAccess -TaskName $launch -ServiceSid $ServiceSidValue
    Grant-GuvfxServiceTaskAccess -TaskName $stop   -ServiceSid $ServiceSidValue
  }

  # The approved definition the agent's launch gate asserts against. Read back through the SAME COM
  # interface the agent uses (Schedule.Service), not from the values we intended: Task Scheduler may
  # normalise the principal to a qualified form or a SID, and the gate compares the 7-field digest by exact
  # equality. Pinning what we MEANT to register, while the agent reads what IS registered, would make every
  # first launch fail task_definition_drift - permanently, since the agent never repairs a task.
  if ($Check) {
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    # BOTH task families are pinned. Recording only the launch task inverted the risk exactly backwards:
    # the launch task merely starts MT5 inside an isolated slot, yet was pinned on every field and
    # re-asserted before each trigger, while the TERMINATE task - whose argument string is the only thing
    # standing between `Stop-Process -Force` and the operator's live trading terminal, which carries the
    # SAME image name - was pinned nowhere and asserted never. The approvals file is written once, at
    # install; adding the terminate definitions later would mean re-prompting four passwords and
    # re-registering eight tasks on the live host.
    foreach ($t in @($launch, $stop)) {
      $reg = $svc.GetFolder("\").GetTask($t)
      $p   = $reg.Definition.Principal
      $act = $reg.Definition.Actions.Item(1)
      $Approved[$t] = [ordered]@{
        task_name         = [string]$reg.Name
        run_as_identity   = [string]$p.UserId
        executable        = [string]$act.Path
        working_directory = [string]$act.WorkingDirectory
        arguments         = [string]$act.Arguments
        logon_type        = [int]$p.LogonType
        run_level         = [int]$p.RunLevel
        # ON-DEMAND MODEL (ADR 0017): the task is registered ENABLED and triggerless and stays that way at rest,
        # so the approval pins enabled=true and the runtime gate finds a matching enabled task. A task found
        # DISABLED at runtime is drift and fails closed (occupancy.assert_task_matches_approved).
        enabled           = $true
      }
    }
    if ($Approved[$launch].arguments -notmatch "(^|\s)/portable(\s|$)") {
      throw "registered task '$launch' does not carry /portable - refusing to approve it"
    }
    # ADR-0016 launch-wrapper scope. The launch task now runs powershell.exe against the fixed wrapper, so -
    # exactly like the terminate task - what bounds it is its ARGUMENT string, not a beneath-the-slot
    # executable. Assert what was ACTUALLY registered: powershell.exe, invoking the FIXED wrapper via -File,
    # naming THIS slot's own terminal64, carrying the beta-agent service SID as grantee, and with NO inline
    # command. Any one of these missing means the launch would not grant (or would grant to the wrong
    # principal, or run un-reviewed code), so all are required.
    $launchExe  = [string]$Approved[$launch].executable
    $launchArgs = [string]$Approved[$launch].arguments
    if ($launchExe -notmatch "(?i)(^|\\)powershell\.exe$") {
      throw "registered task '$launch' executable is '$launchExe', not powershell.exe - refusing to approve it"
    }
    if ($launchArgs -notmatch [regex]::Escape('-File "' + $WrapperDest + '"')) {
      throw "registered task '$launch' does not invoke the fixed wrapper via -File `"$WrapperDest`" - refusing to approve it"
    }
    if ($launchArgs -notmatch [regex]::Escape('-TerminalPath "' + $exe + '"')) {
      throw "registered task '$launch' does not target this slot's own terminal64 ('$exe') - refusing to approve it"
    }
    if ($launchArgs -notmatch [regex]::Escape('-GranteeSid ' + $ServiceSidValue)) {
      throw "registered task '$launch' does not carry the beta-agent service SID ($ServiceSidValue) as grantee - refusing to approve it"
    }
    # Catch ANY powershell.exe prefix-abbreviation of -Command or -EncodedCommand (-c..-command, -e/-ec/-en..
    # -encodedcommand), but NEVER -ExecutionPolicy (which diverges at -ex). Mirrors win_primitives
    # _is_inline_command_switch, so the install approval and the runtime launch gate agree.
    if ($launchArgs -match "(?i)(^|\s)-(c(o(m(m(a(n(d)?)?)?)?)?)?|ec|e(n(c(o(d(e(d(c(o(m(m(a(n(d)?)?)?)?)?)?)?)?)?)?)?)?)?)(\s|$)") {
      throw "registered task '$launch' carries an inline command (-Command/-EncodedCommand) - only -File is permitted - refusing to approve it"
    }
    Write-Host "ok   $launch runs the fixed wrapper against this slot's terminal64, granting $ServiceSidValue"
    # The terminate task's scope is its argument string. Assert what was ACTUALLY registered: it must
    # filter on this slot's own executable path, and it must not reach Stop-Process on a name-only
    # pipeline. Both halves are required - the presence of the right filter does not exclude the wrong one.
    $stopArgs = [string]$Approved[$stop].arguments
    if ($stopArgs -notmatch [regex]::Escape("`$_.Path -eq '$exe'")) {
      throw "registered task '$stop' does not scope termination to '$exe' - refusing to approve it"
    }
    if ($stopArgs -match "Get-Process[^|]*\|\s*Stop-Process") {
      throw "registered task '$stop' pipes Get-Process straight into Stop-Process with no path filter - refusing to approve it"
    }
    Write-Host "ok   $stop is scoped to this slot's own image path"
  }
}

# -- 7. Emit the approved-task definitions the agent loads at startup (F3). Without this file the agent
#      refuses to start in slot_pool mode, and no launch can proceed.
DoIt "write approved task definitions to $ApprovedTasksOut" {
  New-Item -ItemType Directory -Force -Path (Split-Path $ApprovedTasksOut) | Out-Null
  # WriteAllText with UTF8Encoding($false), NOT Set-Content -Encoding UTF8: under Windows PowerShell 5.1
  # the latter emits a BOM, and the agent's json.loads would reject "\ufeff{" as malformed - reported as
  # "tampering or a bad edit" during the one window when the operator is judging whether to trust the host.
  [IO.File]::WriteAllText($ApprovedTasksOut, ($Approved | ConvertTo-Json -Depth 4),
                          (New-Object Text.UTF8Encoding $false))
  # Two separate calls, each checked by the wrapper. If stripping inheritance succeeds and the grant then
  # fails, the file is left with NO explicit ACE at all - so the two steps must not share one check.
  Invoke-GuvfxIcacls $ApprovedTasksOut @("/inheritance:r")
  Invoke-GuvfxIcacls $ApprovedTasksOut @("/grant", "*S-1-5-32-544:F", "/grant", "*S-1-5-18:F")
  # The service account needs READ - it loads this file at startup and refuses to start without it. Read
  # only: the agent must never be able to rewrite its own approvals. Inheritance is stripped, so the
  # inheritable grant on the state dir cannot reach this file; the ACE has to be explicit.
  # Literal, not a parameter: the grant target is fixed, exactly like the identity and task prefixes.
  Grant-GuvfxServiceRead -Path $ApprovedTasksOut -ServiceName "GuvFXBetaAgent"
}

# -- 8. Verify (no start, no trigger). ON-DEMAND MODEL (ADR 0017): tasks are ENABLED but TRIGGERLESS at rest.
if ($Check) {
  Step "VERIFY pool (expect: identities non-admin, tasks present and ENABLED, zero triggers, on-demand only)"
  $svcV = New-Object -ComObject Schedule.Service; $svcV.Connect()
  $verifyServiceSid = Get-GuvfxServiceSidValue "GuvFXBetaAgent"
  for ($n = 1; $n -le $PoolSize; $n++) {
    foreach ($t in @("$LaunchPrefix$n", "$StopPrefix$n")) {
      $task = Get-ScheduledTask -TaskName $t -ErrorAction Stop
      # Enabled but triggerless. An enabled, non-running, triggerless task reports State 'Ready'. A 'Disabled'
      # task can never be triggered by a signed agent request (run_task refuses it), so Disabled is now a
      # provisioning FAILURE, not the resting state. Assert the Settings.Enabled flag directly as well, so a
      # transient Running does not mask a task that has been disabled underneath us.
      if (-not $task.Settings.Enabled) { throw "task '$t' is DISABLED; on-demand model requires Enabled" }
      if ($task.State -eq "Disabled") { throw "task '$t' state is Disabled; on-demand model requires Enabled" }
      # Counted null-safely, and corroborated against the Task Scheduler COM definition - the same source
      # the approvals read-back uses - so one library's null convention cannot decide this alone.
      $trigPs  = Get-GuvfxCount $task.Triggers
      $trigCom = ($svcV.GetFolder("\").GetTask($t)).Definition.Triggers.Count
      if ($trigPs -ne $trigCom) {
        throw "task '$t' trigger count disagrees between sources (Get-ScheduledTask=$trigPs, COM=$trigCom) - refusing to judge it"
      }
      if ($trigCom -gt 0) { throw "task '$t' has $trigCom trigger(s); on-demand model requires ZERO triggers" }
      $principal = $task.Principal
      if ($principal.UserId -notlike "*$IdentityPrefix$n") { throw "task '$t' principal is $($principal.UserId)" }
      if ($principal.RunLevel -ne "Limited") { throw "task '$t' RunLevel is $($principal.RunLevel); expected Limited" }
      # ON-DEMAND MODEL (ADR 0017): an enabled task's DACL is the sole start gate. Prove no non-service principal
      # (in particular the slot identity) can Run it, so a signed agent request stays the only way to start it.
      Assert-GuvfxTaskRunAclSafe -TaskName $t -ServiceSid $verifyServiceSid
      Write-Host "ok   $t ENABLED, zero triggers (on-demand), principal $($principal.UserId), RunLevel Limited"
    }
  }
  # This loop previously had no failing branch: a MISSING estate task printed nothing and the run
  # continued, so "estate untouched" was asserted by silence. The five names are captured BEFORE any
  # mutation and compared after, and a task that has gone missing or changed principal is a hard stop.
  # ACLs were the one authorised object class the VERIFY section never inspected: every grant was applied
  # and then taken on trust. Read them back from the OS.
  Step "VERIFY ACLs (read back from the filesystem, not from what we asked for)"
  $goldenAcl = Get-Acl $GoldenDir
  # (1) inheritance actually removed
  if (-not $goldenAcl.AreAccessRulesProtected) {
    throw "golden image '$GoldenDir' still inherits: the Read+Execute-only guarantee is NOT in force - STOP"
  }
  Write-Host "ok   golden: inheritance removed (AreAccessRulesProtected = True)"
  # (2)(3)(4) NO principal may write, create or append - checked as RIGHTS BITS, not as a name substring.
  #     A substring match on "Write|Modify" is what made an earlier check miss AppendData and CreateFiles
  #     entirely and report the tree clean when it was not (RULE 11).
  $WriteMask = ([System.Security.AccessControl.FileSystemRights]::Write -bor
               [System.Security.AccessControl.FileSystemRights]::CreateFiles -bor
               [System.Security.AccessControl.FileSystemRights]::CreateDirectories -bor
               [System.Security.AccessControl.FileSystemRights]::AppendData -bor
               [System.Security.AccessControl.FileSystemRights]::WriteData -bor
               [System.Security.AccessControl.FileSystemRights]::Delete -bor
               [System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
               [System.Security.AccessControl.FileSystemRights]::WriteAttributes -bor
               [System.Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
               [System.Security.AccessControl.FileSystemRights]::ChangePermissions -bor
               [System.Security.AccessControl.FileSystemRights]::TakeOwnership)
  # Only these two may hold write-class rights on the golden image.
  $GOLDEN_WRITERS = @("S-1-5-32-544", "S-1-5-18")     # Administrators, SYSTEM
  $sidRules = $goldenAcl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier])
  $adminSeen = $false; $systemSeen = $false
  foreach ($r in $sidRules) {
    $who = $r.IdentityReference.Value
    if ($r.AccessControlType -ne "Allow") { continue }
    # GENERIC_ALL/GENERIC_WRITE (0x10000000 / 0x40000000) have NO member in FileSystemRights, so a named-bit
    # mask alone is blind to exactly the inherit-only CREATOR OWNER ACE this check exists to catch.
    $raw = [int]$r.FileSystemRights
    # NAMES DIFFERING ONLY BY CASE ARE THE SAME VARIABLE IN POWERSHELL. This was `$WRITEISH` (the mask) and
    # `$writeish` (the per-ACE result): the first ACE - SYSTEM, FullControl - set the result to $true, which
    # OVERWROTE the mask, and `[int]$true` is 1. Every later ACE was then tested against bit 0x1 (ReadData),
    # which ReadAndExecute contains, so the check threw on the first slot identity and could never pass on a
    # correctly configured golden image. Measured on the host: the mask read 0x1 where it should read
    # 0xD0156. Case is not a namespace - the two names below are distinct words.
    $hasWriteRight = (($raw -band [int]$WriteMask) -ne 0) -or
                     (($raw -band (0x10000000 -bor 0x40000000)) -ne 0)
    if ($hasWriteRight -and ($GOLDEN_WRITERS -notcontains $who)) {
      throw "golden image: '$who' holds write-class rights ($($r.FileSystemRights)) - unexpected writable principal - STOP"
    }
    if ($who -eq "S-1-5-32-544") { $adminSeen = $true }
    if ($who -eq "S-1-5-18")     { $systemSeen = $true }
    # (4) CREATOR OWNER must not survive as an inheritable grant: anything a slot identity created would
    #     otherwise be owned by it with full control.
    if ($who -eq "S-1-3-0" -and $hasWriteRight) {
      throw "golden image: CREATOR OWNER still grants write-class rights - STOP"
    }
  }
  Write-Host "ok   golden: no principal outside Administrators/SYSTEM holds Write, CreateFiles, AppendData, Delete or ChangePermissions"
  # (6) Administrators and SYSTEM retain control - the operator must not lock themselves out.
  if (-not $adminSeen)  { throw "golden image: BUILTIN\Administrators has NO ACE after the inheritance break - STOP" }
  if (-not $systemSeen) { throw "golden image: NT AUTHORITY\SYSTEM has NO ACE after the inheritance break - STOP" }
  Write-Host "ok   golden: Administrators and SYSTEM retain full control"
  # (7) the image itself is unchanged - ACL work must not have touched content.
  # This MUST reproduce win_slot_ops.tree_digest() byte for byte, because BETA_AGENT_GOLDEN_DIGEST is
  # consumed by stage_copy's source_digest_matches pre-check and a mismatch BLOCKS every MATERIALISE.
  #   normalise(p) = p.replace("/", "\").rstrip("\").lower()
  #   line         = "{normalised_relpath}|{size}|{sha256}\n"
  #   body         = "".join(lines sorted ORDINALLY by normalised relpath)
  # An earlier version used forward slashes and Sort-Object FullName (culture-aware). It produced a
  # different hex string for the same clean image - so the installer would have reported the image proven
  # while the agent refused to stage it, permanently, after the passwords had been entered.
  $goldenFiles = @(Get-ChildItem $GoldenDir -Recurse -File -Force -ErrorAction SilentlyContinue)
  $rows = foreach ($f in $goldenFiles) {
    $rel = $f.FullName.Substring($GoldenDir.Length + 1).Replace("/","\").TrimEnd("\").ToLower()
    [pscustomobject]@{ Key = $rel
                       Line = ("{0}|{1}|{2}`n" -f $rel, $f.Length,
                               (Get-FileHash $f.FullName -Algorithm SHA256).Hash.ToLower()) }
  }
  # Ordinal sort, matching Python's sorted() on the same keys. Sort-Object is culture-aware even with
  # -CaseSensitive, so the comparison is done explicitly.
  $keys = [string[]]($rows | ForEach-Object { $_.Key })
  [Array]::Sort($keys, [System.StringComparer]::Ordinal)
  $byKey = @{}; foreach ($r in $rows) { $byKey[$r.Key] = $r.Line }
  $sb = New-Object Text.StringBuilder
  foreach ($k in $keys) { [void]$sb.Append($byKey[$k]) }
  $sha = [Security.Cryptography.SHA256]::Create()
  $treeDigest = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sb.ToString()))).Replace("-","").ToLower()
  Write-Host "ok   golden: $($goldenFiles.Count) files, tree digest $treeDigest"
  Write-Host "     this is the value BETA_AGENT_GOLDEN_DIGEST must hold; a difference means the image changed - STOP"
  for ($n = 1; $n -le $PoolSize; $n++) {
    $user = "$IdentityPrefix$n"
    $slot = Join-Path $SlotsRoot "$n"
    $slotRules = @((Get-Acl $slot).Access | Where-Object { $_.IdentityReference.Value -like "*\$user" })
    if ($slotRules.Count -eq 0) { throw "'$user' has NO ACE on its own slot '$slot' - the grant did not take - STOP" }
    # It must have access to its OWN slot and to no other slot.
    for ($m = 1; $m -le $PoolSize; $m++) {
      if ($m -eq $n) { continue }
      $other = Join-Path $SlotsRoot "$m"
      $x = @((Get-Acl $other).Access | Where-Object { $_.IdentityReference.Value -like "*\$user" })
      if ($x.Count -gt 0) { throw "'$user' has an ACE on slot $m ('$other') - cross-slot access - STOP" }
    }
    $gRules = @($goldenAcl.Access | Where-Object { $_.IdentityReference.Value -like "*\$user" })
    if ($gRules.Count -eq 0) { throw "'$user' has no ACE on the golden image - MATERIALISE could not read it - STOP" }
    foreach ($r in $gRules) {
      if ($r.FileSystemRights.ToString() -match "Write|Modify|FullControl|CreateFiles|AppendData|Delete") {
        throw "'$user' holds '$($r.FileSystemRights)' on the golden image; Read+Execute only was authorised - STOP"
      }
    }
    Write-Host "ok   $user : Modify on its own slot, no ACE on any other slot, read-only on the golden image"
  }

  # ADR-0016: the launcher is HIGHER-value than the golden image - it is EXECUTED as the slot identity every
  # launch, and nothing re-hashes it at runtime - yet the VERIFY block above never inspected it, so a silent
  # icacls mis-apply or a post-install tamper would leave a slot able to rewrite its own (shared) launcher and
  # -VerifyOnly would still print "pool VERIFIED". Read the launcher ACL back from the OS and re-hash the
  # staged wrapper here, in $Check, so both -Apply and -VerifyOnly certify it (RULE 11: do not trust the grant
  # or the copy step - read back what is on disk).
  Step "VERIFY launcher (ADR-0016: admin-only, slots read-execute only, wrapper hash pinned)"
  $launcherAcl = Get-Acl $LauncherDir
  if (-not $launcherAcl.AreAccessRulesProtected) {
    throw "launcher '$LauncherDir' still inherits: the admin-only guarantee is NOT in force - STOP"
  }
  Write-Host "ok   launcher: inheritance removed (AreAccessRulesProtected = True)"
  # No principal outside Administrators (S-1-5-32-544) and SYSTEM (S-1-5-18) may hold ANY write-class bit -
  # checked as RIGHTS BITS (the same $WriteMask the golden read-back uses), never a name/string match.
  $launcherSidRules = $launcherAcl.GetAccessRules($true, $true,
                        [System.Security.Principal.SecurityIdentifier])
  foreach ($r in $launcherSidRules) {
    if ($r.AccessControlType -ne "Allow") { continue }
    $raw = [int]$r.FileSystemRights
    $ff  = [int][System.Security.AccessControl.FileSystemRights]::FullControl
    $hasWrite = (($raw -band [int]$WriteMask) -ne 0) -or (($raw -band $ff) -eq $ff)
    $sidV = $r.IdentityReference.Value
    if ($hasWrite -and $sidV -ne "S-1-5-32-544" -and $sidV -ne "S-1-5-18") {
      throw "launcher '$LauncherDir': '$sidV' holds a write-class right; only Administrators/SYSTEM may write - STOP"
    }
  }
  Write-Host "ok   launcher: no principal outside Administrators/SYSTEM holds a write-class right"
  # Each slot identity must hold an ACE (ReadAndExecute) and NO write bit - it runs the wrapper, never edits it.
  for ($n = 1; $n -le $PoolSize; $n++) {
    $user = "$IdentityPrefix$n"
    $lr = @($launcherAcl.Access | Where-Object { $_.IdentityReference.Value -like "*\$user" })
    if ($lr.Count -eq 0) { throw "'$user' has NO ACE on the launcher '$LauncherDir' - it cannot run its own wrapper - STOP" }
    foreach ($r in $lr) {
      if ($r.FileSystemRights.ToString() -match "Write|Modify|FullControl|CreateFiles|AppendData|Delete") {
        throw "'$user' holds '$($r.FileSystemRights)' on the launcher; Read+Execute only was authorised - a slot must never rewrite its own launcher - STOP"
      }
    }
  }
  Write-Host "ok   each slot identity: read-execute only on the launcher"
  # Re-hash the STAGED wrapper against the pin (RULE 11: read back on-disk bytes; do not trust the copy step).
  if (-not (Test-Path -LiteralPath $WrapperDest -PathType Leaf)) {
    throw "launch wrapper missing from '$LauncherDir' - STOP"
  }
  $stagedHash = (Get-FileHash -LiteralPath $WrapperDest -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($stagedHash -ne $LaunchWrapperSha256) {
    throw "staged launch wrapper hash '$stagedHash' != pinned '$LaunchWrapperSha256' - the wrapper was altered - STOP"
  }
  Write-Host "ok   staged launch wrapper matches the pinned hash"

  # TSV: the service must hold read+execute (exactly 0x1200a9) on each of the eight beta tasks and NOTHING
  # broader - so it can find and run them by exact name but cannot modify or delete them. Read every task's
  # DACL back from the OS (both -Apply and -VerifyOnly). A missing, broadened or write-bearing service ACE is
  # a hard stop; production/estate tasks are never inspected or touched.
  Step "VERIFY task ACLs (service read+execute on the eight beta tasks, nothing broader)"
  $svcSidV = Get-GuvfxServiceSidValue "GuvFXBetaAgent"
  $svcSidObj = New-Object System.Security.Principal.SecurityIdentifier($svcSidV)
  $svcTaskV = New-Object -ComObject Schedule.Service; $svcTaskV.Connect()
  for ($n = 1; $n -le $PoolSize; $n++) {
    foreach ($t in @("$LaunchPrefix$n", "$StopPrefix$n")) {
      $sdV = New-Object System.Security.AccessControl.RawSecurityDescriptor(($svcTaskV.GetFolder("\").GetTask($t)).GetSecurityDescriptor(7))
      $aces = @($sdV.DiscretionaryAcl | Where-Object { $_.SecurityIdentifier -eq $svcSidObj })
      if ($aces.Count -ne 1) { throw "task '$t': expected exactly ONE service ACE, found $($aces.Count) - STOP" }
      # EXACT-equality to read+execute (0x1200A9) is the whole assertion: any write/delete/change-permission
      # bit would make the mask differ, so a broadened grant is a hard stop by construction.
      $m = [int]$aces[0].AccessMask
      if ($m -ne 0x1200a9) { throw ("task '$t': service ACE mask 0x{0:X}, expected read+execute 0x1200A9 - STOP" -f $m) }
      Write-Host "ok   $t : service holds read+execute only (0x1200A9), no write/delete"
    }
  }
  # Defense in depth: the service must hold NO ACE on the ROOT task FOLDER. A folder-level grant would let it
  # ENUMERATE every task on the host (incl. production/estate) - the opposite of the exact-name least-privilege
  # design. Read the folder DACL back and hard-stop on any service ACE.
  $rootSd = New-Object System.Security.AccessControl.RawSecurityDescriptor(($svcTaskV.GetFolder("\").GetSecurityDescriptor(7)))
  $rootSvcAces = @($rootSd.DiscretionaryAcl | Where-Object { $_.SecurityIdentifier -eq $svcSidObj })
  if ($rootSvcAces.Count -ne 0) { throw "root task folder carries $($rootSvcAces.Count) service ACE(s); the service must have NO folder-level access - STOP" }
  Write-Host "ok   root task folder: service holds NO ACE (no folder-list; exact-name lookup only)"

  Step "VERIFY estate untouched (compared against the pre-mutation capture)"
  foreach ($t in $ForbiddenTasks) {
    $before = $EstateBefore[$t]
    $e = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($null -eq $before) {
      if ($e) { throw "estate task '$t' appeared during the install - refusing to report a clean run" }
      Write-Host "ok   estate task '$t' absent before and after"
      continue
    }
    if (-not $e) { throw "estate task '$t' was present before the install and is GONE - STOP" }
    if ([string]$e.Principal.UserId -ne [string]$before.principal) {
      throw "estate task '$t' principal changed from '$($before.principal)' to '$($e.Principal.UserId)' - STOP"
    }
    $note = if ([string]$e.State -ne [string]$before.state) {
      " (state $($before.state) -> $($e.State); GuvFX_BridgeWatchdog and GuvFX_SignalBridge change state on their own - not proof of interference)"
    } else { " (state $($e.State), unchanged)" }
    Write-Host ("ok   estate task '$t' still present, principal unchanged" + $note)
  }
  Write-Host ""
  if ($VerifyOnly) {
    Write-Host "ok   pool VERIFIED. Nothing was created, changed or started by this run."
  } else {
    Write-Host "ok   pool provisioned. Tasks are ENABLED but TRIGGERLESS (on-demand only, ADR 0017). Nothing has been started, triggered or staged."
    Write-Host "     Next: install_service.ps1 -Apply, then firewall.ps1 -Apply. Do NOT start until approval."
  }
} else {
  Write-Host ""
  Write-Host "PLAN complete. Re-run with -Apply on the host to provision the pool (install-only, no start)."
}

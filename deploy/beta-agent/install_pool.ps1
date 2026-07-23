# CVM-Inc-3 B3P-2 - provision the pre-provisioned SLOT POOL: identities, rights, directories, ACLs, tasks.
# DARK ARTEFACT: RUN ONLY on the host, as Administrator, AFTER the Install Authorisation gate.
# INSTALL-ONLY: it creates objects and stops. It does NOT start the service, does NOT enable any task, does
# NOT trigger anything, does NOT launch MT5, does NOT stage a runtime into a slot, and never touches
# Session 3 / the prod terminal / the bridge (:8788) / :8787 / autologon / any GuvFX_* task.
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
  [switch]$Apply,
  # Run ONLY the golden-image validation and exit. Read-only by construction: it reaches no identity,
  # right, ACL, task or directory-creation step.
  [switch]$ValidateGoldenOnly
)
$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "==> $m" }
function DoIt($desc, [scriptblock]$block) {
  if ($Apply) { Step "APPLY: $desc"; & $block } else { Step "PLAN:  $desc" }
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
foreach ($p in @($SlotsRoot, $TombstonesRoot)) {
  if ($p -notlike "C:\GuvFX\beta\*") { throw "refusing: '$p' is outside C:\GuvFX\beta\" }
}
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
      $text = [Text.Encoding]::ASCII.GetString($b) + "`n" + [Text.Encoding]::Unicode.GetString($b)
      foreach ($fp in $foreign) {
        if ($text -match [regex]::Escape($fp.Pattern)) {
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
    $pa = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
    $pb = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
    $same = ($pa -ceq $pb)
    Remove-Variable pa, pb
    if ($same) { $Secrets[$user] = $a; return $a }
    Write-Host "     passwords did not match; try again"
  }
}

for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  if (Get-LocalUser -Name $user -ErrorAction SilentlyContinue) {
    Write-Host "note identity '$user' already exists; leaving as-is (its password is still needed below)"
    continue
  }
  DoIt "create non-admin identity '$user' (password prompted, never a parameter)" {
    New-LocalUser -Name $user -Password (Get-SlotSecret $user) -PasswordNeverExpires -AccountNeverExpires `
                  -Description "GuvFX beta slot $n runtime identity" | Out-Null
    Add-LocalGroupMember -Group "Users" -Member $user
  }
}
# Assert group membership in BOTH directions: in Users, and in nothing privileged.
if ($Apply) {
  for ($n = 1; $n -le $PoolSize; $n++) {
    $user = "$IdentityPrefix$n"
    foreach ($g in @("Administrators","Remote Desktop Users","Backup Operators")) {
      $m = Get-LocalGroupMember -Group $g -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -like "*\$user" }
      if ($m) { throw "identity '$user' is a member of '$g' - refusing to continue" }
    }
    Write-Host "ok   $user is non-admin"
  }
}


# -- 3. Grant SeBatchLogonRight to each slot identity.
Step "SeBatchLogonRight via the LSA policy API (adds one right to one account; no policy line is rewritten)"
for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  if ($Apply) {
    Grant-GuvfxBatchLogonRight -AccountName $user
  } else {
    # PLAN reports the delta without modifying anything. Note honestly what it does and does not prove:
    # on a FRESH host the accounts do not exist yet, so the enumerate path cannot be exercised and only the
    # self-test above (LsaOpenPolicy/LsaClose) has entered advapi32. The enumerate marshalling is first
    # exercised on a re-run of PLAN once the accounts exist, or at APPLY - which is why the interop
    # self-test runs before any account is created, so a broken interop costs nothing.
    if (Get-LocalUser -Name $user -ErrorAction SilentlyContinue) {
      $held = Get-GuvfxAccountRights -AccountName $user
      $verb = if ($held -contains $GuvfxRight) { "already holds (no change)" } else { "WOULD ADD" }
      Write-Host "PLAN:  $user $verb $GuvfxRight; currently holds $($held.Count) right(s): $($held -join ',')"
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
  icacls "C:\GuvFX\beta" /inheritance:r | Out-Null
  icacls "C:\GuvFX\beta" /grant "*S-1-5-32-544:(OI)(CI)F" /grant "*S-1-5-18:(OI)(CI)F" | Out-Null
}
for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  $slot = Join-Path $SlotsRoot "$n"
  DoIt "grant '$user' Modify on $slot only" { icacls $slot /grant ("{0}:(OI)(CI)M" -f $user) | Out-Null }
  DoIt "grant '$user' ReadAndExecute on $GoldenDir" { icacls $GoldenDir /grant ("{0}:(OI)(CI)RX" -f $user) | Out-Null }
}
DoIt "restrict $TombstonesRoot to Administrators + SYSTEM" {
  icacls $TombstonesRoot /inheritance:r | Out-Null
  icacls $TombstonesRoot /grant "*S-1-5-32-544:(OI)(CI)F" /grant "*S-1-5-18:(OI)(CI)F" | Out-Null
}

# -- 6. Scheduled tasks. Registered DISABLED, with NO trigger, on-demand only. TASK_LOGON_PASSWORD stores
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

  DoIt "register '$launch' (disabled, no trigger, /portable, runs as $user)" {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
               [Runtime.InteropServices.Marshal]::SecureStringToBSTR((Get-SlotSecret $user)))
    $action    = New-ScheduledTaskAction -Execute $exe -Argument "/portable" -WorkingDirectory $work
    $settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
                   -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $launch -Action $action -Settings $settings `
      -User $user -Password $plain -RunLevel Limited -Force | Out-Null
    Disable-ScheduledTask -TaskName $launch | Out-Null
    Remove-Variable plain
  }
  DoIt "register '$stop' (disabled, no trigger, terminates ONLY this slot's image)" {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
               [Runtime.InteropServices.Marshal]::SecureStringToBSTR((Get-SlotSecret $user)))
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
    Register-ScheduledTask -TaskName $stop -Action $action -Settings $settings `
      -User $user -Password $plain -RunLevel Limited -Force | Out-Null
    Disable-ScheduledTask -TaskName $stop | Out-Null
    Remove-Variable plain
  }

  # The approved definition the agent's launch gate asserts against. Read back through the SAME COM
  # interface the agent uses (Schedule.Service), not from the values we intended: Task Scheduler may
  # normalise the principal to a qualified form or a SID, and the gate compares the 7-field digest by exact
  # equality. Pinning what we MEANT to register, while the agent reads what IS registered, would make every
  # first launch fail task_definition_drift - permanently, since the agent never repairs a task.
  if ($Apply) {
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    $reg = $svc.GetFolder("\").GetTask($launch)
    $p   = $reg.Definition.Principal
    $act = $reg.Definition.Actions.Item(1)
    $Approved[$launch] = [ordered]@{
      task_name         = [string]$reg.Name
      run_as_identity   = [string]$p.UserId
      executable        = [string]$act.Path
      working_directory = [string]$act.WorkingDirectory
      arguments         = [string]$act.Arguments
      logon_type        = [int]$p.LogonType
      run_level         = [int]$p.RunLevel
      # The gate runs at FIRST START, by which point the operator has enabled the tasks under a separate
      # approval - so the approval pins enabled=true even though the task is disabled right now.
      enabled           = $true
    }
    if ($Approved[$launch].arguments -notmatch "(^|\s)/portable(\s|$)") {
      throw "registered task '$launch' does not carry /portable - refusing to approve it"
    }
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
  icacls $ApprovedTasksOut /inheritance:r | Out-Null
  # The service account needs READ - it loads this file at startup and refuses to start without it. Read
  # only: the agent must never be able to rewrite its own approvals. Inheritance is stripped, so the
  # inheritable grant on the state dir cannot reach this file; the ACE has to be explicit.
  icacls $ApprovedTasksOut /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F" `
                           /grant "NT SERVICE\GuvFXBetaAgent:(R)" | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "icacls failed on $ApprovedTasksOut (exit $LASTEXITCODE)" }
}

# -- 8. Verify (no start, no trigger, no enable).
if ($Apply) {
  Step "VERIFY pool (expect: identities non-admin, tasks present and DISABLED, no triggers)"
  for ($n = 1; $n -le $PoolSize; $n++) {
    foreach ($t in @("$LaunchPrefix$n", "$StopPrefix$n")) {
      $task = Get-ScheduledTask -TaskName $t -ErrorAction Stop
      if ($task.State -ne "Disabled") { throw "task '$t' is $($task.State); expected Disabled (install-only)" }
      if (@($task.Triggers).Count -gt 0) { throw "task '$t' has a trigger; expected on-demand only" }
      $principal = $task.Principal
      if ($principal.UserId -notlike "*$IdentityPrefix$n") { throw "task '$t' principal is $($principal.UserId)" }
      if ($principal.RunLevel -ne "Limited") { throw "task '$t' RunLevel is $($principal.RunLevel); expected Limited" }
      Write-Host "ok   $t disabled, no trigger, principal $($principal.UserId), RunLevel Limited"
    }
  }
  Step "VERIFY estate untouched"
  foreach ($t in $ForbiddenTasks) {
    $e = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($e) { Write-Host "ok   estate task '$t' present and untouched (state $($e.State))" }
  }
  Write-Host ""
  Write-Host "ok   pool provisioned. Tasks are DISABLED. Nothing has been started, triggered or staged."
  Write-Host "     Next: install_service.ps1 -Apply, then firewall.ps1 -Apply. Do NOT start until approval."
} else {
  Write-Host ""
  Write-Host "PLAN complete. Re-run with -Apply on the host to provision the pool (install-only, no start)."
}

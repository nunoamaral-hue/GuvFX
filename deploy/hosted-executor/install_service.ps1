# Stream 7C - install the GuvFX hosted signed executor as a real Windows service via a WinSW WRAPPER.
# THE SINGLE SANCTIONED INSTALL MECHANISM (security RULE 1: never Start-Process/nohup a production listener over
# SSH - an ad-hoc process is session-bound, unsupervised, and dies when its launcher ends). Two explicit profiles:
#
#   -InstallProfile Dark        install-only: Manual start, recovery=none, no supervision. Registering it
#                               changes nothing until an operator starts it.
#   -InstallProfile Supervised  production target: Automatic + delayed start, bounded 3-tier onfailure=restart,
#                               launch-proof env marker, rolling logs, graceful drain.
#
# The profile is EXPLICIT and MANDATORY - no inference, no auto-detection.
#
# DARK ARTEFACT: RUN ONLY on the host, as Administrator, AFTER the bundle + primitives are staged and the
# HOSTED_EXECUTOR_* machine secrets are provisioned. INSTALL-ONLY for BOTH profiles: it does NOT start the
# service, does NOT touch Customer Zero / the beta agent (:8791) / the trade bridge (:8788) / port 3389. Dry-run
# by default; pass -Apply to perform the install. The first manual start waits for explicit approval.
#
# IDENTITY (WinSW v2.12.0 installs LocalSystem regardless of <serviceaccount>): this installer assigns the
# identity AFTER install via `sc config obj=`, VERIFIES SERVICE_START_NAME, and ROLLS BACK otherwise. The DEFAULT
# (Sponsor-approved, ADR-0040) identity is LocalSystem: the reviewed provisioning primitives require admin/SYSTEM
# capability (create local user, NTFS ACL, RDP/RemoteApp), and the security boundary is the SIGNED protocol +
# allow-listed primitives + Customer-Zero refusal, NOT the OS token. LocalSystem is a built-in that already holds
# service-logon, so NO SeServiceLogonRight grant is needed. The least-privilege virtual account
# `NT SERVICE\GuvFXHostedExecutor` remains SUPPORTED via -RunAsUser (it still gets the `sc config obj=` + LSA
# SeServiceLogonRight grant). A bare `winsw install` is never sanctioned.
#
# SCRIPTS (RULE 9): every reviewed provisioning primitive under -ScriptsDir is ParseFile-validated here, before
# first start. A parse failure is a hard refusal (the daemon also ParseFile-gates at startup - defence in depth).
#
# SECRETS: the HMAC keyring (HOSTED_EXECUTOR_KEYRING / _KEY_ID) and the envelope private keyring
# (HOSTED_EXECUTOR_ENC_PRIVKEYS) are provisioned as MACHINE environment variables by the operator BEFORE first
# start; they are never in this script, the XML, or the logs.
param(
  [Parameter(Mandatory=$true)][ValidateSet("Dark","Supervised")][string]$InstallProfile,
  [string]$ServiceName = "GuvFXHostedExecutor",
  [string]$ExecutorDir = "C:\GuvFX\hosted\executor",
  [string]$StateDir    = "C:\GuvFX\hosted\executor-state",
  [string]$ScriptsDir  = "C:\GuvFX\hosted\scripts",
  [string]$Python      = "C:\GuvFX\hosted\executor-venv\Scripts\python.exe",  # dedicated venv; NOT the base
  [string]$RunAsUser   = "LocalSystem",                                       # ADR-0040 default; built-in, no password, inherent service-logon
  [string]$SupervisedToken = "",
  [string]$WinSwSource = "C:\GuvFX\hosted\winsw-src\WinSW.NET4.exe",
  [string]$WinSwSha256 = "923111c7142b3dc783a3c722b19b8a21bcb78222d7a136ac33f0ca8a29f4cb66",  # WinSW v2.12.0 NET4
  [string]$WinSwDir    = "C:\GuvFX\hosted\executor-winsw",
  [switch]$Apply
)
$ErrorActionPreference = "Stop"
$ServiceExe = Join-Path $WinSwDir "$ServiceName.exe"     # WinSW pairs by basename: <name>.exe + <name>.xml
$ServiceXml = Join-Path $WinSwDir "$ServiceName.xml"
$XmlSourceName = if ($InstallProfile -eq "Supervised") { "$ServiceName.supervised.xml" } else { "$ServiceName.xml" }
$XmlSource  = Join-Path $ExecutorDir "winsw\$XmlSourceName"
$PlaceholderToken = "__SET_AT_INSTALL__"
if ($RunAsUser -ne "LocalSystem" -and $RunAsUser -ne "NT SERVICE\GuvFXHostedExecutor") {
  throw "refusing: -RunAsUser '$RunAsUser' must be exactly 'LocalSystem' (ADR-0040 default) or 'NT SERVICE\GuvFXHostedExecutor' (least-privilege virtual account)"
}
$IsLocalSystem = ($RunAsUser -eq "LocalSystem")

# The reviewed provisioning primitives the daemon runs. Kept in sync with primitive_runner.CONTRACT.
$RequiredScripts = @(
  "Provision-GuvfxAccount.ps1", "Set-GuvfxWorkspaceAcl.ps1", "Populate-GuvfxViewerRuntime.ps1",
  "Set-GuvfxAutoTradingConfig.ps1", "Grant-GuvfxRdpAccess.ps1", "Set-GuvfxSingleSession.ps1",
  "Set-GuvfxRemoteApp.ps1", "Set-GuvfxObserver.ps1", "Set-GuvfxAppLockerTenant.ps1"
)

function Write-HxInstallLog {
  param([Parameter(Mandatory)][ValidateSet("START","PRECHECK","STAGE","INSTALL","IDENTITY","VERIFY",
                    "ROLLBACK","SUCCESS","FAIL")][string]$InstStep,
        [Parameter(Mandatory)][string]$Result, [string]$Detail = "")
  $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  $x  = if ($Detail) { " detail=$Detail" } else { "" }
  Write-Host "install_evidence ts=$ts profile=$InstallProfile step=$InstStep result=$Result$x"
}
function Step($m) { Write-Host "==> $m" }
function DoIt($desc, [scriptblock]$block) {
  if ($Apply) { Step "APPLY: $desc"; & $block } else { Step "PLAN:  $desc" }
}
Write-HxInstallLog -InstStep START -Result begin -Detail "apply=$([bool]$Apply)"

# 0. Preconditions (both dry-run and apply)
if (-not (Test-Path (Join-Path $ExecutorDir "daemon.py")))            { throw "daemon.py not found under $ExecutorDir - stage the bundle first" }
if (-not (Test-Path (Join-Path $ExecutorDir "lib\broker_cred_envelope.py"))) { throw "lib\broker_cred_envelope.py not staged under $ExecutorDir" }
if (-not (Test-Path (Join-Path $ExecutorDir "lib\hosted_workspace\host_agent_dispatch.py"))) { throw "lib\hosted_workspace\host_agent_dispatch.py not staged (from backend/hosted_workspace)" }
if (-not (Test-Path $XmlSource)) { throw "WinSW config not found: $XmlSource (profile $InstallProfile bundle incomplete)" }
if (-not (Test-Path $ScriptsDir)) { throw "scripts dir not found: $ScriptsDir - stage the reviewed primitives first" }

# Interpreter identity BY METADATA before it is ever executed (the candidate is executed only under -Apply).
function Test-HxInterpreterIdentity {
  param([Parameter(Mandatory)][string]$Path)
  if (-not (Test-Path $Path)) { throw "interpreter not found: $Path (create the executor venv first)" }
  if ((Get-Item $Path -Force).PSIsContainer) { throw "interpreter path is a directory: $Path" }
  $vi = (Get-Item $Path -Force).VersionInfo
  $orig = [string]$vi.OriginalFilename
  if ($orig -match '(?i)^python-.*\.exe$' -or $orig -match '(?i)\.msi$') {
    throw "refusing: '$Path' is the Python INSTALLER (OriginalFilename '$orig'), not an interpreter"
  }
  if ($orig -notmatch '(?i)^(python|pythonw|py|pyw)\.exe$') {
    throw "refusing: '$Path' OriginalFilename is '$orig'; expected a CPython interpreter or venv shim"
  }
  Write-Host "ok   interpreter identity (metadata, not executed): OriginalFilename '$orig' $($vi.FileVersion)"
}
function Test-HxInterpreterRuntime {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ExecutorDir)
  $ver = & $Path --version 2>&1
  if ($LASTEXITCODE -ne 0 -or "$ver" -notmatch '(?i)^Python 3\.') {
    throw "interpreter '$Path' did not report a Python 3 version (got '$ver', exit $LASTEXITCODE)"
  }
  # daemon_config/nonce_store/primitive_runner are stdlib-only; importing them proves interpreter+bundle
  # coherence. The full graph (cryptography + hosted_workspace) is exercised by the daemon at first start.
  & $Path -c "import sys; sys.path.insert(0, r'$ExecutorDir'); import daemon_config, nonce_store, primitive_runner" 2>$null
  if ($LASTEXITCODE -ne 0) { throw "the executor's own stdlib modules do not import under '$Path'" }
  Write-Host "ok   interpreter runtime: $ver, executor bundle imports"
}
Test-HxInterpreterIdentity -Path $Python
if ($Apply) { Test-HxInterpreterRuntime -Path $Python -ExecutorDir $ExecutorDir }
else { Write-Host "PLAN:  interpreter runtime check DEFERRED to -Apply (PLAN never executes a candidate binary)" }

# RULE 9: ParseFile-validate every reviewed primitive under -ScriptsDir. Uses the AST parser WITHOUT executing.
function Test-HxScriptParses {
  param([Parameter(Mandatory)][string]$Path)
  $errs = $null; $toks = $null
  [void][System.Management.Automation.Language.Parser]::ParseFile($Path, [ref]$toks, [ref]$errs)
  if ($errs -and $errs.Count -gt 0) {
    throw "RULE 9: '$Path' failed to parse ($($errs.Count) error(s); first: $($errs[0].Message))"
  }
}
foreach ($s in $RequiredScripts) {
  $sp = Join-Path $ScriptsDir $s
  if (-not (Test-Path $sp)) { throw "required primitive missing under $ScriptsDir : $s" }
  Test-HxScriptParses -Path $sp
}
Write-Host "ok   all $($RequiredScripts.Count) reviewed primitives present and ParseFile-valid under $ScriptsDir"

# WinSW wrapper: identity by PINNED HASH before it is ever run.
function Test-HxWinSw {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ExpectSha256)
  if (-not (Test-Path $Path)) { throw "WinSW wrapper not found: $Path - place the pinned WinSW.NET4.exe there first (operator-gated)" }
  $got = (Get-FileHash $Path -Algorithm SHA256).Hash.ToLower()
  if ($got -ne $ExpectSha256.ToLower()) {
    throw "WinSW hash mismatch at $Path : got $got, pinned $ExpectSha256 - REFUSING an unverified executable"
  }
  Write-Host "ok   WinSW wrapper verified by pinned SHA-256 ($ExpectSha256)"
}
Test-HxWinSw -Path $WinSwSource -ExpectSha256 $WinSwSha256
Write-HxInstallLog -InstStep PRECHECK -Result ok -Detail "xml=$XmlSourceName scripts=$($RequiredScripts.Count)"

# XML contract validation - profile-aware. The service runs whatever the XML says; bind it to the validated
# interpreter + daemon.py, require stoptimeout > drain, and the profile-specific startmode/recovery invariants.
function Test-HxWinSwXmlContract {
  param([Parameter(Mandatory)][string]$XmlPath, [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$ExecutorDir, [Parameter(Mandatory)][string]$InstallProfile,
        [switch]$Staged)
  [xml]$doc = Get-Content -Raw -Path $XmlPath
  $svc = $doc.service
  if ("$($svc.executable)" -ne $Python) {
    throw "XML <executable> '$($svc.executable)' != validated -Python '$Python'"
  }
  $daemonPy = (Join-Path $ExecutorDir "daemon.py")
  if ("$($svc.arguments)" -notmatch [regex]::Escape($daemonPy)) {
    throw "XML <arguments> '$($svc.arguments)' does not run '$daemonPy' under -ExecutorDir"
  }
  $stopRaw = "$($svc.stoptimeout)"
  if ($stopRaw -match '^\s*(\d+)\s*sec\s*$') { $stopS = [int]$Matches[1] } else { throw "XML <stoptimeout> '$stopRaw' is not '<N> sec'" }
  $drainRaw = [Environment]::GetEnvironmentVariable("HOSTED_EXECUTOR_DRAIN_TIMEOUT_S", "Machine")
  $drainS = 630; if ($drainRaw -and ($drainRaw -match '^\s*\d+(\.\d+)?\s*$')) { $drainS = [int][math]::Ceiling([double]$drainRaw) }
  if ($stopS -le $drainS) {
    throw "XML <stoptimeout> ${stopS}s must EXCEED HOSTED_EXECUTOR_DRAIN_TIMEOUT_S (${drainS}s) or a stop force-kills a mutation mid-drain"
  }
  $of = @($svc.onfailure)
  if ($InstallProfile -eq "Dark") {
    if ($of.Count -ne 1 -or "$($of[0].action)" -ne "none") { throw "DARK XML recovery must be a single onfailure action=none; found $($of.Count)" }
    if ("$($svc.startmode)" -ne "Manual") { throw "DARK XML <startmode> is '$($svc.startmode)', expected Manual" }
    Write-Host "ok   DARK XML contract: runs '$Python' on daemon.py; recovery=none; stoptimeout ${stopS}s > drain ${drainS}s; startmode Manual"
  } else {
    if ("$($svc.startmode)" -ne "Automatic") { throw "SUPERVISED XML <startmode> is '$($svc.startmode)', expected Automatic" }
    if ("$($svc.delayedAutoStart)" -ne "true") { throw "SUPERVISED XML <delayedAutoStart> is '$($svc.delayedAutoStart)', expected true" }
    if ($of.Count -lt 3) { throw "SUPERVISED XML needs >=3 <onfailure> restart tiers; found $($of.Count)" }
    foreach ($o in $of) { if ("$($o.action)" -ne "restart") { throw "SUPERVISED XML <onfailure> action '$($o.action)' must be restart (all tiers)" } }
    if (-not "$($svc.resetfailure)") { throw "SUPERVISED XML missing <resetfailure>" }
    $envNames = @(@($svc.env) | ForEach-Object { [string]$_.name })
    foreach ($need in @("HOSTED_EXECUTOR_SERVICE_IDENTITY","HOSTED_EXECUTOR_SUPERVISED_TOKEN")) {
      if ($envNames -notcontains $need) { throw "SUPERVISED XML missing required <env name='$need'>" }
    }
    if ($Staged) {
      $tok = ""; foreach ($e in @($svc.env)) { if ("$($e.name)" -eq "HOSTED_EXECUTOR_SUPERVISED_TOKEN") { $tok = "$($e.value)" } }
      if (-not $tok -or $tok -eq $PlaceholderToken) { throw "STAGED SUPERVISED XML still has an unsubstituted HOSTED_EXECUTOR_SUPERVISED_TOKEN" }
      if ((Get-Content -Raw -Path $XmlPath) -match [regex]::Escape($PlaceholderToken)) { throw "STAGED SUPERVISED XML still contains the '$PlaceholderToken' placeholder" }
    }
    Write-Host "ok   SUPERVISED XML contract: runs '$Python' on daemon.py; Automatic+delayed; $($of.Count) restart tiers; resetfailure present; stoptimeout ${stopS}s > drain ${drainS}s"
  }
}
Test-HxWinSwXmlContract -XmlPath $XmlSource -Python $Python -ExecutorDir $ExecutorDir -InstallProfile $InstallProfile

# LSA interop for the SERVICE-LOGON right (WinSW v2.12 does not apply <serviceaccount>; identity is corrected
# post-install by `sc config obj=` and the account is granted SeServiceLogonRight here). Copied verbatim from the
# host-proven beta-agent installer, renamed to avoid a type collision.
try {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GuvfxHxLsa {
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
  public static extern uint LsaEnumerateAccountRights(IntPtr handle, byte[] sid, out IntPtr rights, out uint count);
  [DllImport("advapi32.dll")] public static extern uint LsaClose(IntPtr handle);
  [DllImport("advapi32.dll")] public static extern uint LsaFreeMemory(IntPtr buffer);
  [DllImport("advapi32.dll")] public static extern int LsaNtStatusToWinError(uint status);
}
'@ -ErrorAction Stop
} catch { if ("$_" -notmatch 'already exists') { throw } }
$SvcLogonRight = "SeServiceLogonRight"
$LSA_READ_SVC  = 0x00000801
$LSA_WRITE_SVC = 0x00000811
$STATUS_NAME_NOT_FOUND_SVC = [uint32]3221225524
function Get-HxServiceSid {
  param([Parameter(Mandatory)][string]$ServiceName)
  if ($ServiceName -ne "GuvFXHostedExecutor") { throw "refusing service SID lookup for '$ServiceName'" }
  $m = (& sc.exe showsid $ServiceName) | Select-String -Pattern "SERVICE SID:\s*(S-1-5-80-\S+)"
  if (-not $m) { throw "could not compute the service SID for '$ServiceName'" }
  $v = $m.Matches.Groups[1].Value
  if ($v -notmatch "^S-1-5-80-\d+-\d+-\d+-\d+-\d+$") { throw "refusing: '$v' is not a service SID" }
  return $v
}
function Get-HxServiceSidBytes {
  param([Parameter(Mandatory)][string]$ServiceSid)
  $s = New-Object System.Security.Principal.SecurityIdentifier($ServiceSid)
  $b = New-Object byte[] $s.BinaryLength
  $s.GetBinaryForm($b, 0)
  return $b
}
function Open-HxLsaPolicy {
  param([uint32]$Access)
  $oa = New-Object GuvfxHxLsa+LSA_OBJECT_ATTRIBUTES
  $oa.Length = [Runtime.InteropServices.Marshal]::SizeOf($oa)
  $h = [IntPtr]::Zero
  $st = [GuvfxHxLsa]::LsaOpenPolicy([IntPtr]::Zero, [ref]$oa, $Access, [ref]$h)
  if ($st -ne 0) { throw "LsaOpenPolicy failed: NTSTATUS 0x$('{0:X8}' -f $st)" }
  return $h
}
function Get-HxSidRights {
  param([Parameter(Mandatory)][string]$ServiceSid)
  $sid = Get-HxServiceSidBytes -ServiceSid $ServiceSid
  $h = Open-HxLsaPolicy -Access $LSA_READ_SVC
  try {
    $ptr = [IntPtr]::Zero; $count = [uint32]0
    $st = [GuvfxHxLsa]::LsaEnumerateAccountRights($h, $sid, [ref]$ptr, [ref]$count)
    if ($st -eq $STATUS_NAME_NOT_FOUND_SVC) { return @() }
    if ($st -ne 0) { throw "LsaEnumerateAccountRights failed: NTSTATUS 0x$('{0:X8}' -f $st)" }
    $out = @()
    $size = [Runtime.InteropServices.Marshal]::SizeOf([type][GuvfxHxLsa+LSA_UNICODE_STRING])
    for ($i = 0; $i -lt $count; $i++) {
      $item = [Runtime.InteropServices.Marshal]::PtrToStructure([IntPtr]($ptr.ToInt64() + ($i * $size)), [type][GuvfxHxLsa+LSA_UNICODE_STRING])
      $out += [Runtime.InteropServices.Marshal]::PtrToStringUni($item.Buffer, $item.Length / 2)
    }
    [void][GuvfxHxLsa]::LsaFreeMemory($ptr)
    return $out
  } finally { [void][GuvfxHxLsa]::LsaClose($h) }
}
function Grant-HxServiceLogonRight {
  param([Parameter(Mandatory)][string]$ServiceSid)
  $before = Get-HxSidRights -ServiceSid $ServiceSid
  if ($before -contains $SvcLogonRight) { Write-Host "evidence right=$SvcLogonRight sid=$ServiceSid op=add result=already_present"; return }
  $sid = Get-HxServiceSidBytes -ServiceSid $ServiceSid
  $h = Open-HxLsaPolicy -Access $LSA_WRITE_SVC
  try {
    $u = New-Object GuvfxHxLsa+LSA_UNICODE_STRING
    $u.Buffer        = [Runtime.InteropServices.Marshal]::StringToHGlobalUni($SvcLogonRight)
    $u.Length        = [uint16]($SvcLogonRight.Length * 2)
    $u.MaximumLength = [uint16](($SvcLogonRight.Length + 1) * 2)
    $arr = @($u)
    $st = [GuvfxHxLsa]::LsaAddAccountRights($h, $sid, $arr, 1)
    [Runtime.InteropServices.Marshal]::FreeHGlobal($u.Buffer)
    if ($st -ne 0) { throw "LsaAddAccountRights failed: NTSTATUS 0x$('{0:X8}' -f $st)" }
  } finally { [void][GuvfxHxLsa]::LsaClose($h) }
  $after = Get-HxSidRights -ServiceSid $ServiceSid
  if ($after -notcontains $SvcLogonRight) { throw "post-check failed: service account still lacks $SvcLogonRight - do NOT start" }
  foreach ($r in $before) { if ($after -notcontains $r) { throw "user-right regression: service account lost '$r'" } }
  Write-Host "evidence right=$SvcLogonRight sid=$ServiceSid op=add result=granted other_rights_preserved=$($before.Count)"
}

# ---- BACKUP / IDENTITY / ROLLBACK ------------------------------------------------------------------------
function Backup-HxServiceState {
  param([Parameter(Mandatory)][string]$ServiceName, [Parameter(Mandatory)][string]$ServiceXml)
  $ci = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
  $snap = @{ Existed = [bool]$ci; StartName = $null; StartMode = $null; XmlBackup = $null }
  if ($ci) {
    $snap.StartName = "$($ci.StartName)"; $snap.StartMode = "$($ci.StartMode)"
    if (Test-Path $ServiceXml) {
      $bkDir = Join-Path (Split-Path $ServiceXml) "_installer_rollback"
      New-Item -ItemType Directory -Force -Path $bkDir | Out-Null
      $snap.XmlBackup = Join-Path $bkDir "$ServiceName.xml.prev"
      Copy-Item $ServiceXml $snap.XmlBackup -Force
    }
  }
  return $snap
}
function Wait-HxServiceRemoved {
  param([Parameter(Mandatory)][string]$ServiceName, [int]$TimeoutSeconds = 20)
  for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
    if (-not (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue)) { return $true }
    Start-Sleep -Seconds 1
  }
  return (-not (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue))
}
function Uninstall-HxServiceVerified {
  param([Parameter(Mandatory)][string]$ServiceName, [Parameter(Mandatory)][string]$ServiceExe)
  & $ServiceExe uninstall 2>&1 | Write-Host
  $rc = $LASTEXITCODE
  if (-not (Wait-HxServiceRemoved -ServiceName $ServiceName)) {
    throw "service '$ServiceName' still registered after uninstall (winsw exit=$rc)"
  }
}
function Assign-HxIdentity {
  param([Parameter(Mandatory)][string]$ServiceName, [Parameter(Mandatory)][string]$RunAsUser,
        [Parameter(Mandatory)][string]$ServiceSid)
  $scOut  = & sc.exe config $ServiceName obj= "$RunAsUser" 2>&1
  $scRc   = $LASTEXITCODE
  $scText = ($scOut | Out-String).Trim()
  Write-Host "evidence sc_config obj='$RunAsUser' exit=$scRc output=$scText"
  if ($scRc -ne 0 -or $scText -notmatch 'ChangeServiceConfig SUCCESS') { throw "sc config obj= failed (exit=$scRc): $scText - do NOT start" }
  # LocalSystem is a built-in that already holds SeServiceLogonRight; only a virtual/user account needs the grant.
  if ($RunAsUser -ne "LocalSystem") { Grant-HxServiceLogonRight -ServiceSid $ServiceSid }
  return "$((Get-CimInstance Win32_Service -Filter "Name='$ServiceName'").StartName)"
}
function Restore-HxServiceFromSnapshot {
  param([Parameter(Mandatory)]$Snapshot, [Parameter(Mandatory)][string]$ServiceName,
        [Parameter(Mandatory)][string]$ServiceExe, [Parameter(Mandatory)][string]$ServiceXml,
        [Parameter(Mandatory)][string]$RunAsUser, [Parameter(Mandatory)][string]$ServiceSid)
  Write-HxInstallLog -InstStep ROLLBACK -Result begin
  if (-not $Snapshot.Existed) {
    try { Uninstall-HxServiceVerified -ServiceName $ServiceName -ServiceExe $ServiceExe }
    catch {
      Write-HxInstallLog -InstStep ROLLBACK -Result FAILED -Detail "removal_unconfirmed"
      throw "ROLLBACK INCOMPLETE: could not confirm removal of the freshly-created '$ServiceName' - MANUAL OPERATOR INTERVENTION REQUIRED"
    }
    Write-HxInstallLog -InstStep ROLLBACK -Result ok -Detail "no_prior_service_removed_confirmed"
    return
  }
  Uninstall-HxServiceVerified -ServiceName $ServiceName -ServiceExe $ServiceExe
  if (-not ($Snapshot.XmlBackup -and (Test-Path $Snapshot.XmlBackup))) {
    Write-HxInstallLog -InstStep ROLLBACK -Result FAILED -Detail "baseline_xml_missing"
    throw "ROLLBACK INCOMPLETE: baseline WinSW XML backup missing - MANUAL OPERATOR INTERVENTION REQUIRED"
  }
  Copy-Item $Snapshot.XmlBackup $ServiceXml -Force
  & $ServiceExe install 2>&1 | Write-Host
  if ($LASTEXITCODE -ne 0) {
    Write-HxInstallLog -InstStep ROLLBACK -Result FAILED -Detail "reinstall_exit=$LASTEXITCODE"
    throw "ROLLBACK INCOMPLETE: reinstall of the baseline service failed (exit $LASTEXITCODE) - MANUAL OPERATOR INTERVENTION REQUIRED"
  }
  Start-Sleep -Seconds 1
  [void](Assign-HxIdentity -ServiceName $ServiceName -RunAsUser $RunAsUser -ServiceSid $ServiceSid)
  $ci = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
  if ("$($ci.StartName)" -ne $RunAsUser) {
    Write-HxInstallLog -InstStep ROLLBACK -Result FAILED -Detail "identity=$($ci.StartName)"
    throw "ROLLBACK INCOMPLETE: after restore StartName='$($ci.StartName)' != '$RunAsUser' - MANUAL OPERATOR INTERVENTION REQUIRED"
  }
  Write-HxInstallLog -InstStep ROLLBACK -Result ok -Detail "restored_identity=$($ci.StartName)"
}

# Compute the service SID BEFORE the service exists (needed for the logon-right grant + ACL evidence).
$ServiceSid = Get-HxServiceSid -ServiceName $ServiceName
Write-Host "ok   service SID computed before the service exists: $ServiceSid"

# State dir (durable nonce store + logs), separate from the code dir.
DoIt "create state dir $StateDir (+ logs)" { New-Item -ItemType Directory -Force -Path $StateDir, (Join-Path $StateDir "logs") | Out-Null }
DoIt "create WinSW dir $WinSwDir" { New-Item -ItemType Directory -Force -Path $WinSwDir | Out-Null }

if ($Apply -and (-not $SupervisedToken) -and $InstallProfile -eq "Supervised") {
  $SupervisedToken = "guvfxhx-" + ([guid]::NewGuid().ToString("N").Substring(0,12))   # NON-SECRET; value never logged
}
$Snapshot = $null
if ($Apply) {
  $Snapshot = Backup-HxServiceState -ServiceName $ServiceName -ServiceXml $ServiceXml
  if ($Snapshot.Existed -and (-not ($Snapshot.XmlBackup -and (Test-Path $Snapshot.XmlBackup)))) {
    throw "refusing: '$ServiceName' is registered but its baseline WinSW XML is absent at $ServiceXml - cannot guarantee a safe rollback; restore the baseline XML first"
  }
}

try {
  # Stage the WinSW wrapper + reviewed XML. DARK copies verbatim; SUPERVISED substitutes the non-secret launch
  # token into a fresh ASCII copy and REFUSES any non-ASCII (RULE 9 corollary).
  DoIt "stage WinSW wrapper -> $ServiceExe and $InstallProfile config -> $ServiceXml" {
    Copy-Item -Path $WinSwSource -Destination $ServiceExe -Force
    $exeHash = (Get-FileHash $ServiceExe -Algorithm SHA256).Hash.ToLower()
    if ($exeHash -ne $WinSwSha256.ToLower()) { throw "staged WinSW exe hash changed after copy - aborting" }
    if ($InstallProfile -eq "Supervised") {
      $raw = Get-Content -Raw -Path $XmlSource
      $raw = $raw.Replace($PlaceholderToken, $SupervisedToken)
      if ($raw.ToCharArray() | Where-Object { [int]$_ -gt 127 }) { throw "SUPERVISED staged XML contains non-ASCII (RULE 9) - refuse" }
      [System.IO.File]::WriteAllText($ServiceXml, $raw, (New-Object System.Text.ASCIIEncoding))
      Test-HxWinSwXmlContract -XmlPath $ServiceXml -Python $Python -ExecutorDir $ExecutorDir -InstallProfile $InstallProfile -Staged
    } else {
      Copy-Item -Path $XmlSource -Destination $ServiceXml -Force
    }
    Write-HxInstallLog -InstStep STAGE -Result ok
  }

  # Register the service FROM the WinSW config (STOPPED). Uninstall-first if a prior registration exists so the
  # new XML actually applies (WinSW v2.12 install does not update in place).
  DoIt "register service '$ServiceName' via WinSW ($InstallProfile, STOPPED)" {
    if ($Snapshot.Existed) { Uninstall-HxServiceVerified -ServiceName $ServiceName -ServiceExe $ServiceExe }
    & $ServiceExe install
    if ($LASTEXITCODE -ne 0) { throw "WinSW install failed (exit $LASTEXITCODE)" }
    Write-HxInstallLog -InstStep INSTALL -Result ok
  }

  # Assign the service identity (ADR-0040: LocalSystem by default; NT SERVICE virtual account still supported).
  DoIt "assign identity: sc config obj= '$RunAsUser'" {
    $observed = Assign-HxIdentity -ServiceName $ServiceName -RunAsUser $RunAsUser -ServiceSid $ServiceSid
    if ($observed -ne $RunAsUser) { throw "identity assignment did not take: StartName='$observed' != '$RunAsUser' - do NOT start" }
    Write-HxInstallLog -InstStep IDENTITY -Result ok -Detail "start_name=$observed"
  }

  # Verify (no start) - profile-aware.
  if ($Apply) {
    Step "VERIFY service configuration (STOPPED, identity=$RunAsUser, $InstallProfile startmode+recovery)"
    $svc = Get-Service $ServiceName -ErrorAction Stop
    if ($svc.Status -ne "Stopped") { throw "service is $($svc.Status); expected Stopped (install-only)" }
    $ci = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    if ("$($ci.StartName)" -ne $RunAsUser) { throw "service identity is '$($ci.StartName)', expected exactly '$RunAsUser' - do NOT start" }
    if ($ci.ProcessId -ne 0) { throw "service ProcessId is $($ci.ProcessId), expected 0 (not running) - do NOT start" }
    if ("$($ci.PathName)" -notmatch [regex]::Escape($ServiceExe)) { throw "service binary is '$($ci.PathName)', expected the WinSW wrapper $ServiceExe" }
    if ($RunAsUser -ne "LocalSystem") {
      $svcRights = Get-HxSidRights -ServiceSid $ServiceSid
      if ($svcRights -notcontains 'SeServiceLogonRight') { throw "service account lacks SeServiceLogonRight; do NOT start" }
    }
    if ($InstallProfile -eq "Dark") {
      if ($ci.StartMode -notin @("Manual","Disabled")) { throw "DARK service StartMode is '$($ci.StartMode)', expected Manual - do NOT start" }
      Write-Host "ok   DARK service: identity=$($ci.StartName) startmode=$($ci.StartMode) state=$($svc.Status)"
    } else {
      if ($ci.StartMode -ne "Auto") { throw "SUPERVISED service StartMode is '$($ci.StartMode)', expected Auto - do NOT start" }
      Write-Host "ok   SUPERVISED service: identity=$($ci.StartName) startmode=$($ci.StartMode) state=$($svc.Status)"
    }
    Write-HxInstallLog -InstStep VERIFY -Result ok -Detail "identity=$($ci.StartName) start_mode=$($ci.StartMode)"
    Write-Host ""
    Write-Host "ok   $InstallProfile service installed STOPPED via the sanctioned installer. Provision the HOSTED_EXECUTOR_* machine secrets, then the FIRST-START gate."
  }
  Write-HxInstallLog -InstStep SUCCESS -Result ok
}
catch {
  Write-HxInstallLog -InstStep FAIL -Result error -Detail "$($_.Exception.Message)"
  if ($Apply) {
    Restore-HxServiceFromSnapshot -Snapshot $Snapshot -ServiceName $ServiceName -ServiceExe $ServiceExe `
      -ServiceXml $ServiceXml -RunAsUser $RunAsUser -ServiceSid $ServiceSid
  }
  throw
}

if (-not $Apply) {
  Write-Host "PLAN complete ($InstallProfile). Re-run with -Apply -InstallProfile $InstallProfile on the host to perform the install (install-only, no start)."
}

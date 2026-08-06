# CVM-Inc-3 B3P / min-hardening 2026-08-06 - install the beta provisioning agent as a real Windows service
# via a WinSW WRAPPER. THE SINGLE SANCTIONED INSTALL MECHANISM. It supports TWO explicit deployment profiles
# and NEVER requires calling WinSW / sc config / secedit by hand:
#
#   -InstallProfile Dark        the historical install-only profile: Manual start, recovery=none, no supervision.
#                        Exactly today's behaviour. Nothing changes.
#   -InstallProfile Supervised  the min-hardening profile (PR #292): Automatic + delayed start, bounded 3-tier
#                        onfailure=restart, launch-proof env markers, lifecycle logging, exclusive bind.
#
# The profile is EXPLICIT and MANDATORY - no inference, no auto-detection.
#
# DARK ARTEFACT: RUN ONLY on the host, as Administrator, AFTER merge. INSTALL-ONLY for BOTH profiles: it does
# NOT start the service, does NOT touch Session 3 / the prod terminal / the bridge / port 8788 / autologon /
# startup tasks. Dry-run by default; pass -Apply to perform the install. The first manual start waits for
# explicit approval.
#
# IDENTITY (host-proven 2026-07-24 AND re-proven 2026-08-06): WinSW v2.12.0 installs the service as
# LocalSystem regardless of the <serviceaccount> block (virtual-account support is a WinSW v3 feature). This
# installer therefore ALWAYS assigns NT SERVICE\GuvFXBetaAgent AFTER install via `sc config obj=` + grants
# SeServiceLogonRight, then VERIFIES SERVICE_START_NAME == the virtual account before returning success and
# ROLLS BACK if it is anything else. A bare `winsw install` must NEVER be used directly (it leaves
# LocalSystem - the 2026-08-06 blocker).
#
# WHY WINSW, NOT a pywin32 SERVICE HOST (see docs/B3P_SERVICE_HARNESS_COMPARISON.md and the 2026-07-24 STOP).
# The pywin32 service HOST (pythonservice.exe) writes helper DLLs to System32 and next to the BASE interpreter
# (the live bridge's Python) - the venv does not isolate them. WinSW is a standalone .NET wrapper that runs the
# VENV python as a child, so THIS install writes nothing global and needs no pywin32 for the service host.
param(
  [Parameter(Mandatory=$true)][ValidateSet("Dark","Supervised")][string]$InstallProfile,
  [string]$ServiceName = "GuvFXBetaAgent",
  [string]$AgentDir    = "C:\GuvFX\beta\agent",
  [string]$StateDir    = "C:\GuvFX\beta\agent-state",
  [string]$Python      = "C:\GuvFX\beta\agent-venv\Scripts\python.exe",  # dedicated venv; NOT the base/installer
  [string]$RunAsUser   = "NT SERVICE\GuvFXBetaAgent",                    # virtual service account: no password
  [string]$SlotsRoot   = "C:\GuvFX\beta\slots",
  [string]$BetaTombstones = "C:\GuvFX\beta\tombstones",
  [string]$GoldenDir   = "C:\GuvFX\golden\newMT5",
  # SUPERVISED launch-proof marker (NON-SECRET). Substituted into the staged XML's __SET_AT_INSTALL__ token.
  # Empty => generated at runtime. It is never a credential; its VALUE is never logged.
  [string]$SupervisedToken = "",
  # The WinSW wrapper. The operator PLACES the pinned release here (a new executable on the production host
  # is operator-gated); this script REFUSES any binary whose SHA-256 does not match the pin below.
  [string]$WinSwSource = "C:\GuvFX\beta\winsw-src\WinSW.NET4.exe",
  [string]$WinSwSha256 = "923111c7142b3dc783a3c722b19b8a21bcb78222d7a136ac33f0ca8a29f4cb66",  # WinSW v2.12.0 NET4
  [string]$WinSwDir    = "C:\GuvFX\beta\agent-winsw",
  [string]$BaseInterpreterDir = "C:\Program Files\Python311",  # the LIVE bridge's Python - must NOT gain DLLs
  [switch]$Apply
)
$ErrorActionPreference = "Stop"
$ServiceExe = Join-Path $WinSwDir "$ServiceName.exe"      # WinSW config pairs by basename: <name>.exe + <name>.xml
$ServiceXml = Join-Path $WinSwDir "$ServiceName.xml"
# Profile selects the reviewed source XML. Dark = the install-only XML; Supervised = the min-hardening target.
$XmlSourceName = if ($InstallProfile -eq "Supervised") { "$ServiceName.supervised.xml" } else { "$ServiceName.xml" }
$XmlSource  = Join-Path $AgentDir "winsw\$XmlSourceName"
$VenvDir    = Split-Path (Split-Path $Python)             # ...\agent-venv\Scripts\python.exe -> ...\agent-venv
$PlaceholderToken = "__SET_AT_INSTALL__"
# Pin the run-as identity to the least-privilege virtual account (same discipline as the $ServiceName SID
# guard). This closes the drift where -RunAsUser could name a different principal than the SID that receives
# the ACLs + SeServiceLogonRight while VERIFY (which compares StartName to $RunAsUser) still passes.
if ($RunAsUser -ne "NT SERVICE\GuvFXBetaAgent") {
  throw "refusing: -RunAsUser '$RunAsUser' must be exactly 'NT SERVICE\GuvFXBetaAgent' (least-privilege virtual account)"
}

# ---- structured, SECRET-SAFE installer logging (WORKSTREAM F) --------------------------------------------
# Emits one machine-parseable line per lifecycle step. Never receives a credential/key/token VALUE.
function Write-GuvfxInstallLog {
  param([Parameter(Mandatory)][ValidateSet("START","BACKUP","PRECHECK","STAGE","INSTALL","IDENTITY","ACL",
                    "VERIFY","ROLLBACK","SUCCESS","FAIL")][string]$InstStep,
        [Parameter(Mandatory)][string]$Result, [int]$DurationMs = -1, [string]$Detail = "")
  $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
  $d  = if ($DurationMs -ge 0) { " duration_ms=$DurationMs" } else { "" }
  $x  = if ($Detail) { " detail=$Detail" } else { "" }
  Write-Host "install_evidence ts=$ts profile=$InstallProfile step=$InstStep result=$Result$d$x"
}
function Step($m) { Write-Host "==> $m" }
function DoIt($desc, [scriptblock]$block) {
  if ($Apply) { Step "APPLY: $desc"; & $block } else { Step "PLAN:  $desc" }
}
Write-GuvfxInstallLog -InstStep START -Result begin -Detail "apply=$([bool]$Apply)"

# 0. Preconditions (both dry-run and apply)
if (-not (Test-Path (Join-Path $AgentDir "agent.py"))) { throw "agent.py not found under $AgentDir" }
if (-not (Test-Path $XmlSource))                       { throw "WinSW config not found: $XmlSource (profile $InstallProfile bundle incomplete)" }

# INTERPRETER IDENTITY BY METADATA, BEFORE THE BINARY IS EVER EXECUTED. Pointed at the Python INSTALLER
# (C:\GuvFX\python311.exe), executing it launches an installer; so identity is proven from PE metadata,
# statically, and the interpreter is EXECUTED ONLY UNDER -Apply.
function Test-GuvfxInterpreterIdentity {
  param([Parameter(Mandatory)][string]$Path)
  if (-not (Test-Path $Path)) { throw "interpreter not found: $Path (run provision_beta_venv.ps1 -Apply first)" }
  if ((Get-Item $Path -Force).PSIsContainer) { throw "interpreter path is a directory: $Path" }
  $vi = (Get-Item $Path -Force).VersionInfo
  $orig = [string]$vi.OriginalFilename; $desc = [string]$vi.FileDescription
  if ($orig -match '(?i)^python-.*\.exe$' -or $orig -match '(?i)\.msi$') {
    throw "refusing: '$Path' is the Python INSTALLER (OriginalFilename '$orig'), not an interpreter"
  }
  # A full CPython reports 'python.exe'/'pythonw.exe'; a venv Scripts\python.exe is the redirector shim
  # 'py.exe'/'pyw.exe' (host-verified). BOTH are interpreters; the installer 'python-<ver>-amd64.exe' is not.
  if ($orig -notmatch '(?i)^(python|pythonw|py|pyw)\.exe$') {
    throw "refusing: '$Path' OriginalFilename is '$orig'; expected a CPython interpreter or venv shim"
  }
  if ($desc -notmatch '(?i)python') { throw "refusing: '$Path' FileDescription is '$desc'; expected Python" }
  Write-Host "ok   interpreter identity (metadata, not executed): OriginalFilename '$orig', '$desc' $($vi.FileVersion)"
}
function Test-GuvfxInterpreterRuntime {
  <# EXECUTES the interpreter. Only under -Apply, after the static identity check. Checks it is a Python 3
     and that the agent bundle imports. The agent's pywin32 imports are LAZY (inside win_slot_ops methods),
     so `import agent` succeeds without pywin32 loaded - this validates interpreter + bundle coherence, NOT
     that pywin32 is functional (that is provision_beta_venv.ps1's job and the later runtime trial's). #>
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$AgentDir)
  $ver = & $Path --version 2>&1
  if ($LASTEXITCODE -ne 0 -or "$ver" -notmatch '(?i)^Python 3\.') {
    throw "interpreter '$Path' did not report a Python 3 version (got '$ver', exit $LASTEXITCODE)"
  }
  & $Path -c "import sys; sys.path.insert(0, r'$AgentDir'); import config, agent, manifest" 2>$null
  if ($LASTEXITCODE -ne 0) { throw "the agent's own modules (config/agent/manifest) do not import under '$Path'" }
  Write-Host "ok   interpreter runtime: $ver, agent bundle imports (lazy pywin32 not exercised here)"
}
Test-GuvfxInterpreterIdentity -Path $Python
if ($Apply) { Test-GuvfxInterpreterRuntime -Path $Python -AgentDir $AgentDir }
else { Write-Host "PLAN:  interpreter runtime check DEFERRED to -Apply (PLAN never executes a candidate binary)" }

# WinSW wrapper: identity by PINNED HASH before it is ever run. A new executable on the production host is
# operator-placed; a hash mismatch (or absence) is a hard refusal - the wrapper is never fetched or trusted
# by this script.
function Test-GuvfxWinSw {
  param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$ExpectSha256)
  if (-not (Test-Path $Path)) {
    throw "WinSW wrapper not found: $Path - place the pinned WinSW.NET4.exe there first (operator-gated)"
  }
  $got = (Get-FileHash $Path -Algorithm SHA256).Hash.ToLower()
  if ($got -ne $ExpectSha256.ToLower()) {
    throw "WinSW hash mismatch at $Path : got $got, pinned $ExpectSha256 - REFUSING an unverified executable"
  }
  Write-Host "ok   WinSW wrapper verified by pinned SHA-256 ($ExpectSha256)"
}
Test-GuvfxWinSw -Path $WinSwSource -ExpectSha256 $WinSwSha256
Write-Host "ok   preconditions: agent.py + WinSW config present; interpreter + wrapper verified"
Write-GuvfxInstallLog -InstStep PRECHECK -Result ok -Detail "xml=$XmlSourceName"

# XML CONTRACT VALIDATION - PROFILE-AWARE. The identity/runtime guard validates $Python, but the SERVICE runs
# whatever the XML's <executable> says. Bind them for BOTH profiles: refuse unless the XML runs exactly the
# interpreter we validated and the agent under $AgentDir, with a stop timeout that exceeds the drain. Then the
# recovery + startmode invariants DIFFER by profile:
#   Dark:       exactly one <onfailure action=none>; <startmode>Manual.
#   Supervised: <startmode>Automatic + <delayedAutoStart>true; >=3 <onfailure action=restart> (no 'none');
#               <resetfailure> present; the three launch-proof env markers present.
function Test-GuvfxWinSwXmlContract {
  param([Parameter(Mandatory)][string]$XmlPath, [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$AgentDir, [Parameter(Mandatory)][string]$InstallProfile,
        [switch]$Staged)
  [xml]$doc = Get-Content -Raw -Path $XmlPath
  $svc = $doc.service
  # (F4/F8) the interpreter the service will actually launch must be the one the guard just validated
  if ("$($svc.executable)" -ne $Python) {
    throw "XML <executable> '$($svc.executable)' != validated -Python '$Python' - the guard would validate a different interpreter than the service runs"
  }
  $agentPy = (Join-Path $AgentDir "agent.py")
  if ("$($svc.arguments)" -notmatch [regex]::Escape($agentPy)) {
    throw "XML <arguments> '$($svc.arguments)' does not run '$agentPy' under -AgentDir"
  }
  # (F7) stoptimeout must exceed the configured drain (machine env, else config.example default 45) - BOTH profiles
  $stopRaw = "$($svc.stoptimeout)"
  if ($stopRaw -match '^\s*(\d+)\s*sec\s*$') { $stopS = [int]$Matches[1] }
  else { throw "XML <stoptimeout> '$stopRaw' is not '<N> sec'" }
  $drainRaw = [Environment]::GetEnvironmentVariable("BETA_AGENT_DRAIN_TIMEOUT_S", "Machine")
  $drainS = 45; if ($drainRaw -and ($drainRaw -match '^\s*\d+(\.\d+)?\s*$')) { $drainS = [int][math]::Ceiling([double]$drainRaw) }
  if ($stopS -le $drainS) {
    throw "XML <stoptimeout> ${stopS}s must EXCEED BETA_AGENT_DRAIN_TIMEOUT_S (${drainS}s) or a stop force-kills a mutation mid-drain (B-6)"
  }
  $of = @($svc.onfailure)
  $ofActions = (@($of | ForEach-Object { [string]$_.action })) -join ','
  if ($InstallProfile -eq "Dark") {
    # (F5/F9) exactly one recovery entry and it must be 'none' - a second <onfailure> makes .onfailure an array
    if ($of.Count -ne 1 -or "$($of[0].action)" -ne "none") {
      throw "DARK XML recovery must be a single onfailure action=none entry; found $($of.Count) (actions: $ofActions)"
    }
    if ("$($svc.startmode)" -ne "Manual") { throw "DARK XML <startmode> is '$($svc.startmode)', expected Manual (no autostart)" }
    Write-Host "ok   DARK XML contract: runs '$Python' on agent.py; recovery=none; stoptimeout ${stopS}s > drain ${drainS}s; startmode Manual"
  } else {
    # SUPERVISED recovery/startmode invariants.
    if ("$($svc.startmode)" -ne "Automatic") { throw "SUPERVISED XML <startmode> is '$($svc.startmode)', expected Automatic" }
    if ("$($svc.delayedAutoStart)" -ne "true") { throw "SUPERVISED XML <delayedAutoStart> is '$($svc.delayedAutoStart)', expected true" }
    if ($of.Count -lt 3) { throw "SUPERVISED XML needs >=3 <onfailure> tiers (bounded-backoff restart FLOOR); found $($of.Count)" }
    foreach ($o in $of) { if ("$($o.action)" -ne "restart") { throw "SUPERVISED XML <onfailure> action '$($o.action)' must be restart (all tiers); actions: $ofActions" } }
    if (-not "$($svc.resetfailure)") { throw "SUPERVISED XML missing <resetfailure> (the restart counter never resets)" }
    # launch-proof env markers must be present (values validated separately post-stage)
    $envNames = @(@($svc.env) | ForEach-Object { [string]$_.name })
    foreach ($need in @("BETA_AGENT_SERVICE_IDENTITY","BETA_AGENT_SUPERVISED_TOKEN","BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH")) {
      if ($envNames -notcontains $need) { throw "SUPERVISED XML missing required <env name='$need'>" }
    }
    if ($Staged) {
      # post-stage: the token placeholder must be substituted with a concrete non-empty value
      $tok = ""; foreach ($e in @($svc.env)) { if ("$($e.name)" -eq "BETA_AGENT_SUPERVISED_TOKEN") { $tok = "$($e.value)" } }
      if (-not $tok -or $tok -eq $PlaceholderToken) { throw "STAGED SUPERVISED XML still has an unsubstituted/empty BETA_AGENT_SUPERVISED_TOKEN" }
      if ((Get-Content -Raw -Path $XmlPath) -match [regex]::Escape($PlaceholderToken)) { throw "STAGED SUPERVISED XML still contains the '$PlaceholderToken' placeholder" }
    }
    Write-Host "ok   SUPERVISED XML contract: runs '$Python' on agent.py; startmode Automatic+delayed; $($of.Count) restart tiers; resetfailure present; launch markers present; stoptimeout ${stopS}s > drain ${drainS}s"
  }
}
Test-GuvfxWinSwXmlContract -XmlPath $XmlSource -Python $Python -AgentDir $AgentDir -InstallProfile $InstallProfile

# GLOBAL-WRITE MEASUREMENT (RULE 11 / evidence.md). Snapshot the two DLL names in both locations BEFORE any
# mutation, and at VERIFY assert this run created or modified neither.
$GlobalDllPaths = @(
  (Join-Path $env:SystemRoot "System32\pywintypes311.dll"),
  (Join-Path $env:SystemRoot "System32\pythoncom311.dll"),
  (Join-Path $BaseInterpreterDir "pywintypes311.dll"),
  (Join-Path $BaseInterpreterDir "pythoncom311.dll")
)
function Get-GuvfxGlobalDllState {
  param([Parameter(Mandatory)][string[]]$Paths)
  $s = @{}
  foreach ($p in $Paths) {
    $it = Get-Item -LiteralPath $p -Force -ErrorAction SilentlyContinue
    $s[$p] = if ($it) { @{ exists = $true; mtime = $it.LastWriteTimeUtc.Ticks } } else { @{ exists = $false; mtime = 0 } }
  }
  return $s
}
$GlobalDllBaseline = $null
if ($Apply) { $GlobalDllBaseline = Get-GuvfxGlobalDllState -Paths $GlobalDllPaths }

# 1. State dir (durable nonce/idempotency/logs), SEPARATE from the code dir.
DoIt "create state dir $StateDir (+ logs)" { New-Item -ItemType Directory -Force -Path $StateDir, (Join-Path $StateDir "logs") | Out-Null }

# 2. Scoped NTFS ACLs for the service SID (Modify on state/tombstones/slots; ReadAndExecute on code+golden).
function Get-GuvfxServiceSid {
  param([Parameter(Mandatory)][string]$ServiceName)
  if ($ServiceName -ne "GuvFXBetaAgent") { throw "refusing service SID lookup for '$ServiceName'" }
  $m = (& sc.exe showsid $ServiceName) | Select-String -Pattern "SERVICE SID:\s*(S-1-5-80-\S+)"
  if (-not $m) { throw "could not compute the service SID for '$ServiceName'" }
  $v = $m.Matches.Groups[1].Value
  if ($v -notmatch "^S-1-5-80-\d+-\d+-\d+-\d+-\d+$") { throw "refusing: '$v' is not a service SID" }
  return $v
}
function Grant-GuvfxServiceAcl {
  param([Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateSet("Modify","ReadAndExecute")][string]$Rights,
        [Parameter(Mandatory)][string]$ServiceSid)
  $sid  = New-Object System.Security.Principal.SecurityIdentifier($ServiceSid)
  $acl  = Get-Acl -Path $Path
  $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
      $sid, [System.Security.AccessControl.FileSystemRights]::$Rights,
      ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
       [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
      [System.Security.AccessControl.PropagationFlags]::None, "Allow")))
  Set-Acl -Path $Path -AclObject $acl
  $rules = (Get-Acl -Path $Path).GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier])
  if (@($rules | Where-Object { $_.IdentityReference.Value -eq $ServiceSid }).Count -eq 0) {
    throw "post-check failed: service SID $ServiceSid is not on $Path"
  }
  Write-Host "evidence acl path=$Path service_sid=$ServiceSid rights=$Rights result=granted"
}
$ServiceSid = Get-GuvfxServiceSid -ServiceName $ServiceName
Write-Host "ok   service SID computed before the service exists: $ServiceSid"
if (-not (Test-Path $SlotsRoot)) {
  throw "slot pool not provisioned at $SlotsRoot - run install_pool.ps1 -Apply FIRST"
}
# The WinSW wrapper dir must be readable+executable by the service account (it runs the .exe).
DoIt "create WinSW dir $WinSwDir" { New-Item -ItemType Directory -Force -Path $WinSwDir | Out-Null }
foreach ($d in @($StateDir, $BetaTombstones, $SlotsRoot)) {
  DoIt "grant '$RunAsUser' Modify on $d (inherit)" { Grant-GuvfxServiceAcl -Path $d -Rights Modify -ServiceSid $ServiceSid }
}
# $VenvDir is where WinSW's <executable> python.exe AND the agent's pywin32 DLLs (Lib\site-packages\
# pywin32_system32) load from - the least-privilege account must be able to read+execute it, so it is granted
# and verified exactly like the code dirs, never left to an assumed inherited ACE (RULE 11).
foreach ($d in @($AgentDir, $GoldenDir, $WinSwDir, $VenvDir)) {
  DoIt "grant '$RunAsUser' ReadAndExecute on $d (inherit)" { Grant-GuvfxServiceAcl -Path $d -Rights ReadAndExecute -ServiceSid $ServiceSid }
}
if ($Apply) { Write-GuvfxInstallLog -InstStep ACL -Result ok }

# LSA interop for the SERVICE-LOGON right. HOST-PROVEN: WinSW v2.12.0 does NOT apply <serviceaccount>, so
# identity is assigned AFTER install by `sc config obj=` and the SERVICE account is granted SeServiceLogonRight
# here (NOT auto-granted by sc config - secedit-verified), addressed by the DERIVED SID.
try {
  Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GuvfxLsaSvc {
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
$LSA_READ_SVC  = 0x00000801   # POLICY_VIEW_LOCAL_INFORMATION | POLICY_LOOKUP_NAMES
$LSA_WRITE_SVC = 0x00000811   # POLICY_CREATE_ACCOUNT          | POLICY_LOOKUP_NAMES
$STATUS_NAME_NOT_FOUND_SVC = [uint32]3221225524   # 0xC0000034 STATUS_OBJECT_NAME_NOT_FOUND
function Get-GuvfxServiceSidBytes {
  param([Parameter(Mandatory)][string]$ServiceSid)
  $s = New-Object System.Security.Principal.SecurityIdentifier($ServiceSid)
  $b = New-Object byte[] $s.BinaryLength
  $s.GetBinaryForm($b, 0)
  return $b
}
function Open-GuvfxSvcLsaPolicy {
  param([uint32]$Access)
  $oa = New-Object GuvfxLsaSvc+LSA_OBJECT_ATTRIBUTES
  $oa.Length = [Runtime.InteropServices.Marshal]::SizeOf($oa)
  $h = [IntPtr]::Zero
  $st = [GuvfxLsaSvc]::LsaOpenPolicy([IntPtr]::Zero, [ref]$oa, $Access, [ref]$h)
  if ($st -ne 0) { throw "LsaOpenPolicy failed: NTSTATUS 0x$('{0:X8}' -f $st) (win32 $([GuvfxLsaSvc]::LsaNtStatusToWinError($st)))" }
  return $h
}
function Get-GuvfxSidRights {
  # READ-ONLY: the rights currently held by the SID, or an empty array.
  param([Parameter(Mandatory)][string]$ServiceSid)
  $sid = Get-GuvfxServiceSidBytes -ServiceSid $ServiceSid
  $h = Open-GuvfxSvcLsaPolicy -Access $LSA_READ_SVC
  try {
    $ptr = [IntPtr]::Zero; $count = [uint32]0
    $st = [GuvfxLsaSvc]::LsaEnumerateAccountRights($h, $sid, [ref]$ptr, [ref]$count)
    if ($st -eq $STATUS_NAME_NOT_FOUND_SVC) { return @() }
    if ($st -ne 0) { throw "LsaEnumerateAccountRights failed: NTSTATUS 0x$('{0:X8}' -f $st)" }
    $out = @()
    $size = [Runtime.InteropServices.Marshal]::SizeOf([type][GuvfxLsaSvc+LSA_UNICODE_STRING])
    for ($i = 0; $i -lt $count; $i++) {
      $item = [Runtime.InteropServices.Marshal]::PtrToStructure([IntPtr]($ptr.ToInt64() + ($i * $size)), [type][GuvfxLsaSvc+LSA_UNICODE_STRING])
      $out += [Runtime.InteropServices.Marshal]::PtrToStringUni($item.Buffer, $item.Length / 2)
    }
    [void][GuvfxLsaSvc]::LsaFreeMemory($ptr)
    return $out
  } finally { [void][GuvfxLsaSvc]::LsaClose($h) }
}
function Grant-GuvfxServiceLogonRight {
  # Adds ONLY SeServiceLogonRight to the service SID; idempotent; preserves every other right; post-checks.
  param([Parameter(Mandatory)][string]$ServiceSid)
  $before = Get-GuvfxSidRights -ServiceSid $ServiceSid
  if ($before -contains $SvcLogonRight) { Write-Host "evidence right=$SvcLogonRight sid=$ServiceSid op=add result=already_present"; return }
  $sid = Get-GuvfxServiceSidBytes -ServiceSid $ServiceSid
  $h = Open-GuvfxSvcLsaPolicy -Access $LSA_WRITE_SVC
  try {
    $u = New-Object GuvfxLsaSvc+LSA_UNICODE_STRING
    $u.Buffer        = [Runtime.InteropServices.Marshal]::StringToHGlobalUni($SvcLogonRight)
    $u.Length        = [uint16]($SvcLogonRight.Length * 2)
    $u.MaximumLength = [uint16](($SvcLogonRight.Length + 1) * 2)
    $arr = @($u)
    $st = [GuvfxLsaSvc]::LsaAddAccountRights($h, $sid, $arr, 1)
    [Runtime.InteropServices.Marshal]::FreeHGlobal($u.Buffer)
    if ($st -ne 0) { Write-Host "evidence right=$SvcLogonRight sid=$ServiceSid op=add result=failed ntstatus=0x$('{0:X8}' -f $st)"; throw "LsaAddAccountRights failed: NTSTATUS 0x$('{0:X8}' -f $st) (win32 $([GuvfxLsaSvc]::LsaNtStatusToWinError($st)))" }
  } finally { [void][GuvfxLsaSvc]::LsaClose($h) }
  $after = Get-GuvfxSidRights -ServiceSid $ServiceSid
  if ($after -notcontains $SvcLogonRight) { throw "post-check failed: service account still lacks $SvcLogonRight - do NOT start" }
  foreach ($r in $before) { if ($after -notcontains $r) { throw "user-right regression: service account lost '$r'" } }
  Write-Host "evidence right=$SvcLogonRight sid=$ServiceSid op=add result=granted other_rights_preserved=$($before.Count)"
}

# ---- BACKUP / ROLLBACK (WORKSTREAM E) --------------------------------------------------------------------
# Snapshot the CURRENT service (identity, startmode, XML) before mutating, so a verification failure restores
# EXACTLY the pre-install service state. Bundle backup/restore is the paired operator step (deploy package
# section 2); this installer owns the SERVICE (registration + identity + startmode + recovery + XML).
function Backup-GuvfxServiceState {
  param([Parameter(Mandatory)][string]$ServiceName, [Parameter(Mandatory)][string]$ServiceXml)
  $ci = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue
  $snap = @{ Existed = [bool]$ci; StartName = $null; StartMode = $null; XmlBackup = $null }
  if ($ci) {
    $snap.StartName = "$($ci.StartName)"; $snap.StartMode = "$($ci.StartMode)"
    if (Test-Path $ServiceXml) {
      $bkDir = Join-Path (Split-Path $ServiceXml) "_installer_rollback"
      New-Item -ItemType Directory -Force -Path $bkDir | Out-Null
      $snap.XmlBackup = Join-Path $bkDir "GuvFXBetaAgent.xml.prev"
      Copy-Item $ServiceXml $snap.XmlBackup -Force
    }
  }
  Write-GuvfxInstallLog -InstStep BACKUP -Result ok -Detail "existed=$($snap.Existed) start_mode=$($snap.StartMode)"
  return $snap
}
function Assign-GuvfxIdentity {
  # THE fix for the WinSW-v2.12-LocalSystem blocker: assign the virtual account post-install + grant the
  # logon right + return the OBSERVED StartName. Never bypassed.
  param([Parameter(Mandatory)][string]$ServiceName, [Parameter(Mandatory)][string]$RunAsUser,
        [Parameter(Mandatory)][string]$ServiceSid)
  $scOut  = & sc.exe config $ServiceName obj= "$RunAsUser" 2>&1
  $scRc   = $LASTEXITCODE
  $scText = ($scOut | Out-String).Trim()
  Write-Host "evidence sc_config obj='$RunAsUser' exit=$scRc output=$scText"
  if ($scRc -ne 0 -or $scText -notmatch 'ChangeServiceConfig SUCCESS') {
    throw "sc config obj= failed (exit=$scRc): $scText - do NOT start"
  }
  Grant-GuvfxServiceLogonRight -ServiceSid $ServiceSid
  return "$((Get-CimInstance Win32_Service -Filter "Name='$ServiceName'").StartName)"
}
function Wait-GuvfxServiceRemoved {
  # Bounded poll until the service is truly gone from the SCM (WinSW uninstall can leave it 'marked for
  # deletion' with a handle open). Returns $true if removed, $false if still present after the timeout.
  param([Parameter(Mandatory)][string]$ServiceName, [int]$TimeoutSeconds = 20)
  for ($i = 0; $i -lt $TimeoutSeconds; $i++) {
    if (-not (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue)) { return $true }
    Start-Sleep -Seconds 1
  }
  return (-not (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'" -ErrorAction SilentlyContinue))
}
function Uninstall-GuvfxServiceVerified {
  # Uninstall via WinSW and CONFIRM removal; a failure here is loud, never swallowed (adversarial review).
  param([Parameter(Mandatory)][string]$ServiceName, [Parameter(Mandatory)][string]$ServiceExe)
  & $ServiceExe uninstall 2>&1 | Write-Host
  $rc = $LASTEXITCODE
  if (-not (Wait-GuvfxServiceRemoved -ServiceName $ServiceName)) {
    throw "service '$ServiceName' still registered after uninstall (winsw exit=$rc; possibly marked-for-deletion / open handle)"
  }
}
function Restore-GuvfxServiceFromSnapshot {
  # Automatic rollback on verification failure. Restores the service to the snapshot; if there was no prior
  # service, removes the one we created (VERIFIED). Re-asserts NT SERVICE identity + StartMode of the baseline.
  # Every step is checked; an unverifiable restore raises ROLLBACK INCOMPLETE - it never logs a false success.
  param([Parameter(Mandatory)]$Snapshot, [Parameter(Mandatory)][string]$ServiceName,
        [Parameter(Mandatory)][string]$ServiceExe, [Parameter(Mandatory)][string]$ServiceXml,
        [Parameter(Mandatory)][string]$RunAsUser, [Parameter(Mandatory)][string]$ServiceSid)
  Write-GuvfxInstallLog -InstStep ROLLBACK -Result begin
  if (-not $Snapshot.Existed) {
    # No prior service: remove the one we created and CONFIRM it is gone (else fail loud).
    try { Uninstall-GuvfxServiceVerified -ServiceName $ServiceName -ServiceExe $ServiceExe }
    catch {
      Write-GuvfxInstallLog -InstStep ROLLBACK -Result FAILED -Detail "removal_unconfirmed"
      throw "ROLLBACK INCOMPLETE: could not confirm removal of the freshly-created '$ServiceName' - a LocalSystem/Automatic service may remain - MANUAL OPERATOR INTERVENTION REQUIRED"
    }
    Write-GuvfxInstallLog -InstStep ROLLBACK -Result ok -Detail "no_prior_service_removed_confirmed"
    return
  }
  # Prior service existed: the early guard guarantees XmlBackup is present, so we can restore the baseline XML
  # (NOT whatever staged XML is on disk). Remove-then-reinstall from the baseline, then re-assert identity.
  Uninstall-GuvfxServiceVerified -ServiceName $ServiceName -ServiceExe $ServiceExe
  if (-not ($Snapshot.XmlBackup -and (Test-Path $Snapshot.XmlBackup))) {
    Write-GuvfxInstallLog -InstStep ROLLBACK -Result FAILED -Detail "baseline_xml_missing"
    throw "ROLLBACK INCOMPLETE: baseline WinSW XML backup is missing; cannot restore the prior service safely - MANUAL OPERATOR INTERVENTION REQUIRED"
  }
  Copy-Item $Snapshot.XmlBackup $ServiceXml -Force
  & $ServiceExe install 2>&1 | Write-Host
  if ($LASTEXITCODE -ne 0) {
    Write-GuvfxInstallLog -InstStep ROLLBACK -Result FAILED -Detail "reinstall_exit=$LASTEXITCODE"
    throw "ROLLBACK INCOMPLETE: reinstall of the baseline service failed (exit $LASTEXITCODE) - MANUAL OPERATOR INTERVENTION REQUIRED"
  }
  Start-Sleep -Seconds 1
  [void](Assign-GuvfxIdentity -ServiceName $ServiceName -RunAsUser $RunAsUser -ServiceSid $ServiceSid)
  if ($Snapshot.StartMode -match '(?i)manual') {
    $scStart = (& sc.exe config $ServiceName start= demand 2>&1 | Out-String).Trim()
    if ($scStart -notmatch 'ChangeServiceConfig SUCCESS') { Write-GuvfxInstallLog -InstStep ROLLBACK -Result warn -Detail "start_mode_restore_unconfirmed" }
  }
  $ci = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
  if ("$($ci.StartName)" -ne $RunAsUser) {
    Write-GuvfxInstallLog -InstStep ROLLBACK -Result FAILED -Detail "identity=$($ci.StartName)"
    throw "ROLLBACK INCOMPLETE: after restore StartName='$($ci.StartName)' != '$RunAsUser' - MANUAL OPERATOR INTERVENTION REQUIRED"
  }
  Write-GuvfxInstallLog -InstStep ROLLBACK -Result ok -Detail "restored_identity=$($ci.StartName) start_mode=$($ci.StartMode)"
}

# ---- INSTALL + IDENTITY + VERIFY (wrapped so any failure rolls back) -------------------------------------
if ($Apply -and (-not $SupervisedToken) -and $InstallProfile -eq "Supervised") {
  $SupervisedToken = "guvfxbeta-" + ([guid]::NewGuid().ToString("N").Substring(0,12))   # NON-SECRET; value never logged
}
$Snapshot = $null
if ($Apply) {
  $Snapshot = Backup-GuvfxServiceState -ServiceName $ServiceName -ServiceXml $ServiceXml
  # Refuse BEFORE mutating if a prior service is registered but its baseline XML is not captured: a
  # remove-then-reinstall (below) would then have no baseline to roll back to, and reinstalling from the
  # staged new-profile XML would leave a worse-than-baseline service (adversarial review). Operator must
  # restore the baseline XML at $ServiceXml first.
  if ($Snapshot.Existed -and (-not ($Snapshot.XmlBackup -and (Test-Path $Snapshot.XmlBackup)))) {
    throw "refusing: '$ServiceName' is registered but its baseline WinSW XML is absent at $ServiceXml - cannot guarantee a safe rollback; restore the baseline XML first, then re-run"
  }
}

try {
  # 3. Stage the WinSW wrapper + its reviewed XML. DARK copies the XML verbatim; SUPERVISED substitutes the
  #    non-secret launch token into a fresh ASCII copy and REFUSES any non-ASCII (RULE 9 corollary).
  DoIt "stage WinSW wrapper -> $ServiceExe and $InstallProfile config -> $ServiceXml" {
    Copy-Item -Path $WinSwSource -Destination $ServiceExe -Force
    $exeHash = (Get-FileHash $ServiceExe -Algorithm SHA256).Hash.ToLower()
    if ($exeHash -ne $WinSwSha256.ToLower()) { throw "staged WinSW exe hash changed after copy - aborting" }
    if ($InstallProfile -eq "Supervised") {
      $raw = Get-Content -Raw -Path $XmlSource
      $raw = $raw.Replace($PlaceholderToken, $SupervisedToken)
      if ($raw.ToCharArray() | Where-Object { [int]$_ -gt 127 }) { throw "SUPERVISED staged XML contains non-ASCII (RULE 9) - refuse" }
      [System.IO.File]::WriteAllText($ServiceXml, $raw, (New-Object System.Text.ASCIIEncoding))
      Test-GuvfxWinSwXmlContract -XmlPath $ServiceXml -Python $Python -AgentDir $AgentDir -InstallProfile $InstallProfile -Staged
    } else {
      Copy-Item -Path $XmlSource -Destination $ServiceXml -Force
    }
    Write-GuvfxInstallLog -InstStep STAGE -Result ok
  }

  # 4. Register the service FROM the WinSW config (STOPPED). WinSW installs LocalSystem (v2.12); the identity
  #    is corrected in 4a. NOTE: a bare `winsw install` alone is NEVER the sanctioned path.
  #    RE-INSTALL SAFETY: WinSW v2.12 `install` does NOT update an already-registered service in place, so the
  #    new profile's startmode/recovery/env-markers would be silently ignored over an existing registration.
  #    When a prior service exists we therefore UNINSTALL-FIRST (verified) so `install` applies the new XML.
  #    The prior XML is already backed up (early guard), so a later failure still rolls back to baseline.
  DoIt "register service '$ServiceName' via WinSW ($InstallProfile, STOPPED)" {
    if ($Snapshot.Existed) { Uninstall-GuvfxServiceVerified -ServiceName $ServiceName -ServiceExe $ServiceExe }
    & $ServiceExe install
    if ($LASTEXITCODE -ne 0) { throw "WinSW install failed (exit $LASTEXITCODE)" }
    Write-GuvfxInstallLog -InstStep INSTALL -Result ok
  }

  # 4a. Assign the NT SERVICE virtual account + grant SeServiceLogonRight (the LocalSystem fix), for BOTH
  #     profiles. This is the mandatory post-install identity step a bare winsw install cannot do.
  DoIt "assign identity: sc config obj= '$RunAsUser' + grant SeServiceLogonRight" {
    $observed = Assign-GuvfxIdentity -ServiceName $ServiceName -RunAsUser $RunAsUser -ServiceSid $ServiceSid
    if ($observed -ne $RunAsUser) { throw "identity assignment did not take: StartName='$observed' != '$RunAsUser' - do NOT start" }
    Write-GuvfxInstallLog -InstStep IDENTITY -Result ok -Detail "start_name=$observed"
  }

  # 5. Verify (no start) - PROFILE-AWARE.
  if ($Apply) {
    Step "VERIFY service configuration (STOPPED, ProcessId 0, NT SERVICE identity + SeServiceLogonRight, $InstallProfile startmode+recovery, no global DLL)"
    $svc = Get-Service $ServiceName -ErrorAction Stop
    if ($svc.Status -ne "Stopped") { throw "service is $($svc.Status); expected Stopped (install-only, both profiles)" }
    $ci = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    # Identity must be EXACTLY the virtual account - reject LocalSystem/LocalService/NetworkService (the 2026-08-06 blocker).
    if ("$($ci.StartName)" -ne $RunAsUser) {
      throw "service identity is '$($ci.StartName)', expected exactly '$RunAsUser' - no LocalSystem fallback; do NOT start"
    }
    if ($ci.ProcessId -ne 0) { throw "service ProcessId is $($ci.ProcessId), expected 0 (not running) - do NOT start" }
    if ("$($ci.PathName)" -notmatch [regex]::Escape($ServiceExe)) { throw "service binary is '$($ci.PathName)', expected the WinSW wrapper $ServiceExe" }
    $svcRights = Get-GuvfxSidRights -ServiceSid $ServiceSid
    if ($svcRights -notcontains 'SeServiceLogonRight') { throw "service account lacks SeServiceLogonRight - it cannot start; do NOT start" }
    # Profile-specific startmode + recovery.
    $qf = (& sc.exe qfailure $ServiceName) -join "`n"
    Write-Host $qf
    $hasRestart = ($qf -match '(?im)^\s*(RESTART|RUN PROCESS|REBOOT)\b' -or $qf -match '(?i)FAILURE_ACTIONS.*(RESTART|REBOOT|RUN)')
    if ($InstallProfile -eq "Dark") {
      if ($ci.StartMode -notin @("Manual","Disabled")) { throw "DARK service StartMode is '$($ci.StartMode)', expected Manual - do NOT start" }
      if ($hasRestart) { throw "DARK service has SCM recovery actions configured; expected none (install-only) - do NOT start" }
      Write-Host "ok   DARK service: identity=$($ci.StartName) startmode=$($ci.StartMode) recovery=none state=$($svc.Status)"
    } else {
      if ($ci.StartMode -ne "Auto") { throw "SUPERVISED service StartMode is '$($ci.StartMode)', expected Auto - do NOT start" }
      if (-not $hasRestart) { throw "SUPERVISED service has NO SCM restart actions; expected bounded restart tiers - do NOT start" }
      Write-Host "ok   SUPERVISED service: identity=$($ci.StartName) startmode=$($ci.StartMode) recovery=restart-tiers state=$($svc.Status)"
    }
    # (F10) MEASURED: prove THIS run created/modified no pywin32 helper DLL globally.
    $after = Get-GuvfxGlobalDllState -Paths $GlobalDllPaths
    foreach ($p in $GlobalDllPaths) {
      $b = $GlobalDllBaseline[$p]; $a = $after[$p]
      if ((-not $b.exists) -and $a.exists) { throw "GLOBAL WRITE: this install created '$p'; do NOT start" }
      if ($b.exists -and $a.exists -and ($b.mtime -ne $a.mtime)) { throw "GLOBAL WRITE: this install modified '$p'; do NOT start" }
    }
    Write-Host "ok   WinSW install created/modified NO pywin32 DLL in System32 or the base interpreter (measured before/after)"
    foreach ($d in @($StateDir, $BetaTombstones, $SlotsRoot, $AgentDir, $GoldenDir, $WinSwDir, $VenvDir)) {
      $sids = @((Get-Acl -Path $d).GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value })
      if ($sids -notcontains $ServiceSid) { throw "no ACE for '$RunAsUser' ($ServiceSid) on $d - the grant did not take; do NOT start" }
    }
    Write-GuvfxInstallLog -InstStep VERIFY -Result ok -Detail "identity=$($ci.StartName) start_mode=$($ci.StartMode)"
    Write-Host ""
    Write-Host "ok   $InstallProfile service installed STOPPED via the sanctioned installer. Next: firewall.ps1 -Apply, then the FIRST-START gate."
    Write-Host "     The signing keyring (BETA_AGENT_KEYRING / _KEY_ID) must be provisioned by the operator before first start."
  }
  Write-GuvfxInstallLog -InstStep SUCCESS -Result ok
}
catch {
  Write-GuvfxInstallLog -InstStep FAIL -Result error -Detail "$($_.Exception.Message)"
  if ($Apply) {
    Restore-GuvfxServiceFromSnapshot -Snapshot $Snapshot -ServiceName $ServiceName -ServiceExe $ServiceExe `
      -ServiceXml $ServiceXml -RunAsUser $RunAsUser -ServiceSid $ServiceSid
  }
  throw
}

if (-not $Apply) {
  Write-Host "PLAN complete ($InstallProfile). Re-run with -Apply -InstallProfile $InstallProfile on the host to perform the install (install-only, no start)."
}

# CVM-Inc-3 B3P - install the beta provisioning agent as a real Windows service via a WinSW WRAPPER.
# DARK ARTEFACT: RUN ONLY on the host, as Administrator, AFTER merge. INSTALL-ONLY: it does NOT start the
# service, does NOT touch Session 3 / the prod terminal / the bridge / port 8788 / autologon / startup tasks.
# Dry-run by default; pass -Apply to perform the install. The first manual start waits for explicit approval.
#
# WHY WINSW, NOT a pywin32 SERVICE HOST (see docs/B3P_SERVICE_HARNESS_COMPARISON.md and the 2026-07-24 STOP).
# The pywin32 service HOST (pythonservice.exe) (a) writes helper DLLs to System32 and next to the BASE
# interpreter (the live bridge's Python) - the venv does not isolate them - and (b) failed to assign the
# NT SERVICE virtual account via `sc config obj=`, leaving the service as over-privileged LocalSystem.
# WinSW is a standalone .NET wrapper that runs the VENV python as a child and assigns the virtual account
# from its XML, so THIS install writes nothing global and needs no pywin32 for the service host.
# NOTE: the agent's own slot-mutation code (win_slot_ops) still imports pywin32 LAZILY at runtime; that
# pywin32 lives in the venv and its DLLs load from the venv's pywin32_system32 via the pip bootstrap - it
# is NOT a dependency of the service host, and provisioning the venv must not run pywin32_postinstall (which
# is the OTHER global write). See provision_beta_venv.ps1.
param(
  [string]$ServiceName = "GuvFXBetaAgent",
  [string]$AgentDir    = "C:\GuvFX\beta\agent",
  [string]$StateDir    = "C:\GuvFX\beta\agent-state",
  [string]$Python      = "C:\GuvFX\beta\agent-venv\Scripts\python.exe",  # dedicated venv; NOT the base/installer
  [string]$RunAsUser   = "NT SERVICE\GuvFXBetaAgent",                    # virtual service account: no password
  [string]$SlotsRoot   = "C:\GuvFX\beta\slots",
  [string]$BetaTombstones = "C:\GuvFX\beta\tombstones",
  [string]$GoldenDir   = "C:\GuvFX\golden\newMT5",
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
$XmlSource  = Join-Path $AgentDir "winsw\$ServiceName.xml"
$VenvDir    = Split-Path (Split-Path $Python)             # ...\agent-venv\Scripts\python.exe -> ...\agent-venv
function Step($m) { Write-Host "==> $m" }
function DoIt($desc, [scriptblock]$block) {
  if ($Apply) { Step "APPLY: $desc"; & $block } else { Step "PLAN:  $desc" }
}

# 0. Preconditions (both dry-run and apply)
if (-not (Test-Path (Join-Path $AgentDir "agent.py"))) { throw "agent.py not found under $AgentDir" }
if (-not (Test-Path $XmlSource))                       { throw "WinSW config not found: $XmlSource (bundle incomplete)" }

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

# XML CONTRACT VALIDATION. The identity/runtime guard validates $Python, but the SERVICE runs whatever the
# XML's <executable> says. Bind them: refuse unless the XML runs exactly the interpreter we validated and the
# agent under $AgentDir. Also enforce, from the reviewed XML, the two install-only invariants (no auto-restart
# recovery; a stop timeout that exceeds the configured drain so a stop cannot force-kill a mutation mid-drain).
function Test-GuvfxWinSwXmlContract {
  param([Parameter(Mandatory)][string]$XmlPath, [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string]$AgentDir)
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
  # (F5/F9) exactly one recovery entry and it must be 'none' - a second <onfailure> makes .onfailure an array
  $of = @($svc.onfailure)
  $ofActions = (@($of | ForEach-Object { [string]$_.action })) -join ','
  if ($of.Count -ne 1 -or "$($of[0].action)" -ne "none") {
    throw "XML recovery must be a single onfailure action=none entry; found $($of.Count) (actions: $ofActions)"
  }
  # (F7) stoptimeout must exceed the configured drain (machine env, else config.example default 45)
  $stopRaw = "$($svc.stoptimeout)"
  if ($stopRaw -match '^\s*(\d+)\s*sec\s*$') { $stopS = [int]$Matches[1] }
  else { throw "XML <stoptimeout> '$stopRaw' is not '<N> sec'" }
  $drainRaw = [Environment]::GetEnvironmentVariable("BETA_AGENT_DRAIN_TIMEOUT_S", "Machine")
  $drainS = 45; if ($drainRaw -and ($drainRaw -match '^\s*\d+(\.\d+)?\s*$')) { $drainS = [int][math]::Ceiling([double]$drainRaw) }
  if ($stopS -le $drainS) {
    throw "XML <stoptimeout> ${stopS}s must EXCEED BETA_AGENT_DRAIN_TIMEOUT_S (${drainS}s) or a stop force-kills a mutation mid-drain (B-6)"
  }
  if ("$($svc.startmode)" -ne "Manual") { throw "XML <startmode> is '$($svc.startmode)', expected Manual (no autostart)" }
  Write-Host "ok   XML contract: runs '$Python' on agent.py; recovery=none; stoptimeout ${stopS}s > drain ${drainS}s; startmode Manual"
}
Test-GuvfxWinSwXmlContract -XmlPath $XmlSource -Python $Python -AgentDir $AgentDir

# GLOBAL-WRITE MEASUREMENT (RULE 11 / evidence.md). The whole point of WinSW is that this install writes no
# pywin32 helper DLL to System32 or the base interpreter (the 2026-07-24 regression). We MEASURE that instead
# of asserting it: snapshot the two DLL names in both locations BEFORE any mutation, and at VERIFY assert this
# run created or modified neither. A DLL that already exists (from a prior postinstall) with an unchanged
# timestamp is reported as pre-existing - it is NOT evidence that THIS run wrote it.
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

# LSA interop for the SERVICE-LOGON right. HOST-PROVEN 2026-07-24: WinSW v2.12.0 does NOT apply
# <serviceaccount> (virtual-account support is a WinSW v3 feature; it installs LocalSystem), so identity is
# assigned AFTER install by `sc config obj=` and the SERVICE account is granted SeServiceLogonRight here (it
# is NOT auto-granted by sc config - secedit-verified). Same LSA policy API install_pool.ps1 uses for
# SeBatchLogonRight, but addressed by the DERIVED SID because a virtual account has no Get-LocalUser entry.
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
$STATUS_NAME_NOT_FOUND_SVC = [uint32]3221225524   # 0xC0000034 STATUS_OBJECT_NAME_NOT_FOUND (decimal: an 8-hex literal wraps to Int32)
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

# 3. Lay down the WinSW wrapper (renamed to the service id) + its reviewed XML config. Copies only - no
#    global writes, no pywin32.
DoIt "stage WinSW wrapper -> $ServiceExe and config -> $ServiceXml" {
  Copy-Item -Path $WinSwSource -Destination $ServiceExe -Force
  Copy-Item -Path $XmlSource   -Destination $ServiceXml -Force
  $exeHash = (Get-FileHash $ServiceExe -Algorithm SHA256).Hash.ToLower()
  if ($exeHash -ne $WinSwSha256.ToLower()) { throw "staged WinSW exe hash changed after copy - aborting" }
}

# 4. Register the service FROM the WinSW config (manual start, recovery none, STOPPED). WinSW writes no
#    global DLL. NOTE (host-proven 2026-07-24): WinSW v2.12.0 installs the service as LocalSystem regardless
#    of <serviceaccount>; the least-privilege identity is assigned in step 4a, not by the XML alone.
DoIt "register service '$ServiceName' via WinSW (manual start, STOPPED)" {
  & $ServiceExe install
  if ($LASTEXITCODE -ne 0) { throw "WinSW install failed (exit $LASTEXITCODE)" }
}

# 4a. Assign the NT SERVICE virtual account via the supported post-install mechanism (sc config obj=,
#     host-proven to take for a WinSW-created service), then grant it SeServiceLogonRight (NOT auto-granted).
#     The native sc.exe result is CAPTURED and VALIDATED (exit code + text) - never piped to Out-Null - and
#     any unexpected result is a STOP.
DoIt "assign identity: sc config obj= '$RunAsUser' + grant SeServiceLogonRight" {
  $scOut  = & sc.exe config $ServiceName obj= "$RunAsUser" 2>&1
  $scRc   = $LASTEXITCODE
  $scText = ($scOut | Out-String).Trim()
  Write-Host "evidence sc_config obj='$RunAsUser' exit=$scRc output=$scText"
  if ($scRc -ne 0 -or $scText -notmatch 'ChangeServiceConfig SUCCESS') {
    throw "sc config obj= failed (exit=$scRc): $scText - do NOT start"
  }
  Grant-GuvfxServiceLogonRight -ServiceSid $ServiceSid
}

# 5. Verify (no start).
if ($Apply) {
  Step "VERIFY service configuration (expect STOPPED, ProcessId 0, NT SERVICE identity + SeServiceLogonRight, Manual, recovery none, no global DLL)"
  $svc = Get-Service $ServiceName -ErrorAction Stop
  if ($svc.Status -ne "Stopped") { throw "service is $($svc.Status); expected Stopped (install-only)" }
  $ci = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
  # Identity must be EXACTLY the virtual account - reject LocalSystem/LocalService/NetworkService explicitly.
  if ("$($ci.StartName)" -ne $RunAsUser) {
    throw "service identity is '$($ci.StartName)', expected exactly '$RunAsUser' - no LocalSystem fallback; do NOT start"
  }
  if ($ci.ProcessId -ne 0) { throw "service ProcessId is $($ci.ProcessId), expected 0 (not running) - do NOT start" }
  if ($ci.StartMode -notin @("Manual","Disabled")) { throw "service StartMode is '$($ci.StartMode)', expected Manual - do NOT start" }
  if ("$($ci.PathName)" -notmatch [regex]::Escape($ServiceExe)) { throw "service binary is '$($ci.PathName)', expected the WinSW wrapper $ServiceExe" }
  # SeServiceLogonRight must be present or the (later, gated) first start fails 1069.
  $svcRights = Get-GuvfxSidRights -ServiceSid $ServiceSid
  if ($svcRights -notcontains 'SeServiceLogonRight') { throw "service account lacks SeServiceLogonRight - it cannot start; do NOT start" }
  Write-Host "ok   service: identity=$($ci.StartName)  pid=$($ci.ProcessId)  startmode=$($ci.StartMode)  state=$($svc.Status)  SeServiceLogonRight=present  bin=$($ci.PathName)"
  # (F9) recovery must be NONE - PARSE sc.exe qfailure, do not merely print it. sc.exe is a native exe, so its
  # output is text, not objects; a restart/reboot/run action anywhere in it fails the install-only gate.
  $qf = (& sc.exe qfailure $ServiceName) -join "`n"
  Write-Host $qf
  if ($qf -match '(?im)^\s*(RESTART|RUN PROCESS|REBOOT)\b' -or $qf -match '(?i)FAILURE_ACTIONS.*(RESTART|REBOOT|RUN)') {
    throw "service has SCM recovery actions configured; expected none (install-only) - do NOT start"
  }
  Write-Host "ok   SCM recovery is none (sc qfailure parsed, no RESTART/REBOOT/RUN action)"
  # (F10) MEASURED, not asserted: prove THIS run created/modified no pywin32 helper DLL globally.
  $after = Get-GuvfxGlobalDllState -Paths $GlobalDllPaths
  foreach ($p in $GlobalDllPaths) {
    $b = $GlobalDllBaseline[$p]; $a = $after[$p]
    if ((-not $b.exists) -and $a.exists) { throw "GLOBAL WRITE: this install created '$p' - the isolation guarantee is broken; do NOT start" }
    if ($b.exists -and $a.exists -and ($b.mtime -ne $a.mtime)) { throw "GLOBAL WRITE: this install modified '$p'; do NOT start" }
    $note = if ($a.exists) { "pre-existing, unchanged by this run" } else { "absent" }
    Write-Host "ok   global DLL $p : $note"
  }
  Write-Host "ok   WinSW install created/modified NO pywin32 DLL in System32 or the base interpreter (measured before/after)"
  foreach ($d in @($StateDir, $BetaTombstones, $SlotsRoot, $AgentDir, $GoldenDir, $WinSwDir, $VenvDir)) {
    $sids = @((Get-Acl -Path $d).GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value })
    if ($sids -notcontains $ServiceSid) { throw "no ACE for '$RunAsUser' ($ServiceSid) on $d - the grant did not take; do NOT start" }
    Write-Host "ok   DACL on $d carries an ACE for $RunAsUser"
  }
  Write-Host ""
  Write-Host "ok   service installed STOPPED via WinSW. Next: firewall.ps1 -Apply, then the FIRST-START gate."
  Write-Host "     The signing keyring (BETA_AGENT_KEYRING / _KEY_ID) must be provisioned by the operator before first start."
} else {
  Write-Host "PLAN complete. Re-run with -Apply on the host to perform the install (install-only, no start)."
}

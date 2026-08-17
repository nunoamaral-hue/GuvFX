<#
  Relaunch-GuvfxTerminal.ps1  (AJ#6.4 - LiveUpdate-safe post-login AutoTrading capability recovery)

  Gracefully closes and relaunches ONLY this tenant's own portable MT5 (terminal64.exe) in a way that survives
  MetaTrader LiveUpdate.

  AJ#6.3 root cause (proven in prod 2026-08-17 on support@/account 24): relaunching terminal64.exe /portable
  while a MetaTrader update is pending starts the LiveUpdate UPDATER
  (%APPDATA%\MetaQuotes\Terminal\<hash>\liveupdate\terminal64.exe /update), not the trading terminal - closing a
  healthy connected terminal and regressing the workspace to down.

  Corrective: BEFORE relaunch, apply the certified Variant-A LiveUpdate containment (host-proven 2026-07-31 in
  deploy/beta-agent/slot_launch.ps1::Apply-LiveUpdateContainment) - kill the tenant's own stuck updater, purge
  %APPDATA%\MetaQuotes\WebInstall + Terminal\<hash>\liveupdate, and Deny-write those staging paths for the tenant
  SID - so terminal64.exe /portable launches the canonical trading terminal, not the updater. The primitive then
  distinguishes trading vs updater by EXECUTABLE PATH and returns ok:true ONLY when a terminal64 at the canonical
  C:\GuvFX\accounts\<id>\terminal\terminal64.exe is running - fail-closed (containment_failed /
  relaunch_hit_liveupdate / trading_terminal_not_restored / tenant_resolution_failed) otherwise. It does not
  "guarantee" the launch by construction; the final trading-terminal check is the authoritative gate.

  Execution context. The whole primitive runs as the executor (LocalSystem), which is AppLocker-exempt. The
  hosted tenant (guvfx_u_<id>) runs under AppLocker deny-by-default, so a tenant-context PowerShell step is not
  possible (blocked, 0x800704EC) - and, importantly, the tenant CANNOT run mklink/cmd/any non-allowlisted tool,
  so it cannot plant a directory junction on a staging path. The LocalSystem containment therefore closes the
  confused-deputy at two layers: (1) AppLocker prevents the tenant creating a junction in the first place, and
  (2) defence-in-depth reparse-point rejection on the whole ancestor chain + a reparse-safe purge (a junction
  child is deleted as a LINK, never recursed through) + a re-check immediately before each DACL mutate. Only the
  tenant's OWN roaming update-staging is touched - never the runtime dir, another tenant's profile, or the
  operator estate. The tenant SID and REAL profile come from Win32_UserAccount + the ProfileList registry (no
  NTAccount name translation, which hangs on this workgroup host; no reconstructed path).

  Tenant-context steps that ARE AppLocker-allowed - the graceful close (taskkill.exe /PID) and the relaunch
  (terminal64.exe) - run through per-account tenant-principal scheduled tasks. Customer Zero (account 1 /
  guvfx_u_1) is refused as defence in depth. Process matching is tenant-specific (executable path / tracked PID)
  and NEVER taskkill /IM. This is CAPABILITY RECOVERY ONLY: it NEVER logs in, changes the broker account, arms a
  strategy, or places an order. All identity/path values are server-derived. ASCII-only (RULE 9 corollary).
#>
param(
  [Parameter(Mandatory = $true)][string]$Username,
  [Parameter(Mandatory = $true)][string]$TerminalRoot,
  [Parameter(Mandatory = $true)][int]$AccountId
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$RESERVED_ACCOUNT_IDS = @(1, 18)      # SACRED, NEVER relaunched: Customer Zero (1) + the account-18 control
$CLOSE_TIMEOUT_S = 30
$RELAUNCH_TIMEOUT_S = 60

$result = [ordered]@{
  account_id = $AccountId; username = $Username; contained = $false; trading_before = $false;
  closed = $false; relaunched = $false; ok = $false; reason = ""
}
function Emit() { $result | ConvertTo-Json -Compress }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

# Resolve the tenant's SID + REAL profile directory without NTAccount name translation (which hangs on this
# workgroup host). Win32_UserAccount is a local query; ProfileList is keyed by SID and authoritative for the
# profile path. Returns @{ sid=<string>; profile=<dir> } or $null (fail-closed).
function Resolve-Tenant([string]$user) {
  $acct = @(Get-CimInstance Win32_UserAccount -Filter ("Name='" + $user + "' AND LocalAccount=True") -ErrorAction SilentlyContinue)
  if ($acct.Count -ne 1 -or [string]::IsNullOrWhiteSpace($acct[0].SID)) { return $null }  # reject ambiguity
  $sid = $acct[0].SID
  try { $pi = (Get-ItemProperty ("HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\" + $sid) -Name ProfileImagePath -ErrorAction Stop).ProfileImagePath }
  catch { return $null }
  if ([string]::IsNullOrWhiteSpace($pi) -or -not (Test-Path -LiteralPath $pi)) { return $null }
  # Defence in depth: the authoritative profile must live under C:\Users and end with the tenant's own name.
  $fp = [System.IO.Path]::GetFullPath($pi)
  if (-not $fp.ToLower().StartsWith("c:\users\")) { return $null }
  if ([System.IO.Path]::GetFileName($fp.TrimEnd("\")).ToLower() -ne $user.ToLower()) { return $null }
  return @{ sid = $sid; profile = $fp }
}

# True iff NO component from $anchor down to $full (which must be under $anchor) is a reparse point / junction.
# Uses Get-Item (not Test-Path) for the presence test: a junction whose target has been deleted (dangling) can
# make Test-Path return false and silently skip the reparse check - Get-Item -Force still returns the link with
# its ReparsePoint attribute set.
function Test-ChainReparseFree([string]$anchor, [string]$full) {
  $a = $anchor.TrimEnd("\"); $f = $full.TrimEnd("\")
  if (-not $f.ToLower().StartsWith(($a + "\").ToLower())) { return $false }
  $cur = $a
  $it = Get-Item -LiteralPath $cur -Force -ErrorAction SilentlyContinue
  if ($it -and (($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { return $false }
  foreach ($part in $f.Substring($a.Length).Trim("\").Split("\")) {
    if ($part -eq "") { continue }
    $cur = Join-Path $cur $part
    $it = Get-Item -LiteralPath $cur -Force -ErrorAction SilentlyContinue
    if ($it -and (($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { return $false }
  }
  return $true
}

# Certified Variant-A LiveUpdate containment, run as LocalSystem on the tenant's OWN update staging. Returns
# $true only when the Deny(Write) is read-back verified on every target; fail-closed on any reparse point.
function Apply-LiveUpdateContainment($tenant) {
  $sid = New-Object System.Security.Principal.SecurityIdentifier($tenant.sid)
  $mqRoot = Join-Path (Join-Path $tenant.profile "AppData\Roaming") "MetaQuotes"
  # 1) Kill this tenant's OWN stuck updater (terminal64 whose exe lives under the tenant profile liveupdate).
  foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue)) {
    $exe = ""
    if ($p.ExecutablePath) { $exe = [string]$p.ExecutablePath }
    if (($exe -match "(?i)\\liveupdate\\") -and ($exe.ToLower().StartsWith(($tenant.profile.TrimEnd("\") + "\").ToLower()))) {
      try { Stop-Process -Id ([int]$p.ProcessId) -Force -ErrorAction Stop } catch {}
    }
  }
  Start-Sleep -Seconds 2
  # 2) Deny-write + purge the tenant's OWN staging (WebInstall = load-bearing chokepoint; per-hash liveupdate).
  $targets = New-Object System.Collections.Generic.List[string]
  $targets.Add((Join-Path $mqRoot "WebInstall"))
  $terminalRoot = Join-Path $mqRoot "Terminal"
  if ((Test-Path -LiteralPath $terminalRoot) -and (Test-ChainReparseFree $tenant.profile $terminalRoot)) {
    foreach ($d in (Get-ChildItem -LiteralPath $terminalRoot -Directory -ErrorAction SilentlyContinue)) {
      if (($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) { $targets.Add((Join-Path $d.FullName "liveupdate")) }
    }
  }
  $writeRights = [System.Security.AccessControl.FileSystemRights]::Write
  $inherit = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  foreach ($t in $targets) {
    if (-not (Test-ChainReparseFree $tenant.profile $t)) { return $false }
    if (-not (Test-Path -LiteralPath $t)) { New-Item -ItemType Directory -Force -Path $t | Out-Null }
    # Reparse-safe purge: a junction child is removed as a LINK, never recursed through.
    foreach ($c in (Get-ChildItem -LiteralPath $t -Force -ErrorAction SilentlyContinue)) {
      if (($c.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $c.Delete() }
      else { Remove-Item -LiteralPath $c.FullName -Recurse -Force -ErrorAction Stop }
    }
    # Re-check immediately before the DACL mutate (shrink the TOCTOU window).
    if (((Get-Item -LiteralPath $t -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    $acl = Get-Acl -LiteralPath $t
    $deny = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $writeRights, $inherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)
    [void]$acl.RemoveAccessRule($deny)
    [void]$acl.AddAccessRule($deny)
    Set-Acl -LiteralPath $t -AclObject $acl
    $ok = $false
    foreach ($r in (Get-Acl -LiteralPath $t).GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
      if ($r.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny -and
          (-not $r.IsInherited) -and
          $r.IdentityReference.Value -eq $sid.Value -and
          (([int]$r.FileSystemRights -band [int]$writeRights) -eq [int]$writeRights)) { $ok = $true }
    }
    if (-not $ok) { return $false }
  }
  return $true
}

# Tenant TRADING terminals, identified by the canonical executable path (tenant-specific by NTFS ACL, so this
# needs no owner attribution and is immune to a transient GetOwner=null under-count). Returns PIDs.
function Get-TenantTradingPids([string]$expectedExe) {
  $out = New-Object System.Collections.Generic.List[int]
  foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue)) {
    $exe = ""
    if ($p.ExecutablePath) { $exe = [string]$p.ExecutablePath }
    if ($exe -ne "" -and $exe.ToLower() -eq $expectedExe.ToLower()) { [void]$out.Add([int]$p.ProcessId) }
  }
  return $out
}

# Is any LiveUpdate updater for THIS tenant running (executable under the tenant profile's liveupdate)?
function Any-TenantUpdater([string]$profile) {
  foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue)) {
    $exe = ""
    if ($p.ExecutablePath) { $exe = [string]$p.ExecutablePath }
    if (($exe -match "(?i)\\liveupdate\\") -and ($exe.ToLower().StartsWith(($profile.TrimEnd("\") + "\").ToLower()))) { return $true }
  }
  return $false
}

try {
  # ---- Confinement (defence in depth; the dispatcher already derived every value + refused reserved ids) ----
  if ($RESERVED_ACCOUNT_IDS -contains $AccountId) { Fail "refusing_reserved_identity" }
  if ($AccountId -le 0) { Fail "refusing_account_id_out_of_range" }
  if ($Username -ne ("guvfx_u_" + $AccountId)) { Fail "refusing_username_mismatch" }
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "refusing_path_traversal" }
  $expected = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $ACCOUNTS_BASE ([string]$AccountId)) "terminal"))
  if ($full.ToLower() -ne $expected.ToLower()) { Fail "refusing_terminal_root_mismatch" }
  if (-not (Test-Path -LiteralPath $full)) { Fail "terminal_root_missing" }
  $exe = Join-Path $full "terminal64.exe"
  if (-not (Test-Path -LiteralPath $exe)) { Fail "terminal64_missing" }
  $expectedExe = [System.IO.Path]::GetFullPath($exe)

  $tenant = Resolve-Tenant $Username
  if ($null -eq $tenant) { Fail "tenant_resolution_failed" }
  $tenantPrincipal = New-ScheduledTaskPrincipal -UserId $Username -LogonType Interactive -RunLevel Limited

  # ---- 1) LiveUpdate containment BEFORE relaunch (LocalSystem; reparse-safe; deny tenant write; fail-closed) --
  $contained = $false
  try { $contained = Apply-LiveUpdateContainment $tenant } catch { $contained = $false }
  $result.contained = $contained
  if (-not $contained) { Fail "containment_failed" }

  # ---- 2) Gracefully close the tenant's TRADING terminal by tracked PID (never taskkill /IM; detection is
  #         PID disappearance, never an owner recompute). AppLocker allows taskkill.exe for the tenant. ----
  $tradingPids = Get-TenantTradingPids $expectedExe
  $result.trading_before = ($tradingPids.Count -ge 1)
  if ($tradingPids.Count -ge 1) {
    $closeTask = "GuvFX_HostedClose_" + $AccountId
    $pidArgs = ($tradingPids | ForEach-Object { "/PID " + $_ }) -join " "
    $closeAction = New-ScheduledTaskAction -Execute "C:\Windows\System32\taskkill.exe" -Argument $pidArgs
    Register-ScheduledTask -TaskName $closeTask -Action $closeAction -Principal $tenantPrincipal -Force | Out-Null
    Start-ScheduledTask -TaskName $closeTask
    $deadline = (Get-Date).AddSeconds($CLOSE_TIMEOUT_S)
    while ((Get-Date) -lt $deadline) {
      $alive = $false
      foreach ($tp in $tradingPids) { if (Get-Process -Id $tp -ErrorAction SilentlyContinue) { $alive = $true } }
      if (-not $alive) { $result.closed = $true; break }
      Start-Sleep -Milliseconds 500
    }
    if (-not $result.closed) { Fail "close_timeout" }
  }
  else {
    # Nothing to close (restoration from a fully-down / updater-only state).
    $result.closed = $true
  }

  # ---- 3+4) Relaunch and bounded wait for the ACTUAL trading terminal - an updater is NEVER accepted as
  #  success. Retried at most once for a transient Session-0 launch no-op (containment is already in force, so a
  #  second launch is cheap and safe, and it shrinks the window in which the accepted close-before-relaunch
  #  regression could leave a previously-CONNECTED terminal DOWN). Note (accepted architectural constraint):
  #  MT5 /portable is a singleton per data directory, so a launch-verify-then-close atomic swap is impossible -
  #  the close must precede the relaunch. A PERSISTENT relaunch failure will therefore leave the terminal DOWN;
  #  this fails CLOSED (never ok:true with a dead terminal) and the next recovery pass relaunches the down
  #  runtime (trading_before=False, no close), so the regression is bounded to a single, self-recovering
  #  occurrence. ----
  $relTask = "GuvFX_HostedRelaunch_" + $AccountId
  $relAction = New-ScheduledTaskAction -Execute $exe -Argument "/portable"
  Register-ScheduledTask -TaskName $relTask -Action $relAction -Principal $tenantPrincipal -Force | Out-Null
  $sawUpdater = $false
  $attempt = 0
  while ($attempt -lt 2 -and (-not $result.relaunched) -and (-not $sawUpdater)) {
    $attempt = $attempt + 1
    Start-ScheduledTask -TaskName $relTask
    $deadline = (Get-Date).AddSeconds($RELAUNCH_TIMEOUT_S)
    while ((Get-Date) -lt $deadline) {
      if ((Get-TenantTradingPids $expectedExe).Count -ge 1) { $result.relaunched = $true; break }
      if (Any-TenantUpdater $tenant.profile) { $sawUpdater = $true; break }   # containment breach - fail closed
      Start-Sleep -Milliseconds 500
    }
  }
  if (-not $result.relaunched) {
    if ($sawUpdater) { Fail "relaunch_hit_liveupdate" } else { Fail "trading_terminal_not_restored" }
  }

  $result.ok = $true
  $result.reason = "ok"
  Emit
  exit 0
}
catch {
  Fail "relaunch_exception"
}

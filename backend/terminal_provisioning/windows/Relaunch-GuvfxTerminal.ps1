<#
  Relaunch-GuvfxTerminal.ps1  (AJ#6.4 - LiveUpdate-safe post-login AutoTrading capability recovery)

  Gracefully closes and relaunches ONLY this tenant's own portable MT5 (terminal64.exe) in a way that survives
  MetaTrader LiveUpdate.

  AJ#6.3 root cause (proven in prod 2026-08-17 on support@/account 24): relaunching terminal64.exe /portable
  while a MetaTrader update is pending starts the LiveUpdate UPDATER
  (%APPDATA%\MetaQuotes\Terminal\<hash>\liveupdate\terminal64.exe /update), not the trading terminal - closing a
  healthy connected terminal and regressing the workspace to down.

  Corrective: BEFORE relaunch, apply the certified Variant-A LiveUpdate containment (host-proven 2026-07-31 in
  deploy/beta-agent/slot_launch.ps1::Apply-LiveUpdateContainment) - deny the tenant WRITE on its OWN MT5 update
  staging (%APPDATA%\MetaQuotes\WebInstall + Terminal\<hash>\liveupdate) and purge any staged update. That
  containment runs INSIDE the tenant's own session as the tenant user (a Limited token), EXACTLY like the
  certified prior art: the tenant token bounds the blast radius, so a tenant-planted directory junction on a
  staging path can never make this a confused deputy that reaches Customer Zero / another tenant / the operator
  estate (NTFS denies the tenant token), and $env:APPDATA / GetCurrent().User resolve the tenant's REAL profile
  and SID with no NTAccount name translation (which hangs on this workgroup host).

  The orchestrator runs as the executor (LocalSystem) and NEVER performs a recursive delete or Set-Acl over a
  tenant-writable path; it only registers/starts the per-account tenant tasks (containment, graceful close,
  relaunch) and OBSERVES. It distinguishes the TRADING terminal from the UPDATER by EXECUTABLE PATH (the
  canonical C:\GuvFX\accounts\<id>\terminal\terminal64.exe is tenant-specific by NTFS ACL) and returns ok:true
  ONLY when the actual trading terminal is running - fail-closed (containment_failed / relaunch_hit_liveupdate /
  trading_terminal_not_restored) otherwise. It does not "guarantee" the launch by construction; the final
  trading-terminal check is the authoritative gate (the fresh-slot WebInstall proof does not itself cover an
  already-staged update - that case is proven by the AJ#6.4 on-host certification).

  Customer Zero (account 1 / guvfx_u_1) is refused as defence in depth (the signed dispatcher already refuses
  reserved identities). Process matching is tenant-specific (executable path / tracked PID) and NEVER
  taskkill /IM. This is CAPABILITY RECOVERY ONLY: it NEVER logs in, changes the broker account, arms a strategy,
  or places an order. All identity/path values are server-derived. ASCII-only (RULE 9 corollary).
#>
param(
  [Parameter(Mandatory = $true)][string]$Username,
  [Parameter(Mandatory = $true)][string]$TerminalRoot,
  [Parameter(Mandatory = $true)][int]$AccountId,
  [string]$Step = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$RESERVED_ACCOUNT_IDS = @(1)          # Customer Zero - NEVER relaunched via this primitive
$CLOSE_TIMEOUT_S = 30
$RELAUNCH_TIMEOUT_S = 60
$CONTAIN_TIMEOUT_S = 60

# ==========================================================================================================
#  TENANT STEP - LiveUpdate containment, run AS the tenant (a Limited token) via the per-account task.
#  Mirrors slot_launch.ps1::Apply-LiveUpdateContainment: uses the tenant's OWN $env:APPDATA and token SID
#  (GetCurrent().User - no NTAccount translation) so the tenant token bounds the blast radius. Exit 0 on
#  success; any non-zero code is a containment failure the orchestrator maps to containment_failed.
# ==========================================================================================================
if ($Step -eq "Contain") {
  try {
    $roaming = $env:APPDATA
    if ([string]::IsNullOrWhiteSpace($roaming)) { exit 3 }
    $mqRoot = Join-Path $roaming "MetaQuotes"
    # 1) Kill this tenant's OWN stuck updater first (releases its handle on the staged exe before the purge).
    foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue)) {
      $exe = ""
      if ($p.ExecutablePath) { $exe = [string]$p.ExecutablePath }
      if ($exe -match '(?i)\\liveupdate\\') { try { Stop-Process -Id ([int]$p.ProcessId) -Force -ErrorAction Stop } catch {} }
    }
    Start-Sleep -Seconds 1
    # 2) Deny-write + purge the tenant's OWN update staging.
    $sid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User
    $targets = New-Object System.Collections.Generic.List[string]
    $targets.Add((Join-Path $mqRoot "WebInstall"))
    $terminalRoot = Join-Path $mqRoot "Terminal"
    if (Test-Path -LiteralPath $terminalRoot) {
      foreach ($d in (Get-ChildItem -LiteralPath $terminalRoot -Directory -ErrorAction SilentlyContinue)) {
        $targets.Add((Join-Path $d.FullName "liveupdate"))
      }
    }
    $writeRights = [System.Security.AccessControl.FileSystemRights]::Write
    $inherit = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($t in $targets) {
      # Defence in depth (belt-and-braces to the tenant-token boundary): refuse a reparse point / junction.
      if ((Test-Path -LiteralPath $t) -and ((([System.IO.FileInfo](Get-Item -LiteralPath $t -Force)).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { exit 4 }
      if (-not (Test-Path -LiteralPath $t)) { New-Item -ItemType Directory -Force -Path $t | Out-Null }
      # Purge staged payload; delete a reparse-point child as a LINK (never recurse through a junction).
      foreach ($c in (Get-ChildItem -LiteralPath $t -Force -ErrorAction SilentlyContinue)) {
        if (($c.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $c.Delete() }
        else { Remove-Item -LiteralPath $c.FullName -Recurse -Force -ErrorAction Stop }
      }
      $acl = Get-Acl -LiteralPath $t
      $deny = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $writeRights, $inherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)
      [void]$acl.RemoveAccessRule($deny)
      [void]$acl.AddAccessRule($deny)
      Set-Acl -LiteralPath $t -AclObject $acl
      # Positive control (RULE 11): read the DACL back and confirm the tenant-SID Deny(Write) is in force.
      $ok = $false
      foreach ($r in (Get-Acl -LiteralPath $t).GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
        if ($r.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny -and
            (-not $r.IsInherited) -and
            $r.IdentityReference.Value -eq $sid.Value -and
            (([int]$r.FileSystemRights -band [int]$writeRights) -eq [int]$writeRights)) { $ok = $true }
      }
      if (-not $ok) { exit 5 }
    }
    exit 0
  }
  catch { exit 2 }
}

# ==========================================================================================================
#  ORCHESTRATOR - runs as the executor (LocalSystem). It NEVER deletes/ACLs a tenant-writable path; it only
#  registers/starts the per-account tenant tasks and observes.
# ==========================================================================================================
$result = [ordered]@{
  account_id = $AccountId; username = $Username; contained = $false; trading_before = $false;
  closed = $false; relaunched = $false; ok = $false; reason = ""
}
function Emit() { $result | ConvertTo-Json -Compress }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

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
function Any-TenantUpdater([string]$user) {
  foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue)) {
    $exe = ""
    if ($p.ExecutablePath) { $exe = [string]$p.ExecutablePath }
    if ($exe -match '(?i)\\liveupdate\\' -and $exe -match ('(?i)\\' + [regex]::Escape($user) + '\\')) { return $true }
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
  $self = $PSCommandPath
  if ([string]::IsNullOrWhiteSpace($self)) { Fail "self_path_unresolved" }

  $tenantPrincipal = New-ScheduledTaskPrincipal -UserId $Username -LogonType Interactive -RunLevel Limited

  # ---- 1) LiveUpdate containment via the TENANT task (Limited token bounds the blast radius) ----
  $containTask = "GuvFX_HostedContain_" + $AccountId
  $argLine = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + $self + '" -Username ' + $Username + ' -TerminalRoot "' + $full + '" -AccountId ' + $AccountId + ' -Step Contain'
  $containAction = New-ScheduledTaskAction -Execute "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $argLine
  Register-ScheduledTask -TaskName $containTask -Action $containAction -Principal $tenantPrincipal -Force | Out-Null
  $before = (Get-ScheduledTaskInfo -TaskName $containTask).LastRunTime
  Start-ScheduledTask -TaskName $containTask
  $deadline = (Get-Date).AddSeconds($CONTAIN_TIMEOUT_S)
  while ((Get-Date) -lt $deadline) {
    $info = Get-ScheduledTaskInfo -TaskName $containTask
    if ($info.State -ne "Running" -and $info.LastRunTime -gt $before) { break }
    Start-Sleep -Milliseconds 500
  }
  $info = Get-ScheduledTaskInfo -TaskName $containTask
  if ($info.LastRunTime -le $before) { Fail "containment_task_did_not_run" }
  if ($info.LastTaskResult -ne 0) { Fail "containment_failed" }
  $result.contained = $true

  # ---- 2) Gracefully close the tenant's TRADING terminal by tracked PID (never taskkill /IM; detection is
  #         PID disappearance, never an owner recompute) ----
  $tradingPids = Get-TenantTradingPids $expectedExe
  $result.trading_before = ($tradingPids.Count -ge 1)
  if ($tradingPids.Count -ge 1) {
    $closeTask = "GuvFX_HostedClose_" + $AccountId
    $pidArgs = ($tradingPids | ForEach-Object { "/PID " + $_ }) -join " "
    # No /F: a graceful WM_CLOSE to specific tenant PIDs only. Cannot touch another tenant / Customer Zero.
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
  #  second launch is cheap and safe, and it shrinks the window in which the confirmed close-before-relaunch
  #  regression could leave a previously-CONNECTED terminal DOWN). Note (accepted architectural constraint):
  #  MT5 /portable is a singleton per data directory, so a launch-verify-then-close atomic swap is impossible -
  #  the close must precede the relaunch. A PERSISTENT relaunch failure (e.g. a genuinely broken binary) will
  #  therefore leave the terminal DOWN; this fails CLOSED (never ok:true with a dead terminal) and the next
  #  recovery pass relaunches the down runtime (trading_before=False, no close), so the regression is bounded to
  #  a single, self-recovering occurrence. ----
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
      if (Any-TenantUpdater $Username) { $sawUpdater = $true; break }   # containment breach - fail closed, do not retry
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

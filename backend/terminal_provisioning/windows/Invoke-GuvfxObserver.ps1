<#
  STREAM 9E - trigger the account's session-bound observer ONCE and return its snapshot (read-only).

  Runs as the LocalSystem hosted-executor daemon. It performs NO MT5 IPC itself (a session-0 attach cannot
  reach a hosted user's terminal); it ORCHESTRATES only: prove the tenant identity/session/terminal, delete
  any stale result, Start-ScheduledTask the per-account observer (which runs AS guvfx_u_<id> and does the
  guarded attach + read), wait bounded, then validate + return the fresh snapshot. Every identity/path/task is
  DERIVED here from the server-supplied -AccountId; nothing (command/script/username/path/task/result file) is
  accepted from the caller. Customer Zero (account 1) is refused. Fail-closed everywhere. Emits ONE compact
  JSON line (the RawWorkspaceSnapshot fields). ASCII-only (RULE 9).

  Usage (invoked by the primitive runner with a fixed argv):
    powershell -NoProfile -File Invoke-GuvfxObserver.ps1 -Username guvfx_u_18 -RuntimeRoot 'C:\GuvFX\accounts\18' -TerminalRoot 'C:\GuvFX\accounts\18\terminal' -AccountId 18
#>
param(
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [Parameter(Mandatory=$true)][string]$TerminalRoot,
  [Parameter(Mandatory=$true)][int]$AccountId,
  [int]$TimeoutSeconds = 60,
  [string]$ObserverDir = "C:\GuvFX\observer"
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"

# The single compact-JSON verdict. Every UNPROVEN fact stays False / every unknown identity stays null.
$snap = [ordered]@{
  ok=$false; account_id=$AccountId;
  process_running=$false; attach_attempted=$false; attach_succeeded=$false; ipc_available=$false;
  terminal_connected=$false; trade_allowed=$false;
  observed_login=$null; observed_server=$null; observed_trade_mode=$null; observed_at=$null;
  attach_reason=""; process_reason=""; connection_reason=""; corroboration=$null; reason=""
}
function Emit([string]$why) { if ($why) { $snap.reason = $why }; ($snap | ConvertTo-Json -Compress -Depth 5); if (-not $snap.ok) { exit 1 } else { exit 0 } }

# --- LocalSystem network corroboration -------------------------------------------------------------------------
# The tenant snapshot's broker facts are tenant-attested (forgeable in-session). LocalSystem independently proves
# the OBJECTIVE facts a tenant cannot forge: the proven terminal process/owner/session/path (above) plus the raw
# set of ESTABLISHED remote endpoints owned by that exact terminal PID. This function ONLY ENUMERATES (no
# public/private classification) - the load-bearing public-vs-private decision is made by the BACKEND classifier
# (tested off-host with RULE 11 positive/negative controls on its real code path). Fail-closed: any query
# failure yields an EMPTY list (no corroboration of a live link), never a false endpoint.
function Get-TerminalRemoteEndpoints([int]$procId) {
  $eps = New-Object System.Collections.ArrayList
  try {
    $conns = Get-NetTCPConnection -OwningProcess $procId -State Established -ErrorAction Stop
    foreach ($c in @($conns)) { $ip = "$($c.RemoteAddress)"; if ($ip) { [void]$eps.Add($ip) } }
    return ,$eps.ToArray()
  } catch {}
  try {
    $out = & netstat -ano
    foreach ($line in @($out)) {
      $t = "$line".Trim()
      if ($t -notmatch "^TCP\s") { continue }
      $parts = $t -split "\s+"
      if ($parts.Count -lt 5) { continue }
      if ($parts[3] -ne "ESTABLISHED") { continue }
      if ($parts[4] -notmatch "^[0-9]+$") { continue }
      if ([int]$parts[4] -ne $procId) { continue }
      $remote = $parts[2]
      if ($remote.StartsWith("[")) { $ip = $remote.Substring(1, $remote.IndexOf("]") - 1) }
      else { $ip = $remote.Substring(0, $remote.LastIndexOf(":")) }
      if ($ip) { [void]$eps.Add($ip) }
    }
  } catch {}
  return ,$eps.ToArray()
}

try {
  # 1-5. Server-derived identity contract. Refuse anything that is not the exact derivation from AccountId.
  if ($AccountId -le 0 -or $AccountId -eq 1) { Emit "reserved_or_invalid_account" }
  $expectedUser = "guvfx_u_$AccountId"
  if ($Username -ne $expectedUser) { Emit "username_mismatch" }
  $expectedRuntime = Join-Path $ACCOUNTS_BASE "$AccountId"
  $expectedTerminalRoot = Join-Path $expectedRuntime "terminal"
  $expectedTermExe = Join-Path $expectedTerminalRoot "terminal64.exe"
  $taskName = "GuvFX_HostedObserver_$AccountId"
  $resultPath = Join-Path (Join-Path $expectedRuntime "_obs") "observation.json"
  if ([System.IO.Path]::GetFullPath($RuntimeRoot) -ne [System.IO.Path]::GetFullPath($expectedRuntime)) { Emit "runtime_mismatch" }
  if ([System.IO.Path]::GetFullPath($TerminalRoot) -ne [System.IO.Path]::GetFullPath($expectedTerminalRoot)) { Emit "terminal_root_mismatch" }

  # 6. Prove the hosted user exists.
  if (-not (Get-LocalUser -Name $expectedUser -ErrorAction SilentlyContinue)) { Emit "no_user" }

  # 7-9. Prove EXACTLY ONE expected hosted terminal, in an interactive session, owned by the hosted user, at
  #      the tenant path. This simultaneously proves: the session exists (SessionId>0), a single unambiguous
  #      terminal, the correct owner, and the correct path (no legacy IS6 / Customer-Zero terminal confusion).
  $procs = @(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue)
  $matched = @()
  foreach ($p in $procs) {
    $exe = $null; try { $exe = [System.IO.Path]::GetFullPath($p.ExecutablePath) } catch { $exe = $null }
    if (-not $exe -or ($exe -ne [System.IO.Path]::GetFullPath($expectedTermExe))) { continue }
    $ownerUser = $null
    try { $o = Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction SilentlyContinue; $ownerUser = $o.User } catch { $ownerUser = $null }
    if ($ownerUser -ne $expectedUser) { continue }
    if ([int]$p.SessionId -le 0) { continue }
    $matched += $p
  }
  if ($matched.Count -eq 0) { Emit "terminal_not_running" }
  if ($matched.Count -gt 1) { Emit "duplicate_terminal" }
  $snap.process_running = $true
  $sessionId = [int]$matched[0].SessionId

  # 10. Stale-protect: delete any prior result BEFORE triggering, so we can only ever return a FRESH snapshot.
  $triggerAt = Get-Date
  if (Test-Path -LiteralPath $resultPath) { Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue }

  # Verify the observer task exists and its action points ONLY at the approved observer tooling (no drift).
  $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
  if (-not $task) { Emit "no_observer_task" }
  $obsPy = Join-Path $ObserverDir "run_observer.py"
  $actionsOk = $false
  foreach ($a in @($task.Actions)) {
    $ex = "$($a.Execute)"; $arg = "$($a.Arguments)"
    if ($ex -match "(?i)\bpy(w)?(\.exe)?$|python(\.exe)?$" -and $arg -match [regex]::Escape($obsPy) -and $arg -match "--account\s+$AccountId(\b|$)") { $actionsOk = $true }
  }
  if (-not $actionsOk) { Emit "observer_task_action_untrusted" }

  # 11. Trigger the on-demand observer task (runs AS guvfx_u_<id>).
  try { Start-ScheduledTask -TaskName $taskName -ErrorAction Stop } catch { Emit "task_launch_failed" }

  # 12. Bounded wait for a FRESH result file (written after the trigger).
  $deadline = (Get-Date).AddSeconds([Math]::Max(5, $TimeoutSeconds))
  $found = $false
  while ((Get-Date) -lt $deadline) {
    if (Test-Path -LiteralPath $resultPath) {
      try { $w = (Get-Item -LiteralPath $resultPath).LastWriteTime } catch { $w = $null }
      if ($w -and $w -ge $triggerAt) { $found = $true; break }
    }
    Start-Sleep -Milliseconds 500
  }
  if (-not $found) { Emit "observation_timeout" }

  # 13-14. Read + validate the result belongs to THIS account and is fresh; carry the approved fields through.
  $raw = $null
  try { $raw = Get-Content -LiteralPath $resultPath -Raw -ErrorAction Stop } catch { Emit "result_unreadable" }
  $obj = $null
  try { $obj = $raw | ConvertFrom-Json -ErrorAction Stop } catch { Emit "result_malformed" }
  if ([int]$obj.account_id -ne $AccountId) { Emit "result_account_mismatch" }
  if ($null -eq $obj.observed_at) { Emit "result_no_timestamp" }
  $snap.attach_attempted   = [bool]$obj.attach_attempted
  $snap.attach_succeeded   = [bool]$obj.attach_succeeded
  $snap.ipc_available      = [bool]$obj.ipc_available
  $snap.terminal_connected = [bool]$obj.terminal_connected
  $snap.trade_allowed      = [bool]$obj.trade_allowed
  $snap.observed_login     = $obj.observed_login
  $snap.observed_server    = $obj.observed_server
  if ($obj.observed_trade_mode -is [int]) { $snap.observed_trade_mode = [int]$obj.observed_trade_mode }
  $snap.observed_at        = [double]$obj.observed_at
  $snap.attach_reason      = "$($obj.attach_reason)"
  $snap.process_reason     = "$($obj.process_reason)"
  $snap.connection_reason  = "$($obj.connection_reason)"
  $snap.ok = [bool]$obj.ok

  # 15. LocalSystem corroboration - gathered from LocalSystem's OWN view of the proven terminal (never from the
  #     tenant file). Combined with the tenant snapshot and agreed on the backend before any lifecycle advance.
  $endpoints = @(Get-TerminalRemoteEndpoints ([int]$matched[0].ProcessId))
  $snap.corroboration = [ordered]@{
    account_id            = $AccountId
    process_present       = $true
    exe_path              = [System.IO.Path]::GetFullPath($expectedTermExe)
    owner_user            = $expectedUser
    session_id            = $sessionId
    runtime_root          = [System.IO.Path]::GetFullPath($expectedRuntime)
    remote_endpoints      = $endpoints
    collected_at          = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
  }
  Emit ""
}
catch { $snap.ok = $false; Emit "observer_primitive_error" }

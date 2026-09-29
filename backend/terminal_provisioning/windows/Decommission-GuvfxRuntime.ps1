<#
  Remove Broker Account - STAGE 2 physical host teardown for ONE removed account (idempotent, per-account).

  Runs on the MT5 host via the signed executor (primitive: decommission_runtime). Logical removal (tombstone,
  credential destroy, endpoint retire, entitlement release) has ALREADY happened in the GuvFX DB and is
  authoritative; this script only reclaims the Windows runtime footprint of that ONE account and NEVER un-does
  the logical removal. It terminates ONLY resources it can PROVE belong to this account:
    - the tenant bridge is targeted by its per-tenant LISTENING PORT (server-derived from the account's own
      retired endpoint), which the backend keeps RESERVED until this cleanup SUCCEEDS (so the port cannot be
      reallocated to another tenant mid-teardown); host-side we STILL only kill a port owner proven to be a GuvFX
      order bridge (its command line runs mt5_signal_bridge) or owned by this identity - never an arbitrary reuser;
    - the terminal64 is terminated ONLY when its owner == guvfx_u_<id> AND its exe path is under RuntimeRoot\
      (trailing separator: 37 never matches 370/375; never taskkill /IM, never by image name, never a sibling);
    - directories are canonicalized, their leaf MUST equal the account id, and they are refused if the tree
      contains a reparse point (junction/symlink) so a recursive delete can never escape the account subtree.
  Customer Zero (account 1) and any id < 2 are refused. Every step is best-effort + idempotent (already-absent
  counts as ok); a step that leaves residual makes ok=$false so the backend marks the cleanup FAILED_RETRYABLE
  and retries. ASCII-only (RULE 9). Emits exactly ONE compact JSON object as its final line.

  Usage:
    powershell -NoProfile -File Decommission-GuvfxRuntime.ps1 -AccountId 37 -Username guvfx_u_37 -RuntimeRoot "C:\GuvFX\accounts\37" -Port 8806
#>
param(
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [int]$Port = 0
)
$ErrorActionPreference = "Continue"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$TENANTS_BASE  = "C:\GuvFX\tenants"
$steps = [ordered]@{}
$result = [ordered]@{ account_id=$AccountId; ok=$false; steps=$steps; reason="" }

function Emit([string]$why) {
  $result.reason = $why
  $failed = @($steps.Keys | Where-Object { "$($steps[$_])".StartsWith("failed") })
  $result.ok = ($failed.Count -eq 0) -and ($why -eq "")
  if ($failed.Count -gt 0 -and $why -eq "") { $result.reason = "residual:" + ($failed -join ",") }
  $result | ConvertTo-Json -Compress -Depth 5
}

function Remove-TaskSafe([string]$name) {
  # Idempotent: End+Unregister a scheduled task. Returns "ok" | "absent" | "failed:<type>".
  try {
    $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if (-not $t) { return "absent" }
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
    return "ok"
  } catch { return ("failed:" + $_.Exception.GetType().Name) }
}

function Test-TreeNoReparse([string]$p) {
  # $true iff $p exists, is NOT itself a reparse point, and has NO descendant reparse point (junction/symlink),
  # so a subsequent Remove-Item -Recurse can never follow a link out of the account subtree.
  try {
    $root = Get-Item -LiteralPath $p -Force -ErrorAction Stop
    if ([int]($root.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    $rp = Get-ChildItem -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue |
          Where-Object { [int]($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0 } |
          Select-Object -First 1
    return (-not $rp)
  } catch { return $false }
}

# ---- Guards (fail closed, never emit a partial teardown against the wrong target) ----
if ($AccountId -lt 2) { Emit "refused_reserved_account"; exit 1 }
if ($Username -ne ("guvfx_u_" + $AccountId)) { Emit "refused_identity_mismatch"; exit 1 }
try { $rt = [System.IO.Path]::GetFullPath($RuntimeRoot) } catch { Emit "refused_bad_runtime_path"; exit 1 }
$rtTrim = $rt.TrimEnd("\")
$expectRt = (Join-Path $ACCOUNTS_BASE ([string]$AccountId))
if ($rtTrim.ToLower() -ne $expectRt.ToLower()) { Emit "refused_runtime_not_account_leaf"; exit 1 }
$rtPrefix = $rtTrim.ToLower() + "\"       # trailing separator: accounts\37\ never matches accounts\370\ / accounts\375\
$tenantDir = Join-Path $TENANTS_BASE ([string]$AccountId)

# ---- Step 1: watchdog task FIRST (so it cannot relaunch the bridge mid-teardown) ----
$steps.watchdog_task = Remove-TaskSafe ("GuvFX_TenantBridgeWatchdog_" + $AccountId)
# ---- Step 2: tenant bridge task (ends the launcher; prevents restart) ----
$steps.bridge_task = Remove-TaskSafe ("GuvFX_TenantBridge_" + $AccountId)
# ---- Step 2b: legacy per-account close/relaunch tasks (registered -Force elsewhere; reclaim them too) ----
$c = Remove-TaskSafe ("GuvFX_HostedClose_" + $AccountId)
$r = Remove-TaskSafe ("GuvFX_HostedRelaunch_" + $AccountId)
$steps.legacy_tasks = $(if (($c -like "failed*") -or ($r -like "failed*")) { "failed:close=$c,relaunch=$r" } else { "close=$c,relaunch=$r" })

# ---- Step 3: bridge PROCESS by its per-tenant listening port, WITH ownership proof ----
# The port is server-derived + kept reserved by the backend until this cleanup SUCCEEDS, so it still maps to THIS
# account. Host-side defence in depth: only kill a port owner proven to be a GuvFX order bridge (command line runs
# mt5_signal_bridge) or owned by this identity - an unrelated process that later binds the port is left alone.
try {
  if ($Port -ge 8800 -and $Port -le 8899) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
      $bp = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $conn.OwningProcess) -ErrorAction SilentlyContinue
      $bcmd = ("" + $bp.CommandLine).ToLower()
      $bown = ""
      if ($bp) { try { $bown = (Invoke-CimMethod -InputObject $bp -MethodName GetOwner).User } catch { } }
      if ($bp -and (($bcmd.Contains("mt5_signal_bridge")) -or ($bown -eq $Username))) {
        Stop-Process -Id $conn.OwningProcess -Force -ErrorAction Stop
        $steps.bridge_port = "ok"
      } else { $steps.bridge_port = "absent" }   # not a GuvFX bridge / not ours -> never kill (reused port)
    } else { $steps.bridge_port = "absent" }
  } else { $steps.bridge_port = "absent" }
} catch { $steps.bridge_port = "failed:" + $_.Exception.GetType().Name }

# ---- Step 4: launcher shells whose command line references THIS tenant dir (secondary cleanup) ----
try {
  $needle = ($tenantDir.ToLower() + "\")
  Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
    $cl = "" + $_.CommandLine
    if ($cl -and $cl.ToLower().Contains($needle)) { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
  }
  $steps.bridge_launcher = "ok"
} catch { $steps.bridge_launcher = "failed:" + $_.Exception.GetType().Name }

# ---- Step 5: terminate ONLY this account's terminal64 (owner == user AND exe path under RuntimeRoot\) ----
try {
  $any = $false; $failedKill = $false
  Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue | ForEach-Object {
    $p = $_
    $exe = "" + $p.ExecutablePath
    if (-not $exe) { return }
    $exedir = ([System.IO.Path]::GetDirectoryName($exe)).ToLower() + "\"
    if (-not ($exedir.StartsWith($rtPrefix))) { return }     # exe MUST be under this account's runtime (sep-safe)
    $o = Invoke-CimMethod -InputObject $p -MethodName GetOwner
    if ($o.User -ne $Username) { return }                    # AND owned by this account's identity
    $any = $true
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch { $failedKill = $true }
  }
  if ($failedKill) { $steps.terminal = "failed:kill" } elseif ($any) { $steps.terminal = "ok" } else { $steps.terminal = "absent" }
} catch { $steps.terminal = "failed:" + $_.Exception.GetType().Name }

# ---- Step 6: end this identity's RDP/RemoteApp session (scoped to guvfx_u_<id> only) ----
try {
  $ended = $false
  $lines = (qwinsta 2>$null)
  foreach ($ln in $lines) {
    if ($ln -match ("\b" + [regex]::Escape($Username) + "\s+(\d+)\s")) { logoff $Matches[1] 2>$null; $ended = $true }
  }
  $steps.session = $(if ($ended) { "ok" } else { "absent" })
} catch { $steps.session = "failed:" + $_.Exception.GetType().Name }

# ---- Step 7: remove tenant dir (canonical; leaf == id; refuse reparse points) ----
try {
  $td = [System.IO.Path]::GetFullPath($tenantDir).TrimEnd("\")
  $tdExpect = (Join-Path $TENANTS_BASE ([string]$AccountId)).ToLower()
  if ($td.ToLower() -ne $tdExpect) { $steps.tenant_dir = "failed:path_guard" }
  elseif (Test-Path -LiteralPath $td) {
    if (-not (Test-TreeNoReparse $td)) { $steps.tenant_dir = "failed:reparse_point" }
    else { Remove-Item -LiteralPath $td -Recurse -Force -ErrorAction Stop; $steps.tenant_dir = "ok" }
  } else { $steps.tenant_dir = "absent" }
} catch { $steps.tenant_dir = "failed:" + $_.Exception.GetType().Name }

# ---- Step 8: remove runtime dir (canonical; leaf == id; refuse reparse points) ----
try {
  if ($rtTrim.ToLower() -ne $expectRt.ToLower()) { $steps.runtime_dir = "failed:path_guard" }
  elseif (Test-Path -LiteralPath $rtTrim) {
    if (-not (Test-TreeNoReparse $rtTrim)) { $steps.runtime_dir = "failed:reparse_point" }
    else { Remove-Item -LiteralPath $rtTrim -Recurse -Force -ErrorAction Stop; $steps.runtime_dir = "ok" }
  } else { $steps.runtime_dir = "absent" }
} catch { $steps.runtime_dir = "failed:" + $_.Exception.GetType().Name }

# ---- Step 9: disable (never delete) the Windows identity so it can never launch the old runtime again ----
try {
  $u = Get-LocalUser -Name $Username -ErrorAction SilentlyContinue
  if ($u) {
    if ($u.Enabled) {
      try { Disable-LocalUser -Name $Username -ErrorAction Stop }
      catch { & net user $Username /active:no | Out-Null }
    }
    $steps.identity_disabled = "ok"
  } else { $steps.identity_disabled = "absent" }
} catch { $steps.identity_disabled = "failed:" + $_.Exception.GetType().Name }

# ---- Step 10: verify no account-specific residual remains (same proofs the kill steps use) ----
try {
  $residual = @()
  $term = Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue | Where-Object {
    $exe = "" + $_.ExecutablePath
    if (-not $exe) { return $false }
    $dir = ([System.IO.Path]::GetDirectoryName($exe)).ToLower() + "\"
    if (-not ($dir.StartsWith($rtPrefix))) { return $false }
    $ow = ""
    try { $ow = (Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User } catch { }
    return ($ow -eq $Username)
  }
  if ($term) { $residual += "terminal" }
  if ($Port -ge 8800 -and $Port -le 8899) {
    if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { $residual += "port" }
  }
  if (Test-Path -LiteralPath $rtTrim) { $residual += "runtime_dir" }
  if (Test-Path -LiteralPath $tenantDir) { $residual += "tenant_dir" }
  foreach ($tn in @(("GuvFX_TenantBridge_" + $AccountId), ("GuvFX_TenantBridgeWatchdog_" + $AccountId),
                    ("GuvFX_HostedObserver_" + $AccountId), ("GuvFX_HostedClose_" + $AccountId),
                    ("GuvFX_HostedRelaunch_" + $AccountId))) {
    if (Get-ScheduledTask -TaskName $tn -ErrorAction SilentlyContinue) { $residual += ("task:" + $tn) }
  }
  $steps.verify = $(if ($residual.Count -eq 0) { "ok" } else { "failed:" + ($residual -join "+") })
} catch { $steps.verify = "failed:" + $_.Exception.GetType().Name }

Emit ""

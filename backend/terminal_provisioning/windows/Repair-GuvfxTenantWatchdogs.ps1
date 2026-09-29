# Repair-GuvfxTenantWatchdogs.ps1 - systematic correction of per-tenant bridge watchdog registration (2026-09-29, P1).
#
# WHY: GuvFX_TenantBridgeWatchdog_<id> tasks provisioned before the 2026-09-14 fix were registered against the OLD
# node2_bridge_watchdog.ps1, which HARDCODES port 8789 and IGNORES its -Port/-Task args (so those watchdogs silently
# health-checked account 25's node port and NEVER restarted their own tenant's :88xx bridge - the exact defect that
# let account 35's :8804 stay dead for days). The repo registrar (Activate-GuvfxTenantBridge.ps1) already uses the
# FIXED per-port tenant_bridge_watchdog.ps1 for NEW tenants; this script systematically corrects EXISTING host tasks.
#
# WHAT: for every GuvFX_TenantBridgeWatchdog_<id> task whose action still points at node2_bridge_watchdog.ps1, repoint
# it to tenant_bridge_watchdog.ps1, PRESERVING the task's own -Port/-Task arguments (optionally arming -Port-scoped
# wedge-kill escalation via -WedgeKillThreshold). Idempotent: a task already on the fixed script is reported and left
# unchanged. Only ever touches GuvFX_TenantBridgeWatchdog_* tasks - never the node watchdog, never Customer Zero.
# Read-only except the exact Set-ScheduledTask repoint. ASCII-only (RULE 9); validate with ParseFile before use.
param(
  [string]$FixedScript = 'C:\GuvFX\node2\tenant_bridge_watchdog.ps1',
  [int]$WedgeKillThreshold = 0,
  [switch]$WhatIfOnly
)
$ErrorActionPreference = 'Continue'
if (-not (Test-Path $FixedScript)) { Write-Output ("ABORT: fixed watchdog script not found: " + $FixedScript); exit 1 }

# RULE 9: refuse to repoint tasks at a script that does not itself parse.
$perr = $null; $ptok = $null
[System.Management.Automation.Language.Parser]::ParseFile($FixedScript, [ref]$ptok, [ref]$perr) | Out-Null
if ($perr -and $perr.Count -gt 0) { Write-Output ("ABORT: fixed watchdog script has " + $perr.Count + " parse errors"); exit 1 }

$tasks = Get-ScheduledTask | Where-Object { $_.TaskName -match '^GuvFX_TenantBridgeWatchdog_\d+$' } | Sort-Object TaskName
if (-not $tasks) { Write-Output "no GuvFX_TenantBridgeWatchdog_<id> tasks found"; exit 0 }

$repointed = 0; $already = 0; $skipped = 0
foreach ($t in $tasks) {
  $name = $t.TaskName
  # Read BOTH Execute and Arguments: schtasks /tr may place the powershell path, -File and the -Port/-Task tail in
  # either field depending on Windows/schtasks version, so keying decisions off .Arguments alone can silently miss a
  # broken task (RULE 11). Combining both is robust regardless of the split.
  $args0 = ($t.Actions | ForEach-Object { (([string]$_.Execute) + ' ' + ([string]$_.Arguments)) }) -join ' '
  # Extract the task's own -Port and -Task so we preserve them exactly.
  $port = if ($args0 -match '-Port\s+(\d+)') { [int]$matches[1] } else { $null }
  $tgt  = if ($args0 -match '-Task\s+(\S+)') { $matches[1] } else { $null }
  if ($args0 -match 'tenant_bridge_watchdog\.ps1') {
    $already++; Write-Output ("OK (already fixed): " + $name + " -Port " + $port); continue
  }
  if ($args0 -notmatch 'node2_bridge_watchdog\.ps1') {
    $skipped++; Write-Output ("SKIP (unrecognised action, not repointing): " + $name); continue
  }
  if ((-not $port) -or (-not $tgt) -or ($port -lt 8800) -or ($port -gt 8899)) {
    $skipped++; Write-Output ("SKIP (missing/invalid -Port/-Task; not repointing): " + $name + " args=[" + $args0 + "]"); continue
  }
  $newArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$FixedScript`" -Port $port -Task $tgt"
  if ($WedgeKillThreshold -gt 0) { $newArgs += " -WedgeKillThreshold $WedgeKillThreshold" }
  if ($WhatIfOnly) {
    Write-Output ("WOULD REPOINT: " + $name + " -> " + $newArgs)
  } else {
    $act = New-ScheduledTaskAction -Execute 'powershell' -Argument $newArgs
    Set-ScheduledTask -TaskName $name -Action $act | Out-Null
    $after = ((Get-ScheduledTask -TaskName $name).Actions | ForEach-Object { (([string]$_.Execute) + ' ' + ([string]$_.Arguments)) }) -join ' '
    $ok = ($after -match 'tenant_bridge_watchdog\.ps1')
    if ($ok) { $repointed++; Write-Output ("REPOINTED: " + $name + " -Port " + $port + " -> fixed script" + $(if($WedgeKillThreshold -gt 0){" (escalation armed t=$WedgeKillThreshold)"}else{""})) }
    else { $skipped++; Write-Output ("FAILED to repoint: " + $name) }
  }
}
$total = ($tasks | Measure-Object).Count
Write-Output ("SUMMARY: repointed=" + $repointed + " already_fixed=" + $already + " skipped=" + $skipped + " total=" + $total)
# Positive control (RULE 11): if EVERY task classified as unrecognised (none fixed, none already-correct), the
# action field-read assumption is almost certainly wrong for this host - do NOT trust the clean-looking result.
if (($total -gt 0) -and ($repointed -eq 0) -and ($already -eq 0)) {
  Write-Output ("WARNING: 0 tasks matched a known watchdog script across " + $total + " tenant watchdog task(s) - the")
  Write-Output ("         scheduled-task action field-read may be wrong on this host; VERIFY MANUALLY before trusting.")
  exit 2
}

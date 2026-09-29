# tenant_bridge_watchdog.ps1 - per-TENANT order-bridge liveness watchdog (2026-09-14; escalation 2026-09-29).
#
# Fixes the P0 gap: the reused node2_bridge_watchdog.ps1 has NO param() block and HARDCODES port 8789 +
# start_node2_bridge.bat, so GuvFX_TenantBridgeWatchdog_<id> (invoked '-Port 8802 -Task GuvFX_TenantBridge_<id>')
# silently supervised the node bridge, never the per-tenant bridge -> a dead :8802 was never restarted.
#
# This dedicated watchdog HONORS its params: it health-checks the given -Port and, if unhealthy, restarts the
# per-tenant bridge via its own scheduled task (-Task) using the task's single-instance policy (never a blanket
# python kill, never a duplicate). It NEVER touches the node bridge (:8789) or Customer Zero (:8788). ASCII-only
# (RULE 9). Read-only except restarting exactly the named task; a no-op while the bridge is healthy.
#
# 2026-09-29 (P1): add an OPT-IN, DARK-by-default PID-scoped wedge-kill escalation. A bridge that is alive-but-
# WEDGED (its :$Port is LISTENING but /health times out because a degraded MT5 IPC head-of-line-blocks the single
# HTTP thread) can never be recovered by Start-ScheduledTask alone (IgnoreNew is a no-op while the wedged instance
# still owns the port). When -WedgeKillThreshold > 0, after that many CONSECUTIVE wedged cycles the watchdog kills
# ONLY the process that owns :$Port (verified python, and refused if it also owns a reserved 8788/8789 port), then
# lets Start-ScheduledTask launch exactly one fresh bridge. Default 0 = escalation OFF = byte-identical prior
# behaviour except a small per-port state file. Never a blanket kill; never CZ/node.
param(
  [Parameter(Mandatory=$true)][int]$Port,
  [Parameter(Mandatory=$true)][string]$Task,
  [int]$WedgeKillThreshold = 0
)
$ErrorActionPreference = 'SilentlyContinue'
$LogFile = 'C:\GuvFX\node2\tenant_watchdog.log'
$HealthUrl = "http://localhost:$Port/health"
$StateFile = "C:\GuvFX\node2\tenant_wd_wedge_$Port.txt"

function Write-Log([string]$msg) {
  $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  "$ts [tenant-wd port=$Port task=$Task] $msg" | Out-File -Append -FilePath $LogFile -Encoding ASCII
}
function Get-WedgeCount { try { [int]((Get-Content $StateFile -ErrorAction Stop) | Select-Object -First 1) } catch { 0 } }
function Set-WedgeCount([int]$n) { Set-Content -Path $StateFile -Value ([string]$n) -Encoding ASCII -ErrorAction SilentlyContinue }

# Refuse reserved ports as defence in depth (per-tenant bridges live in 8800-8899).
if ($Port -lt 8800 -or $Port -gt 8899) { Write-Log "refusing reserved/out-of-range port"; exit 0 }

# Only supervise a task that actually exists.
$t = Get-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
if (-not $t) { Write-Log "task not found - nothing to supervise"; exit 0 }

# Read the shared inbound token (documented exception) to authenticate /health.
$AgentToken = ((Select-String -Path 'C:\GuvFX\secrets\bridge.tokens.bat' -Pattern '^\s*set\s+GUVFX_AGENT_TOKEN=').Line -replace '^\s*set\s+GUVFX_AGENT_TOKEN=','').Trim()

function Restart-TenantBridge([string]$why) {
  Write-Log "$why - restarting via scheduled task (single-instance policy prevents a duplicate)."
  # Do NOT kill by port/PID here: the per-tenant bridge is a single foreground process owned by its task.
  # Start-ScheduledTask is a no-op if an instance is already running (IgnoreNew), so this can never fork a
  # second :$Port bridge; when the previous instance has ended it launches exactly one fresh bridge.
  Start-ScheduledTask -TaskName $Task -ErrorAction SilentlyContinue
}

# PID-scoped kill of the WEDGED bridge that owns :$Port. Returns $true only if it killed exactly that process.
# Fail-closed: refuse if the owner also holds a reserved port (never touch CZ :8788 / node :8789), or is not a
# python bridge. This is the ONLY place the watchdog ever kills a process, and only when escalation is armed.
function Stop-WedgedBridge {
  $c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $c) { return $false }
  $procId = [int]$c.OwningProcess
  $owned = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { [int]$_.OwningProcess -eq $procId } | ForEach-Object { [int]$_.LocalPort })
  if (($owned -contains 8788) -or ($owned -contains 8789)) { Write-Log "REFUSE wedge-kill: pid $procId also owns a reserved port"; return $false }
  $p = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $procId) -ErrorAction SilentlyContinue
  if ((-not $p) -or ($p.Name -notmatch 'python')) { Write-Log "REFUSE wedge-kill: pid $procId is not a python bridge"; return $false }
  Write-Log ("escalation: PID-scoped kill of wedged bridge pid=" + $procId + " (owns only :" + ($owned -join ',') + ")")
  Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  return $true
}

$listen = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $listen) { Set-WedgeCount 0; Restart-TenantBridge "no process listening on $Port"; exit 0 }

try {
  $headers = @{ 'X-GuvFX-Agent-Token' = $AgentToken }
  $resp = Invoke-WebRequest -Uri $HealthUrl -Headers $headers -TimeoutSec 10 -UseBasicParsing
  $body = $resp.Content | ConvertFrom-Json
  if ($body.ok -eq $true) { Set-WedgeCount 0; exit 0 }        # healthy: silent success, reset wedge counter
  # Listening but health ok!=true => alive-but-wedged.
  $n = (Get-WedgeCount) + 1
  if (($WedgeKillThreshold -gt 0) -and ($n -ge $WedgeKillThreshold)) {
    if (Stop-WedgedBridge) { Set-WedgeCount 0 } else { Set-WedgeCount $n }
    Restart-TenantBridge "health ok=false; wedge escalation (n=$n>=$WedgeKillThreshold)"
  } else {
    Set-WedgeCount $n
    Restart-TenantBridge "health returned ok=false (wedge n=$n; escalation off or below threshold)"
  }
} catch {
  # Timeout/connection error while :$Port is LISTENING is the classic wedge symptom.
  $n = (Get-WedgeCount) + 1
  if (($WedgeKillThreshold -gt 0) -and ($n -ge $WedgeKillThreshold)) {
    if (Stop-WedgedBridge) { Set-WedgeCount 0 } else { Set-WedgeCount $n }
    Restart-TenantBridge "health check failed (wedge escalation n=$n>=$WedgeKillThreshold): $($_.Exception.Message)"
  } else {
    Set-WedgeCount $n
    Restart-TenantBridge "health check failed (wedge n=$n; escalation off or below threshold): $($_.Exception.Message)"
  }
}
exit 0

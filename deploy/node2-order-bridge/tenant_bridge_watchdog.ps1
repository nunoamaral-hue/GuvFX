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
  # PID-reuse TOCTOU guard: re-read the :$Port owner immediately before the kill and confirm it is still $procId,
  # so a PID recycled between the checks above and the kill can never be targeted.
  $again = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
  if ((-not $again) -or ([int]$again.OwningProcess -ne $procId)) { Write-Log "ABORT wedge-kill: :$Port owner changed before kill"; return $false }
  Write-Log ("escalation: PID-scoped kill of wedged bridge pid=" + $procId + " (owns only :" + ($owned -join ',') + ")")
  Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
  return $true
}

# A genuine wedge (transport failure while :$Port is LISTENING): increment the consecutive-wedge counter and,
# once ARMED and at/above threshold, PID-kill the wedged owner before relaunching. Off by default (threshold 0).
function Invoke-WedgeHandling([string]$why) {
  $n = (Get-WedgeCount) + 1
  if (($WedgeKillThreshold -gt 0) -and ($n -ge $WedgeKillThreshold)) {
    if (Stop-WedgedBridge) { Set-WedgeCount 0 } else { Set-WedgeCount $n }
    Restart-TenantBridge "$why; wedge escalation (n=$n>=$WedgeKillThreshold)"
  } else {
    Set-WedgeCount $n
    Restart-TenantBridge "$why (wedge n=$n; escalation off or below threshold)"
  }
}

$listen = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $listen) { Set-WedgeCount 0; Restart-TenantBridge "no process listening on $Port"; exit 0 }

try {
  $headers = @{ 'X-GuvFX-Agent-Token' = $AgentToken }
  $resp = Invoke-WebRequest -Uri $HealthUrl -Headers $headers -TimeoutSec 10 -UseBasicParsing
  $body = $resp.Content | ConvertFrom-Json
  if ($body.ok -eq $true) { Set-WedgeCount 0; exit 0 }        # healthy: silent success, reset wedge counter
  # The server RESPONDED with ok!=true (responsive-but-unhealthy) - this is NOT a wedge (a wedged bridge cannot
  # answer /health at all). Restart via the task (no-op if running) and reset the wedge counter: never PID-kill a
  # bridge that is answering. Only a no-response transport failure (below) is treated as a wedge.
  Set-WedgeCount 0
  Restart-TenantBridge "health returned ok=false (server responsive; not a wedge - no escalation)"
} catch {
  # Distinguish a genuine TRANSPORT failure (timeout / connection refused = a real wedge) from an HTTP error
  # STATUS (401/403/5xx: the server RESPONDED, so it is NOT wedged). PS 5.1 Invoke-WebRequest throws on any 4xx/5xx,
  # so a wrong/empty agent token (401) or a transient 5xx must NEVER be counted as a wedge or trigger the PID-kill;
  # only a no-response failure while :$Port is LISTENING is a wedge.
  $ex = $_.Exception
  $responded = $false
  try { if ($ex -and ($ex.PSObject.Properties.Name -contains 'Response') -and ($ex.Response -ne $null)) { $responded = $true } } catch { $responded = $false }
  if ($responded) {
    Set-WedgeCount 0
    Restart-TenantBridge "health HTTP error status (server responded; not a wedge - no escalation): $($ex.Message)"
  } else {
    Invoke-WedgeHandling "health check transport failure (timeout/refused) while :$Port listening: $($ex.Message)"
  }
}
exit 0

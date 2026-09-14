# tenant_bridge_watchdog.ps1 - per-TENANT order-bridge liveness watchdog (2026-09-14).
#
# Fixes the P0 gap: the reused node2_bridge_watchdog.ps1 has NO param() block and HARDCODES port 8789 +
# start_node2_bridge.bat, so GuvFX_TenantBridgeWatchdog_<id> (invoked '-Port 8802 -Task GuvFX_TenantBridge_<id>')
# silently supervised the node bridge, never the per-tenant bridge -> a dead :8802 was never restarted.
#
# This dedicated watchdog HONORS its params: it health-checks the given -Port and, if unhealthy, restarts the
# per-tenant bridge via its own scheduled task (-Task) using the task's single-instance policy (never a blanket
# python kill, never a duplicate). It NEVER touches the node bridge (:8789) or Customer Zero (:8788). ASCII-only
# (RULE 9). Read-only except restarting exactly the named task; a no-op while the bridge is healthy.
param(
  [Parameter(Mandatory=$true)][int]$Port,
  [Parameter(Mandatory=$true)][string]$Task
)
$ErrorActionPreference = 'SilentlyContinue'
$LogFile = 'C:\GuvFX\node2\tenant_watchdog.log'
$HealthUrl = "http://localhost:$Port/health"

function Write-Log([string]$msg) {
  $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
  "$ts [tenant-wd port=$Port task=$Task] $msg" | Out-File -Append -FilePath $LogFile -Encoding ASCII
}

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

$listen = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $listen) { Restart-TenantBridge "no process listening on $Port"; exit 0 }

try {
  $headers = @{ 'X-GuvFX-Agent-Token' = $AgentToken }
  $resp = Invoke-WebRequest -Uri $HealthUrl -Headers $headers -TimeoutSec 10 -UseBasicParsing
  $body = $resp.Content | ConvertFrom-Json
  if ($body.ok -eq $true) { exit 0 }        # healthy: silent success
  Restart-TenantBridge 'health returned ok=false'
} catch {
  Restart-TenantBridge "health check failed: $($_.Exception.Message)"
}
exit 0

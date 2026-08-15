# ============================================================================
# GuvFX Node 2 (Closed-Beta) bridge watchdog. ASCII only (RULE 9).
# Runs every 60s via a dedicated scheduled task. Health-checks :8789 ONLY and
# restarts ONLY the node2 bridge.
#
# CUSTOMER-ZERO SAFETY: this watchdog is strictly PORT-SPECIFIC. It NEVER issues
# a blanket 'Get-Process python | Stop-Process' (that would kill Customer Zero's
# :8788 bridge). It only ever stops the single process that owns TCP :8789, and
# only when node2's own /health fails. Customer Zero's bridge and watchdog are
# never referenced.
# ============================================================================

$ErrorActionPreference = 'SilentlyContinue'
$LogFile   = 'C:\GuvFX\node2\watchdog.log'
$Port      = 8789
$HealthUrl = "http://localhost:$Port/health"
$StartBat  = 'C:\GuvFX\node2\start_node2_bridge.bat'
$EnvBat    = 'C:\GuvFX\node2\node2_bridge.env.bat'
$MaxLog    = 500

function Write-Log([string]$msg) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    "$ts $msg" | Out-File -Append -FilePath $LogFile -Encoding ASCII
}

# Only supervise once the bridge has been activated (env present). Before
# activation this task is a no-op, so staging the watchdog early is harmless.
if (-not (Test-Path $EnvBat)) { exit 0 }

# Read the shared inbound token (documented exception) to authenticate /health.
$AgentToken = ((Select-String -Path 'C:\GuvFX\secrets\bridge.tokens.bat' -Pattern '^\s*set\s+GUVFX_AGENT_TOKEN=').Line -replace '^\s*set\s+GUVFX_AGENT_TOKEN=','').Trim()

function Get-Node2Pid {
    # The single process that owns TCP :8789, or $null. Never matches CZ's :8788.
    $c = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return $null
}

function Restart-Node2 {
    param([string]$why)
    Write-Log "[node2-wd] $why - restarting node2 bridge (port-specific)."
    $node2Pid = Get-Node2Pid
    if ($node2Pid) {
        # Stop ONLY the process bound to :8789. Never a blanket python kill.
        Stop-Process -Id $node2Pid -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }
    Start-Process -FilePath $StartBat -WindowStyle Minimized
}

$listenPid = Get-Node2Pid
if (-not $listenPid) {
    Restart-Node2 'no process listening on 8789'
    exit 0
}

try {
    $headers = @{ 'X-GuvFX-Agent-Token' = $AgentToken }
    $resp = Invoke-WebRequest -Uri $HealthUrl -Headers $headers -TimeoutSec 10 -UseBasicParsing
    $body = $resp.Content | ConvertFrom-Json
    if ($body.ok -eq $true) { exit 0 }   # healthy: silent success
    Restart-Node2 'health returned ok=false'
} catch {
    Restart-Node2 "health check failed: $($_.Exception.Message)"
}

# Trim the log.
if (Test-Path $LogFile) {
    $lines = Get-Content $LogFile
    if ($lines.Count -gt $MaxLog) { $lines | Select-Object -Last $MaxLog | Set-Content $LogFile -Encoding ASCII }
}

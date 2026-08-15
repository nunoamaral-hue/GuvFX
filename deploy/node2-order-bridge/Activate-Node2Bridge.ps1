[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [int]    $AccountId,
    [Parameter(Mandatory = $true)] [string] $TerminalPath,
    [Parameter(Mandatory = $true)] [string] $ExpectedLogin,
    [Parameter(Mandatory = $true)] [string] $ExpectedServer
)
# ============================================================================
# GuvFX Node 2 (Closed-Beta) bridge ACTIVATION. ASCII only (RULE 9).
# Run ONCE at materialise time, when the beta user's workspace has materialised
# and his demo terminal is running + broker-logged-in. Writes the account
# config, registers the node2 bridge + watchdog tasks (run-as CLONED from
# Customer Zero's GuvFX_SignalBridge so cross-session MT5 attach behaves
# identically), starts the bridge, and verifies /health.
#
# CUSTOMER-ZERO SAFETY: refuses AccountId 1; never modifies CZ's tasks/bridge.
# ============================================================================

$ErrorActionPreference = 'Stop'
$Node2Dir = 'C:\GuvFX\node2'

if ($AccountId -eq 1) {
    Write-Error 'REFUSED: account id 1 is Customer Zero. The node2 beta bridge must never be activated for CZ.'
    exit 2
}

# 1) Write the account-specific env from the template (ASCII). The four placeholder
#    values carry no regex/$ specials (ids, Windows paths, logins), so a plain
#    -replace is exact; the PowerShell $env: provider is untouched (local var $envOut).
$tpl = Get-Content -Raw -Path (Join-Path $Node2Dir 'node2_bridge.env.template.bat')
$envOut = $tpl `
    -replace '__ACCOUNT_ID__',     ([string]$AccountId) `
    -replace '__TERMINAL_PATH__',  $TerminalPath `
    -replace '__EXPECTED_LOGIN__',  $ExpectedLogin `
    -replace '__EXPECTED_SERVER__', $ExpectedServer
Set-Content -Path (Join-Path $Node2Dir 'node2_bridge.env.bat') -Value $envOut -Encoding ASCII
Write-Host "[activate] wrote node2_bridge.env.bat for account $AccountId"

# 2) Register the bridge task by CLONING Customer Zero's run-as principal.
$czXmlRaw = & schtasks /query /tn '\GuvFX_SignalBridge' /xml ONE 2>$null
if (-not $czXmlRaw) { Write-Error 'Could not read CZ GuvFX_SignalBridge task XML to clone run-as.'; exit 3 }
$czXml = ($czXmlRaw -join "`n")
# Re-point the command at the node2 launcher; preserve principal + triggers.
$node2Xml = $czXml -replace [regex]::Escape('C:\GuvFX\start_signal_bridge.bat'), 'C:\GuvFX\node2\start_node2_bridge.bat'
$xmlPath = Join-Path $Node2Dir 'GuvFX_Node2Bridge.xml'
Set-Content -Path $xmlPath -Value $node2Xml -Encoding Unicode
& schtasks /create /tn 'GuvFX_Node2Bridge' /xml $xmlPath /f | Out-Null
Write-Host '[activate] registered task GuvFX_Node2Bridge (run-as cloned from CZ)'

# 3) Register the port-specific watchdog (SYSTEM; safe, no interactive attach needed).
& schtasks /create /tn 'GuvFX_Node2BridgeWatchdog' `
    /tr 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\GuvFX\node2\node2_bridge_watchdog.ps1' `
    /sc MINUTE /mo 1 /ru SYSTEM /rl HIGHEST /f | Out-Null
Write-Host '[activate] registered task GuvFX_Node2BridgeWatchdog (every 1 min)'

# 4) Start the bridge and verify /health.
& schtasks /run /tn 'GuvFX_Node2Bridge' | Out-Null
$token = ((Select-String -Path 'C:\GuvFX\secrets\bridge.tokens.bat' -Pattern '^\s*set\s+GUVFX_AGENT_TOKEN=').Line -replace '^\s*set\s+GUVFX_AGENT_TOKEN=','').Trim()
$ok = $false
for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri 'http://localhost:8789/health' -Headers @{ 'X-GuvFX-Agent-Token' = $token } -TimeoutSec 5 -UseBasicParsing
        if (($r.Content | ConvertFrom-Json).ok -eq $true) { $ok = $true; break }
    } catch { }
}
if ($ok) {
    Write-Host '[activate] OK - node2 bridge healthy on :8789'
    exit 0
} else {
    Write-Warning '[activate] node2 bridge did NOT report healthy on :8789 within timeout - inspect C:\GuvFX\node2\watchdog.log and the bridge console.'
    exit 4
}

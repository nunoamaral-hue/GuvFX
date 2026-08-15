# ============================================================================
# GuvFX Node 2 (Closed-Beta) bridge DEACTIVATION / teardown. ASCII only (RULE 9).
# Fully reverses activation: stops + unregisters the node2 tasks and stops the
# process bound to :8789. Customer Zero's :8788 bridge, its tasks and watchdog
# are never referenced. After running this, clear
# TerminalNode(pk=2).order_bridge_base_url to revert routing.
# ============================================================================

$ErrorActionPreference = 'SilentlyContinue'

# Stop + remove the scheduled tasks (node2-specific names only).
foreach ($t in @('GuvFX_Node2Bridge', 'GuvFX_Node2BridgeWatchdog')) {
    & schtasks /end    /tn $t 2>$null | Out-Null
    & schtasks /delete /tn $t /f 2>$null | Out-Null
    Write-Host "[deactivate] removed task $t (if present)"
}

# Stop ONLY the process bound to :8789 (never a blanket python kill).
$c = Get-NetTCPConnection -State Listen -LocalPort 8789 -ErrorAction SilentlyContinue | Select-Object -First 1
if ($c) {
    Stop-Process -Id ([int]$c.OwningProcess) -Force -ErrorAction SilentlyContinue
    Write-Host '[deactivate] stopped process on :8789'
} else {
    Write-Host '[deactivate] nothing listening on :8789'
}

# Remove the account-specific env so a stray restart cannot re-launch against a
# stale identity (the launcher refuses to start without it).
Remove-Item -Path 'C:\GuvFX\node2\node2_bridge.env.bat' -Force -ErrorAction SilentlyContinue
Write-Host '[deactivate] removed node2_bridge.env.bat'
Write-Host '[deactivate] DONE. Remember to clear TerminalNode(pk=2).order_bridge_base_url to revert routing.'

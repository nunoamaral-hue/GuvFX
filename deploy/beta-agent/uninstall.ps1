# CVM-Inc-3 B2/B3P-1 — teardown: remove the beta agent service, its firewall rule, its ACL grants and any
# launch tasks. RETAINS runtime + tombstone data (audit). Leaves Nuno's estate untouched.
# DARK ARTEFACT: RUN ONLY in B3, on the host, as Administrator. Dry-run by default; pass -Apply to remove.
#
# Verification (rollback/uninstall safety): the prior stub only stopped+deleted the service. This removes the
# firewall rule, the service-account ACL grants and any launch task(s) too, so no orphaned rule/grant/task is
# left behind. (B3P-2 adds: drain any RUNNING beta runtimes first, and remove the per-<uuid> runtime identities;
# in B3P-1 no runtime can have been launched yet.)
param(
  [string]$ServiceName = "GuvFXBetaAgent",
  [string]$RuleName    = "GuvFX-Beta-Agent-In",
  [string]$RunAsUser   = "NT SERVICE\GuvFXBetaAgent",
  [string]$AgentDir    = "C:\GuvFX\beta\agent",
  [string]$StateDir    = "C:\GuvFX\beta\agent-state",
  [string]$BetaAccounts= "C:\GuvFX\beta\accounts",
  [string]$BetaTombstones = "C:\GuvFX\beta\tombstones",
  [string]$LaunchTaskPrefix = "GuvFXBetaRuntime-",
  [switch]$Apply
)
$ErrorActionPreference = "Stop"
function DoIt($desc, [scriptblock]$block) {
  if ($Apply) { Write-Host "APPLY: $desc"; & $block } else { Write-Host "PLAN:  $desc" }
}

# 1. Stop + delete the service (pywin32 remove cleans the SCM registration).
DoIt "stop + remove service '$ServiceName'" {
  sc.exe stop $ServiceName 2>$null | Out-Null
  if (Test-Path (Join-Path $AgentDir "service.py")) {
    & "C:\GuvFX\python311.exe" (Join-Path $AgentDir "service.py") "remove" 2>$null
  }
  sc.exe delete $ServiceName 2>$null | Out-Null
}

# 2. Remove the firewall rule (leaves :8788/:8787 rules untouched).
DoIt "remove firewall rule '$RuleName'" {
  if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    Remove-NetFirewallRule -DisplayName $RuleName
  }
}

# 3. Remove any per-runtime launch tasks registered under the fixed prefix (none in B3P-1).
DoIt "unregister launch tasks '$LaunchTaskPrefix*'" {
  Get-ScheduledTask -TaskName "$LaunchTaskPrefix*" -ErrorAction SilentlyContinue |
    Unregister-ScheduledTask -Confirm:$false
}

# 4. Remove the service-account ACL grants from the beta tree (no standing principal on retained data).
foreach ($d in @($AgentDir, $StateDir, $BetaAccounts, $BetaTombstones)) {
  DoIt "revoke '$RunAsUser' ACL grant on $d" {
    if (Test-Path $d) { icacls $d /remove:g "$RunAsUser" | Out-Null }
  }
}

Write-Host ""
Write-Host "RETAINED (never deleted): runtime dirs under $BetaAccounts and tombstones under $BetaTombstones."
Write-Host "UNTOUCHED: Nuno's terminal (Session 3), bridge (:8788), :8787, autologon, startup tasks."
if (-not $Apply) { Write-Host "PLAN complete. Re-run with -Apply on the host to perform the teardown." }

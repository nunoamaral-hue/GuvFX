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
  [string]$SlotsRoot   = "C:\GuvFX\beta\slots",
  [string]$BetaTombstones = "C:\GuvFX\beta\tombstones",
  [string]$GoldenDir   = "C:\GuvFX\beta\golden",
  [string]$LaunchTaskPrefix = "GuvFXBetaRuntime-",
  [string]$StopTaskPrefix   = "GuvFXBetaRuntimeStop-",
  [string]$IdentityPrefix   = "guvfx_b_slot",
  [int]$PoolSize            = 4,
  # Identities are DISABLED by default, not deleted: deletion orphans anything they own and destroys the
  # ability to attribute retained tombstone evidence. -RemoveIdentities is an explicit operator choice.
  [switch]$RemoveIdentities,
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

# 3. Remove BOTH task families. The B2 version removed only the launch prefix, which left the terminate
#    tasks — and their stored credentials — behind (install-only review F4). Unregistering a task removes
#    its credential with it.
foreach ($prefix in @($LaunchTaskPrefix, $StopTaskPrefix)) {
  DoIt "unregister tasks '$prefix*'" {
    Get-ScheduledTask -TaskName "$prefix*" -ErrorAction SilentlyContinue |
      Unregister-ScheduledTask -Confirm:$false
  }
}

# 4. Remove the service-account ACL grants (no standing principal on retained data).
foreach ($d in @($AgentDir, $StateDir, $BetaTombstones)) {
  DoIt "revoke '$RunAsUser' ACL grant on $d" {
    if (Test-Path $d) { icacls $d /remove:g "$RunAsUser" | Out-Null }
  }
}

# 5. Remove each slot identity's grants, revoke SeBatchLogonRight, and disable (not delete) the account.
for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  if (-not (Get-LocalUser -Name $user -ErrorAction SilentlyContinue)) { continue }
  foreach ($d in @((Join-Path $SlotsRoot "$n"), $GoldenDir)) {
    DoIt "revoke '$user' ACL grant on $d" {
      if (Test-Path $d) { icacls $d /remove:g "$user" | Out-Null }
    }
  }
  DoIt "disable identity '$user' (NOT deleted unless -RemoveIdentities)" {
    Disable-LocalUser -Name $user
  }
  if ($RemoveIdentities) {
    DoIt "DELETE identity '$user' (explicitly requested)" { Remove-LocalUser -Name $user }
  }
}
DoIt "revoke SeBatchLogonRight from the slot identities" {
  $tmp = New-TemporaryFile
  secedit /export /areas USER_RIGHTS /cfg "$tmp" | Out-Null
  $cfg  = Get-Content "$tmp"
  $line = ($cfg | Where-Object { $_ -match "^SeBatchLogonRight" })
  if ($line) {
    $sids = @()
    for ($n = 1; $n -le $PoolSize; $n++) {
      $u = Get-LocalUser -Name "$IdentityPrefix$n" -ErrorAction SilentlyContinue
      if ($u) { $sids += "*" + $u.SID.Value }
    }
    $kept = (($line -split "=", 2)[1].Trim() -split "," | Where-Object { $_ -and ($sids -notcontains $_) }) -join ","
    Set-Content -Path "$tmp" -Value ($cfg -replace "^SeBatchLogonRight.*", "SeBatchLogonRight = $kept")
    secedit /configure /db "$env:windir\security\local.sdb" /cfg "$tmp" /areas USER_RIGHTS | Out-Null
  }
  Remove-Item "$tmp" -Force
}

Write-Host ""
Write-Host "RETAINED (never deleted): slot dirs under $SlotsRoot, tombstones under $BetaTombstones,"
Write-Host "                          and $StateDir (nonce/idempotency/slot/audit stores = the evidence chain)."
Write-Host "UNTOUCHED: Nuno's terminal (Session 3), bridge (:8788), :8787, autologon, startup tasks."
if (-not $Apply) { Write-Host "PLAN complete. Re-run with -Apply on the host to perform the teardown." }

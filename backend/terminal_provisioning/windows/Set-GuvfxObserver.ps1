<#
  Stream 5 - prepare the read-only, session-bound hosted observer for one identity (idempotent).

  Reuses the already-certified Hosted Observation architecture (attach -> read -> exit); it does NOT redesign it
  and creates NO permanent execution bridge. For guvfx_u_<id> it ensures:
    * the reviewed observer tooling exists in the AppLocker-allowed location (default C:\GuvFX\observer, admin-owned),
    * guvfx_u_<id> holds ReadAndExecute on that tooling (never write),
    * an ON-DEMAND scheduled task 'GuvFX_HostedObserver_<id>' registered to run AS guvfx_u_<id> (InteractiveToken,
      LeastPrivilege), created only if absent (never duplicated).

  The observer itself remains attach/read/exit and is launched on demand; this primitive only prepares it.
  ASCII-only (RULE 9). Emits a single compact JSON object.

  Usage:
    powershell -NoProfile -File Set-GuvfxObserver.ps1 -Mode Ensure -Username guvfx_u_14 -RuntimeRoot 'C:\GuvFX\accounts\14'
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("Ensure","Verify","Remove")][string]$Mode,
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [string]$ObserverDir = "C:\GuvFX\observer"
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$result = [ordered]@{ username=$Username; task=""; observer_dir=$ObserverDir; acl_ok=$false; task_present=$false; ok=$false; reason="" }

function Fail([string]$why) { $result.ok=$false; $result.reason=$why; $result | ConvertTo-Json -Compress; exit 1 }

try {
  if ($Username -notmatch "^guvfx_u_[1-9][0-9]*$") { Fail "refusing: not a hosted identity" }
  $full = [System.IO.Path]::GetFullPath($RuntimeRoot)
  if ($full -like "*..*") { Fail "refusing: path traversal" }
  if (-not ($full.ToLower().StartsWith(($ACCOUNTS_BASE.ToLower() + "\")))) { Fail "refusing: outside accounts base" }
  $taskName = "GuvFX_HostedObserver_" + ($Username -replace '^guvfx_u_','')
  $result.task = $taskName

  if ($Mode -eq "Remove") {
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
      Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    $result.task_present = [bool](Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
    $result.ok = (-not $result.task_present); if (-not $result.ok) { Fail "remove did not clear the task" }
    $result | ConvertTo-Json -Compress; return
  }

  if (-not (Test-Path -LiteralPath $ObserverDir)) { Fail "observer tooling dir absent - stage the reviewed observer first" }

  if ($Mode -eq "Ensure") {
    # Grant ONLY ReadAndExecute to the identity on the observer tooling (never write). Additive icacls; the dir
    # is admin-owned and outside every slot tree, so the identity cannot modify the observer.
    & icacls $ObserverDir /grant ("{0}:(OI)(CI)RX" -f $Username) | Out-Null
    if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
      $obsPy = Join-Path $ObserverDir "run_observer.py"
      $action = New-ScheduledTaskAction -Execute "py" -Argument ('"{0}" --account {1}' -f $obsPy, ($Username -replace '^guvfx_u_',''))
      $principal = New-ScheduledTaskPrincipal -UserId $Username -LogonType Interactive -RunLevel Limited
      $settings = New-ScheduledTaskSettings -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
      Register-ScheduledTask -TaskName $taskName -Action $action -Principal $principal -Settings $settings | Out-Null
    }
  }

  # Verify: identity has RX on the observer dir + the on-demand task exists (exactly one).
  $rules = (Get-Acl -Path $ObserverDir).Access | Where-Object { $_.IdentityReference -like "*$Username" -and $_.FileSystemRights -match "ReadAndExecute|Read" }
  $result.acl_ok = [bool]$rules
  $result.task_present = [bool](Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)
  if (-not ($result.acl_ok -and $result.task_present)) { Fail "observer preparation incomplete" }
  $result.ok = $true
  $result | ConvertTo-Json -Compress
}
catch { $result.ok=$false; $result.reason="error"; $result | ConvertTo-Json -Compress; exit 1 }

<#
  Relaunch-GuvfxTerminal.ps1  (AJ#6.3 - post-login AutoTrading capability recovery)

  Gracefully closes and relaunches ONLY this tenant's own portable MT5 (terminal64.exe), inside the tenant's
  interactive session, as the tenant user. A session-0 service cannot WM_CLOSE / launch a terminal that lives in
  another session, so the close and relaunch are run through per-account scheduled tasks whose principal is the
  tenant user (Interactive, Limited) - exactly the mechanism the Customer Zero certification used, generalised
  per account.

  This is CAPABILITY RECOVERY ONLY. It NEVER logs in, changes the broker account, arms a strategy, or places an
  order. It relaunches the SAME portable runtime so the broker profile/login persists (persistent-workspace
  behaviour). Customer Zero (account 1 / guvfx_u_1) is refused here as defence in depth (the signed dispatcher
  already refuses reserved identities before this script is ever selected).

  All identity/path values are server-derived and passed as named args; nothing is read from a caller string.
  ASCII-only (RULE 9 corollary) so it parses identically under any encoding.
#>
param(
  [Parameter(Mandatory = $true)][string]$Username,
  [Parameter(Mandatory = $true)][string]$TerminalRoot,
  [Parameter(Mandatory = $true)][int]$AccountId
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$RESERVED_ACCOUNT_IDS = @(1)          # Customer Zero - NEVER relaunched via this primitive
$CLOSE_TIMEOUT_S = 30
$RELAUNCH_TIMEOUT_S = 45

$result = [ordered]@{
  account_id = $AccountId; username = $Username; running_before = $false;
  closed = $false; relaunched = $false; ok = $false; reason = ""
}
function Emit() { $result | ConvertTo-Json -Compress; }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

function Count-TenantTerminal([string]$user) {
  $n = 0
  foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue)) {
    try { $owner = (Invoke-CimMethod -InputObject $p -MethodName GetOwner -ErrorAction SilentlyContinue).User }
    catch { $owner = $null }
    if ($owner -eq $user) { $n = $n + 1 }
  }
  return $n
}

try {
  # ---- Confinement (defence in depth; the dispatcher already derived every value + refused reserved ids) ----
  if ($RESERVED_ACCOUNT_IDS -contains $AccountId) { Fail "refusing_reserved_identity" }
  if ($AccountId -le 0) { Fail "refusing_account_id_out_of_range" }
  if ($Username -ne ("guvfx_u_" + $AccountId)) { Fail "refusing_username_mismatch" }
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "refusing_path_traversal" }
  $expected = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $ACCOUNTS_BASE ([string]$AccountId)) "terminal"))
  if ($full.ToLower() -ne $expected.ToLower()) { Fail "refusing_terminal_root_mismatch" }
  if (-not (Test-Path -LiteralPath $full)) { Fail "terminal_root_missing" }
  $exe = Join-Path $full "terminal64.exe"
  if (-not (Test-Path -LiteralPath $exe)) { Fail "terminal64_missing" }

  # ---- Only recover a terminal that is actually running (never spuriously launch a dormant runtime) ----
  $before = Count-TenantTerminal $Username
  $result.running_before = ($before -ge 1)
  if ($before -lt 1) { Fail "no_tenant_terminal_running" }

  # ---- Per-account close/relaunch tasks, running IN the tenant session as the tenant user (idempotent) ----
  $closeTask = "GuvFX_HostedClose_" + $AccountId
  $relTask = "GuvFX_HostedRelaunch_" + $AccountId
  $principal = New-ScheduledTaskPrincipal -UserId $Username -LogonType Interactive -RunLevel Limited
  # Graceful WM_CLOSE (no /F): a non-admin taskkill can only close its OWN terminal, so this never touches
  # another tenant's or Customer Zero's terminal even though it matches by image name.
  $closeAction = New-ScheduledTaskAction -Execute "C:\Windows\System32\taskkill.exe" -Argument "/IM terminal64.exe"
  Register-ScheduledTask -TaskName $closeTask -Action $closeAction -Principal $principal -Force | Out-Null
  $relAction = New-ScheduledTaskAction -Execute $exe -Argument "/portable"
  Register-ScheduledTask -TaskName $relTask -Action $relAction -Principal $principal -Force | Out-Null

  # ---- 1) graceful close, bounded wait for the tenant terminal to exit ----
  Start-ScheduledTask -TaskName $closeTask
  $deadline = (Get-Date).AddSeconds($CLOSE_TIMEOUT_S)
  while ((Get-Date) -lt $deadline) {
    if ((Count-TenantTerminal $Username) -eq 0) { $result.closed = $true; break }
    Start-Sleep -Milliseconds 500
  }
  if (-not $result.closed) { Fail "close_timeout" }

  # ---- 2) relaunch, bounded wait for the tenant terminal to reappear ----
  Start-ScheduledTask -TaskName $relTask
  $deadline = (Get-Date).AddSeconds($RELAUNCH_TIMEOUT_S)
  while ((Get-Date) -lt $deadline) {
    if ((Count-TenantTerminal $Username) -ge 1) { $result.relaunched = $true; break }
    Start-Sleep -Milliseconds 500
  }
  if (-not $result.relaunched) { Fail "relaunch_timeout" }

  $result.ok = $true
  $result.reason = "ok"
  Emit
  exit 0
}
catch {
  Fail "relaunch_exception"
}

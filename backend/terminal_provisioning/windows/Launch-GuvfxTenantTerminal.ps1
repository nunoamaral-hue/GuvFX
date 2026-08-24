<#
  Launch-GuvfxTenantTerminal.ps1  (P0 tenant-scoped single-instance MT5 launch guard)

  The customer RemoteApp is the ONLY normal launch authority for a tenant's portable MT5 (the bridge + observer
  are attach-only, enforced by MT5_GUARDED_ATTACH). But MT5 /portable does NOT enforce single-instance, so a
  browser refresh / reconnect / second tab re-runs the RemoteApp start-program and produces a SECOND
  terminal64.exe against the same tenant data directory -> the observer fails closed (duplicate_terminal) and
  onboarding stalls. This wrapper makes the launch idempotent: RemoteApp is repointed to run THIS script (in the
  tenant session), and it guarantees at most one tenant terminal regardless of how many times it is invoked.

  Invariant (tenant-scoped; exact executable-path scoped; server-derived identity):
    0 existing accounts\<id>\terminal\terminal64.exe  -> launch EXACTLY one (/portable), then hold the session.
    1 existing                                         -> do NOT launch; reuse (wait on the existing one).
    >=2 existing                                       -> REFUSE, fail closed (duplicate_terminal). Never choose.

  It NEVER: launches a second instance, kills/replaces an existing terminal, logs in, touches another tenant, or
  uses a machine-global (cross-tenant) mutex. Customer Zero + the account-18 control are refused. The governed
  AJ#6.4 relaunch primitive remains the ONLY explicit close+relaunch recovery authority. All identity/path values
  are server-derived. ASCII-only (RULE 9). The per-tenant serialisation mutex is Global\ so it is visible across
  RDP sessions (a refresh may land in a different session id transiently) but its NAME is per-account, so it can
  never gate or affect another tenant.

  Modes: emits a single "LAUNCH-VERDICT {json}" line, then -- unless GUVFX_LAUNCH_NO_HOLD=1 (throwaway
  validation) -- waits on the tenant terminal so the RemoteApp session lives exactly as long as the terminal.
#>
param(
  [Parameter(Mandatory = $true)][string]$Username,
  [Parameter(Mandatory = $true)][string]$TerminalRoot,
  [Parameter(Mandatory = $true)][int]$AccountId
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$RESERVED_ACCOUNT_IDS = @(1, 18)     # SACRED: Customer Zero (1) + the account-18 control. Never guarded-launch.
$MUTEX_WAIT_MS = 30000               # serialise concurrent launches (refresh/second-tab race)

$result = [ordered]@{
  account_id = $AccountId; username = $Username; mode = ""; count_before = -1; count_after = -1;
  launched_pid = 0; ok = $false; reason = ""
}
function Emit() { Write-Output ("LAUNCH-VERDICT " + ($result | ConvertTo-Json -Compress)) }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

# PIDs of terminal64.exe whose ExecutablePath is EXACTLY the tenant's canonical exe (never image-name alone).
function Get-TenantTerminalPids([string]$expectedExe) {
  $out = New-Object System.Collections.Generic.List[int]
  foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue)) {
    $exe = ""
    if ($p.ExecutablePath) { $exe = [string]$p.ExecutablePath }
    if ($exe -ne "" -and $exe.ToLower() -eq $expectedExe.ToLower()) { [void]$out.Add([int]$p.ProcessId) }
  }
  return $out
}

try {
  # ---- Confinement (defence in depth; the RemoteApp start command is server-derived) ----
  if ($RESERVED_ACCOUNT_IDS -contains $AccountId) { Fail "refusing_reserved_identity" }
  if ($AccountId -le 0) { Fail "refusing_account_id_out_of_range" }
  if ($Username -ne ("guvfx_u_" + $AccountId)) { Fail "refusing_username_mismatch" }
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "refusing_path_traversal" }
  $expected = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $ACCOUNTS_BASE ([string]$AccountId)) "terminal"))
  if ($full.ToLower() -ne $expected.ToLower()) { Fail "refusing_terminal_root_mismatch" }
  if (-not (Test-Path -LiteralPath $full)) { Fail "terminal_root_missing" }
  $exe = [System.IO.Path]::GetFullPath((Join-Path $full "terminal64.exe"))
  if (-not (Test-Path -LiteralPath $exe)) { Fail "terminal64_missing" }

  # ---- Per-tenant serialisation (Global\ = cross-session; NAME is per-account = never cross-tenant) ----
  $mutexName = "Global\GuvFX_MT5_launch_" + $AccountId
  $mutex = New-Object System.Threading.Mutex($false, $mutexName)
  $held = $false
  try {
    try { $held = $mutex.WaitOne($MUTEX_WAIT_MS) } catch [System.Threading.AbandonedMutexException] { $held = $true }
    if (-not $held) { Fail "launch_serialisation_timeout" }

    $pids = Get-TenantTerminalPids $exe
    $result.count_before = $pids.Count

    if ($pids.Count -ge 2) {
      # Never arbitrate between duplicates - fail closed; the governed relaunch is the only recovery authority.
      $result.mode = "refuse"; $result.count_after = $pids.Count
      Fail "duplicate_terminal"
    }
    elseif ($pids.Count -eq 1) {
      # Reuse the single existing tenant terminal (refresh/reconnect path). Do NOT launch a second.
      $result.mode = "reuse"; $result.launched_pid = $pids[0]; $result.count_after = 1
      $result.ok = $true; $result.reason = "ok"
    }
    else {
      # Zero existing -> launch EXACTLY one. /portable is hard-coded (never taken from an argument).
      $proc = Start-Process -FilePath $exe -ArgumentList "/portable" -PassThru
      $result.launched_pid = [int]$proc.Id
      # Confirm exactly one now exists at the exact path (bounded wait); refuse if a race produced two.
      $deadline = (Get-Date).AddSeconds(20); $after = @()
      while ((Get-Date) -lt $deadline) {
        $after = Get-TenantTerminalPids $exe
        if ($after.Count -ge 1) { break }
        Start-Sleep -Milliseconds 300
      }
      $result.count_after = $after.Count
      if ($after.Count -ge 2) { $result.mode = "refuse"; Fail "duplicate_terminal_after_launch" }
      $result.mode = "launch"; $result.ok = $true; $result.reason = "ok"
    }
  }
  finally {
    if ($held) { try { $mutex.ReleaseMutex() } catch {} }
    $mutex.Dispose()
  }

  Emit
  # Hold the RemoteApp session for exactly as long as the tenant terminal lives (unless validating).
  if ($env:GUVFX_LAUNCH_NO_HOLD -ne "1" -and $result.launched_pid -gt 0) {
    try { Wait-Process -Id $result.launched_pid -ErrorAction SilentlyContinue } catch {}
  }
  exit 0
}
catch {
  Fail "launch_exception"
}

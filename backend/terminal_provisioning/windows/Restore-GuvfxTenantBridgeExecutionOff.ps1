<#
  ADR-0049 LIVE bridge provisioning integrator - restore THIS tenant's OWN per-tenant order bridge to the
  canonical EXECUTION-OFF posture (MT5_ALLOW_LIVE removed) and restart ONLY this tenant's scheduled task, then
  ATTEST OFF. Risk-reducing, idempotent, and usable even when the LIVE authorization is expired/consumed/revoked
  and the LIVE arm flags are OFF (the backend renders the OFF env with render_bridge_env(allow_live=False),
  which never needs the flags/authorization).

  Reviewed host primitive for the signed executor (restore_tenant_bridge_execution_off). The authoritative
  OFF environment is produced by the backend and piped to STDIN as base64. This script decodes it, re-validates
  allow-listed keys, REFUSES any payload that still contains MT5_ALLOW_LIVE, writes it atomically with read-back,
  restarts ONLY this tenant's task, and attests that MT5_ALLOW_LIVE is OFF.

  Server-derived args only: -AccountId, -TerminalRoot, -Port. STDIN: base64 of the backend-rendered OFF env.
  Isolation identical to Activate-GuvfxTenantBridgeLive.ps1. ASCII-only (RULE 9).
#>
param(
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][string]$TerminalRoot,
  [Parameter(Mandatory=$true)][int]$Port
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$TENANTS_BASE  = "C:\GuvFX\tenants"
$SHARED_DIR    = "C:\GuvFX\node2"
$ALLOWED_KEYS  = @("MT5_ACCOUNT_ID","MT5_TERMINAL_PATH","HTTP_SERVER_PORT","MT5_EXPECTED_LOGIN",
                   "MT5_EXPECTED_SERVER","MT5_EXPECTED_WINDOWS_USERNAME","MT5_EXPECTED_WORKSPACE_UUID",
                   "MT5_EXPECTED_IS_DEMO","MT5_REQUIRE_IDENTITY_PIN","MT5_GUARDED_ATTACH")
$result = [ordered]@{ account_id=$AccountId; port=$Port; ok=$false; reason="";
                      allow_live=$false; require_pin=$false; guarded_attach=$false; env_sha256=""; task=""; listening=$false }
function Fail([string]$why) { $result.ok=$false; $result.reason=$why; $result | ConvertTo-Json -Compress; exit 1 }
function Done([string]$why) { $result.ok=$true;  $result.reason=$why; $result | ConvertTo-Json -Compress; exit 0 }

try {
  if ($AccountId -le 1) { Fail "reserved_identity_refused" }
  if ($Port -lt 8800 -or $Port -gt 8899) { Fail "port_out_of_range" }
  $expectedRoot = Join-Path (Join-Path $ACCOUNTS_BASE ([string]$AccountId)) "terminal"
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "path_traversal_refused" }
  if ($full.TrimEnd('\').ToLower() -ne $expectedRoot.TrimEnd('\').ToLower()) { Fail "terminal_root_confinement" }

  $b64 = ([Console]::In.ReadToEnd()).Trim()
  if ([string]::IsNullOrEmpty($b64)) { Fail "env_payload_missing" }
  try { $envText = [System.Text.Encoding]::ASCII.GetString([System.Convert]::FromBase64String($b64)) }
  catch { Fail "env_payload_malformed" }
  if ($envText.Length -eq 0 -or $envText.Length -gt 8192) { Fail "env_payload_size_invalid" }

  # ---- Re-validate; REFUSE any residual MT5_ALLOW_LIVE (this op guarantees OFF) ----
  $sawPin=$false; $sawGuard=$false
  foreach ($ln in ($envText -replace "`r`n","`n").Split("`n")) {
    $s = $ln.Trim()
    if ($s -eq "" -or $s.ToUpper().StartsWith("REM ")) { continue }
    if (-not $s.StartsWith("set ")) { Fail "env_line_malformed" }
    $body = $s.Substring(4)
    $eq = $body.IndexOf("=")
    if ($eq -lt 1) { Fail "env_line_malformed" }
    $key = $body.Substring(0,$eq).Trim()
    $val = $body.Substring($eq+1).Trim()
    if ($key -eq "MT5_ALLOW_LIVE") { Fail "env_unexpected_live" }      # OFF env must NEVER carry it
    if ($ALLOWED_KEYS -notcontains $key) { Fail "env_key_not_allowed" }
    if ($val.IndexOfAny([char[]]('&','|','<','>','^','%','"')) -ge 0) { Fail "env_value_unsafe" }
    if ($key -eq "MT5_REQUIRE_IDENTITY_PIN") { $sawPin   = ($val -eq "1") }
    if ($key -eq "MT5_GUARDED_ATTACH")       { $sawGuard = ($val -eq "1") }
  }
  if (-not ($sawPin -and $sawGuard)) { Fail "env_missing_mandatory_pin_guard" }
  $result.allow_live=$false; $result.require_pin=$sawPin; $result.guarded_attach=$sawGuard

  $bin = Join-Path $SHARED_DIR "mt5_signal_bridge.py"
  $wd  = Join-Path $SHARED_DIR "tenant_bridge_watchdog.ps1"
  foreach ($p in @($bin,$wd)) { if (-not (Test-Path $p)) { Fail "bridge_package_missing" } }

  $tenantDir = Join-Path $TENANTS_BASE ([string]$AccountId)
  New-Item -ItemType Directory -Force -Path $tenantDir | Out-Null

  $envFile = Join-Path $tenantDir "bridge.env.bat"
  $tmp = Join-Path $tenantDir ("bridge.env.staging-" + ([guid]::NewGuid().ToString("N")) + ".bat")
  [System.IO.File]::WriteAllText($tmp, $envText, [System.Text.Encoding]::ASCII)
  $shaTmp = (Get-FileHash -Algorithm SHA256 -Path $tmp).Hash.ToLower()
  if (Test-Path $envFile) { Remove-Item -Force $envFile }
  [System.IO.File]::Move($tmp, $envFile)
  $result.env_sha256 = (Get-FileHash -Algorithm SHA256 -Path $envFile).Hash.ToLower()
  if ($result.env_sha256 -ne $shaTmp) { Fail "env_readback_mismatch" }

  $tokens = "C:\GuvFX\secrets\bridge.tokens.bat"
  $launcher = Join-Path $tenantDir "start_tenant_bridge.bat"
  $lines = @(
    "@echo off",
    "set GUVFX_API_URL=https://api.guvfx.com",
    ("call """ + $tokens + """"),
    ("call """ + $envFile + """"),
    ("set POLL_INTERVAL_SECONDS=2"),
    ("python """ + $bin + """")
  )
  Set-Content -Path $launcher -Value $lines -Encoding ASCII

  $taskName = "GuvFX_TenantBridge_$AccountId"
  $czXmlRaw = & schtasks /query /tn '\GuvFX_SignalBridge' /xml ONE 2>$null
  if (-not $czXmlRaw) { Fail "cz_task_xml_unavailable" }
  $tenantXml = ($czXmlRaw -join "`n") -replace [regex]::Escape('C:\GuvFX\start_signal_bridge.bat'), $launcher
  $xmlPath = Join-Path $tenantDir "task.xml"
  Set-Content -Path $xmlPath -Value $tenantXml -Encoding Unicode
  & schtasks /end /tn $taskName 2>$null | Out-Null
  # Port-targeted kill (never a blanket python kill): terminate ONLY the process bound to THIS -Port so the
  # restart RELOADS the OFF env - a lingering old (possibly LIVE) process must never keep answering on the port.
  $owners = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue | Select-Object -Expand OwningProcess -Unique)
  foreach ($opid in $owners) { try { Stop-Process -Id $opid -Force -ErrorAction SilentlyContinue } catch { } }
  & schtasks /create /tn $taskName /xml $xmlPath /f | Out-Null

  $wdTask = "GuvFX_TenantBridgeWatchdog_$AccountId"
  $wdCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File " + $wd + " -Port " + [string]$Port + " -Task " + $taskName
  & schtasks /create /tn $wdTask /tr $wdCmd /sc MINUTE /mo 1 /ru SYSTEM /rl HIGHEST /f | Out-Null
  $result.task = $taskName

  & schtasks /run /tn $taskName | Out-Null
  $token = ((Select-String -Path $tokens -Pattern '^\s*set\s+GUVFX_AGENT_TOKEN=').Line -replace '^\s*set\s+GUVFX_AGENT_TOKEN=','').Trim()
  for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    $listen = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
    if ($listen.Count -gt 0) { $result.listening = $true }
    try {
      $r = Invoke-WebRequest -Uri "http://localhost:$Port/health" -Headers @{ 'X-GuvFX-Agent-Token' = $token } -TimeoutSec 5 -UseBasicParsing
      if (($r.Content | ConvertFrom-Json).ok -eq $true) { Done "execution_off_restored" }
    } catch { }
  }
  Fail "health_check_timeout"
} catch {
  Fail "restore_error"
}

<#
  P0-B1.1 multi-tenant - activate THIS tenant's OWN pin-enforcing ORDER BRIDGE on its per-tenant PORT and
  health-check it. The general per-customer equivalent of Activate-GuvfxOrderBridge.ps1's single node bridge:
  many isolated tenants share one Windows host, each with a PRIVATE bridge process + port + scheduled task +
  port-specific watchdog.

  Reviewed host primitive for the signed executor (activate_tenant_bridge). Server-derived args only:
      -AccountId    the tenant trading-account id (NEVER 1 / Customer Zero)
      -TerminalRoot the tenant terminal dir (must be exactly C:\GuvFX\accounts\<AccountId>\terminal)
      -Port         the tenant's dedicated port from its HostedExecutionEndpoint (backend-allocated 8800-8899)

  It configures + starts a SEPARATE mt5_signal_bridge process bound to -Port, running MT5_REQUIRE_IDENTITY_PIN=1
  + MT5_GUARDED_ATTACH=1, DEMO only (MT5_ALLOW_LIVE never set). The per-ORDER identity pin carries the expected
  login/server, so no broker login is needed here. Isolation: it writes ONLY this tenant's env (account id +
  terminal path + port) under C:\GuvFX\tenants\<id>, registers ONLY this tenant's task/watchdog, and its
  watchdog restarts ONLY the process bound to -Port - never a blanket python kill - so it can never touch
  another tenant's bridge or Customer Zero's :8788.

  CUSTOMER-ZERO SAFETY: refuses AccountId 1 and confines -TerminalRoot to this account's own tree.
  Idempotent: bind-guards a double start; re-running re-asserts the same env + task. ASCII-only (RULE 9).
  Emits a single compact JSON object as the LAST line; ok=$true + exit 0 only on a verified /health.

  Usage:
    powershell -NoProfile -File Activate-GuvfxTenantBridge.ps1 -AccountId 26 -TerminalRoot 'C:\GuvFX\accounts\26\terminal' -Port 8801
#>
param(
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][string]$TerminalRoot,
  [Parameter(Mandatory=$true)][int]$Port
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$TENANTS_BASE  = "C:\GuvFX\tenants"
$SHARED_DIR    = "C:\GuvFX\node2"            # the shared, reviewed bridge binary + launcher template live here
$result = [ordered]@{ account_id=$AccountId; port=$Port; ok=$false; reason="" }

function Fail([string]$why) { $result.ok=$false; $result.reason=$why; $result | ConvertTo-Json -Compress; exit 1 }
function Done([string]$why) { $result.ok=$true;  $result.reason=$why; $result | ConvertTo-Json -Compress; exit 0 }

try {
  # ---- Customer-Zero + path + port confinement (fail closed) ----
  if ($AccountId -le 1) { Fail "reserved_identity_refused" }
  if ($Port -lt 8800 -or $Port -gt 8899) { Fail "port_out_of_range" }
  $expectedRoot = Join-Path (Join-Path $ACCOUNTS_BASE ([string]$AccountId)) "terminal"
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "path_traversal_refused" }
  if ($full.TrimEnd('\').ToLower() -ne $expectedRoot.TrimEnd('\').ToLower()) { Fail "terminal_root_confinement" }
  $terminalExe = Join-Path $full "terminal64.exe"

  # ---- The shared bridge package must be staged (binary + launcher template + watchdog) ----
  $bin = Join-Path $SHARED_DIR "mt5_signal_bridge.py"
  $tpl = Join-Path $SHARED_DIR "node2_bridge.env.template.bat"
  $wd  = Join-Path $SHARED_DIR "node2_bridge_watchdog.ps1"
  foreach ($p in @($bin,$tpl,$wd)) { if (-not (Test-Path $p)) { Fail "bridge_package_missing" } }

  $tenantDir = Join-Path $TENANTS_BASE ([string]$AccountId)
  New-Item -ItemType Directory -Force -Path $tenantDir | Out-Null

  # ---- Bind-guard: if -Port is already LISTENING, it must be OUR tenant task (idempotent); else refuse so we
  #      never bind onto a port another tenant/CZ owns. ----
  $listening = @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
  $taskName = "GuvFX_TenantBridge_$AccountId"
  if ($listening.Count -gt 0) {
    $existing = & schtasks /query /tn "\$taskName" 2>$null
    if (-not $existing) { Fail "port_owned_by_other" }
    Done "already_active"
  }

  # ---- 1) Write THIS tenant's env (account id + terminal path + port; pin carries identity). ASCII. ----
  $envOut = (Get-Content -Raw -Path $tpl) `
    -replace '__ACCOUNT_ID__', ([string]$AccountId) `
    -replace '__TERMINAL_PATH__', $terminalExe `
    -replace '__EXPECTED_LOGIN__', '' `
    -replace '__EXPECTED_SERVER__', ''
  $envOut = $envOut -replace 'set HTTP_SERVER_PORT=\d+', ("set HTTP_SERVER_PORT=" + [string]$Port)
  $envFile = Join-Path $tenantDir "bridge.env.bat"
  Set-Content -Path $envFile -Value $envOut -Encoding ASCII

  # ---- 2) Per-tenant launcher: run the SHARED bridge binary with THIS tenant's env on THIS port. ASCII.
  #         Mirrors the certified start_node2_bridge.bat: the bridge's validate_config REQUIRES GUVFX_API_URL +
  #         GUVFX_WORKER_TOKEN + GUVFX_AGENT_TOKEN (from the shared bridge.tokens.bat - the documented Closed-
  #         Beta shared-token exception) BEFORE MT5_ACCOUNT_ID, or it exits 1 without binding the port. ----
  $tokens = "C:\GuvFX\secrets\bridge.tokens.bat"
  $launcher = Join-Path $tenantDir "start_tenant_bridge.bat"
  $lines = @(
    "@echo off",
    "set GUVFX_API_URL=https://api.guvfx.com",
    ("call """ + $tokens + """"),
    ("call """ + $envFile + """"),
    ("set MT5_REQUIRE_IDENTITY_PIN=1"),
    ("set MT5_GUARDED_ATTACH=1"),
    ("set POLL_INTERVAL_SECONDS=2"),
    ("set HTTP_SERVER_PORT=" + [string]$Port),
    ("python """ + $bin + """")
  )
  Set-Content -Path $launcher -Value $lines -Encoding ASCII

  # ---- 3) Register THIS tenant's bridge task by cloning Customer Zero's run-as principal (identical
  #         cross-session MT5 attach), re-pointed at THIS tenant launcher. CZ's own task is never modified. ----
  $czXmlRaw = & schtasks /query /tn '\GuvFX_SignalBridge' /xml ONE 2>$null
  if (-not $czXmlRaw) { Fail "cz_task_xml_unavailable" }
  $tenantXml = ($czXmlRaw -join "`n") -replace [regex]::Escape('C:\GuvFX\start_signal_bridge.bat'), $launcher
  $xmlPath = Join-Path $tenantDir "task.xml"
  Set-Content -Path $xmlPath -Value $tenantXml -Encoding Unicode
  & schtasks /create /tn $taskName /xml $xmlPath /f | Out-Null

  # ---- 4) Per-tenant, port-targeted watchdog (SYSTEM). It restarts ONLY the process bound to THIS -Port -
  #         never a blanket python kill - so it can never touch another tenant/CZ. ----
  $wdTask = "GuvFX_TenantBridgeWatchdog_$AccountId"
  $wdCmd = "powershell -NoProfile -ExecutionPolicy Bypass -File " + $wd + " -Port " + [string]$Port + " -Task " + $taskName
  & schtasks /create /tn $wdTask /tr $wdCmd /sc MINUTE /mo 1 /ru SYSTEM /rl HIGHEST /f | Out-Null

  # ---- 5) Start + verify /health on THIS -Port (shared inbound token - documented temporary exception) ----
  & schtasks /run /tn $taskName | Out-Null
  $token = ((Select-String -Path 'C:\GuvFX\secrets\bridge.tokens.bat' -Pattern '^\s*set\s+GUVFX_AGENT_TOKEN=').Line -replace '^\s*set\s+GUVFX_AGENT_TOKEN=','').Trim()
  for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    try {
      $r = Invoke-WebRequest -Uri "http://localhost:$Port/health" -Headers @{ 'X-GuvFX-Agent-Token' = $token } -TimeoutSec 5 -UseBasicParsing
      if (($r.Content | ConvertFrom-Json).ok -eq $true) { Done "activated" }
    } catch { }
  }
  Fail "health_check_timeout"
} catch {
  Fail "activation_error"
}

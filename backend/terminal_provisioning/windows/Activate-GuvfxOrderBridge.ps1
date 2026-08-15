<#
  FINAL Closed-Beta stream - activate THIS node's dedicated pin-enforcing ORDER BRIDGE and health-check it.

  Reviewed host primitive for the signed executor (activate_order_bridge). Server-derived args only:
      -AccountId    the tenant trading-account id (NEVER 1 / Customer Zero)
      -TerminalRoot the tenant terminal dir (must be exactly C:\GuvFX\accounts\<AccountId>\terminal)

  It configures + starts the node's SECOND, separate mt5_signal_bridge process on its OWN port (:8789),
  supervised by its OWN scheduled task + port-specific watchdog, running MT5_REQUIRE_IDENTITY_PIN=1 +
  MT5_GUARDED_ATTACH=1, DEMO only (MT5_ALLOW_LIVE unset). It writes ONLY the account-specific env (account id
  + terminal path); the per-ORDER identity pin carries the expected login/server, so no broker login is
  needed here. It never touches Customer Zero's :8788 bridge / tasks / watchdog / secrets (it only READS the
  shared token - the documented, temporary Closed-Beta exception). Idempotent: the launcher bind-guards a
  double start; re-running re-asserts the same env + tasks.

  CUSTOMER-ZERO SAFETY: refuses AccountId 1 and confines -TerminalRoot to this account's own tree.
  ASCII-only (RULE 9). Emits a single compact JSON object as the LAST line; ok=$true + exit 0 only on a
  verified /health.

  Usage:
    powershell -NoProfile -File Activate-GuvfxOrderBridge.ps1 -AccountId 14 -TerminalRoot 'C:\GuvFX\accounts\14\terminal'
#>
param(
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][string]$TerminalRoot
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$NODE2_DIR = "C:\GuvFX\node2"
$PORT = 8789
# The bridge's PORT is a fixed constant that MUST equal the backend's ORDER_BRIDGE_PORT and the launcher's
# HTTP_SERVER_PORT (all 8789). The backend derives + persists the authoritative endpoint from the node's
# rdp_host + that port; this primitive only reports ok/reason/port (it does not know its own rdp_host).
$result = [ordered]@{ account_id=$AccountId; port=$PORT; ok=$false; reason="" }

function Fail([string]$why) { $result.ok=$false; $result.reason=$why; $result | ConvertTo-Json -Compress; exit 1 }
function Done([string]$why) { $result.ok=$true;  $result.reason=$why; $result | ConvertTo-Json -Compress; exit 0 }

try {
  # ---- Customer-Zero + path confinement (fail closed) ----
  if ($AccountId -le 1) { Fail "reserved_identity_refused" }
  $expectedRoot = Join-Path (Join-Path $ACCOUNTS_BASE ([string]$AccountId)) "terminal"
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "path_traversal_refused" }
  if ($full.TrimEnd('\').ToLower() -ne $expectedRoot.TrimEnd('\').ToLower()) { Fail "terminal_root_confinement" }
  $terminalExe = Join-Path $full "terminal64.exe"

  # ---- The node bridge package must be staged (binary + launcher + template + watchdog) ----
  $bin = Join-Path $NODE2_DIR "mt5_signal_bridge.py"
  $bat = Join-Path $NODE2_DIR "start_node2_bridge.bat"
  $tpl = Join-Path $NODE2_DIR "node2_bridge.env.template.bat"
  $wd  = Join-Path $NODE2_DIR "node2_bridge_watchdog.ps1"
  foreach ($p in @($bin,$bat,$tpl,$wd)) { if (-not (Test-Path $p)) { Fail "node_bridge_package_missing" } }

  # ---- 1) Write the account-specific env (account id + terminal path only; pin carries identity) ----
  $envOut = (Get-Content -Raw -Path $tpl) `
    -replace '__ACCOUNT_ID__', ([string]$AccountId) `
    -replace '__TERMINAL_PATH__', $terminalExe `
    -replace '__EXPECTED_LOGIN__', '' `
    -replace '__EXPECTED_SERVER__', ''
  Set-Content -Path (Join-Path $NODE2_DIR "node2_bridge.env.bat") -Value $envOut -Encoding ASCII

  # ---- 2) Register the bridge task by CLONING Customer Zero's run-as principal (identical cross-session
  #         MT5 attach) - re-pointed at the node2 launcher. CZ's own task is never modified. ----
  $czXmlRaw = & schtasks /query /tn '\GuvFX_SignalBridge' /xml ONE 2>$null
  if (-not $czXmlRaw) { Fail "cz_task_xml_unavailable" }
  $node2Xml = ($czXmlRaw -join "`n") -replace [regex]::Escape('C:\GuvFX\start_signal_bridge.bat'), 'C:\GuvFX\node2\start_node2_bridge.bat'
  $xmlPath = Join-Path $NODE2_DIR "GuvFX_Node2Bridge.xml"
  Set-Content -Path $xmlPath -Value $node2Xml -Encoding Unicode
  & schtasks /create /tn 'GuvFX_Node2Bridge' /xml $xmlPath /f | Out-Null

  # ---- 3) Port-specific watchdog (SYSTEM). It only ever restarts the process bound to :8789 - never a
  #         blanket python kill - so it can never touch Customer Zero's :8788 bridge. ----
  & schtasks /create /tn 'GuvFX_Node2BridgeWatchdog' /tr 'powershell -NoProfile -ExecutionPolicy Bypass -File C:\GuvFX\node2\node2_bridge_watchdog.ps1' /sc MINUTE /mo 1 /ru SYSTEM /rl HIGHEST /f | Out-Null

  # ---- 4) Start + verify /health on :8789 (shared inbound token - documented temporary exception) ----
  & schtasks /run /tn 'GuvFX_Node2Bridge' | Out-Null
  $token = ((Select-String -Path 'C:\GuvFX\secrets\bridge.tokens.bat' -Pattern '^\s*set\s+GUVFX_AGENT_TOKEN=').Line -replace '^\s*set\s+GUVFX_AGENT_TOKEN=','').Trim()
  for ($i = 0; $i -lt 15; $i++) {
    Start-Sleep -Seconds 2
    try {
      $r = Invoke-WebRequest -Uri "http://localhost:$PORT/health" -Headers @{ 'X-GuvFX-Agent-Token' = $token } -TimeoutSec 5 -UseBasicParsing
      if (($r.Content | ConvertFrom-Json).ok -eq $true) { Done "activated" }
    } catch { }
  }
  Fail "health_check_timeout"
} catch {
  Fail "activation_error"
}

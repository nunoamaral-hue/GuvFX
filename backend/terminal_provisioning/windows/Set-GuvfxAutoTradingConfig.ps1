<#
  Stream 5 - write the empirically certified AutoTrading CAPABILITY config into a hosted runtime (idempotent).

  Writes ONLY the [Experts] block certified as the minimum effective AutoTrading config (PR #336):
      [Experts]
      AllowLiveTrading=1
      Enabled=1
  into <terminal_root>\config\common.ini. This is CAPABILITY only: it authorises NO order. A live order still
  requires an independent, human-gated chain (broker login inside MT5, the per-workspace arm, the
  HOSTED_MT5_EXECUTION_ENABLED subsystem gate, and the live order-time bridge) - none of which this touches.

  Per-runtime mutation (NOT the immutable golden - RULE 10). Idempotent: re-running re-asserts the same keys.
  ASCII-only (RULE 9). Emits a single compact JSON object.

  Usage:
    powershell -NoProfile -File Set-GuvfxAutoTradingConfig.ps1 -TerminalRoot 'C:\GuvFX\accounts\14\terminal'
#>
param(
  [Parameter(Mandatory=$true)][string]$TerminalRoot
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$result = [ordered]@{ terminal_root=$TerminalRoot; common_ini=""; allow_live_trading=$false; enabled=$false; ok=$false; reason="" }

function Fail([string]$why) { $result.ok=$false; $result.reason=$why; $result | ConvertTo-Json -Compress; exit 1 }

try {
  # Confine: terminal_root must be exactly <accounts base>\<id>\terminal (no traversal / foreign tree).
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "refusing: path traversal" }
  if (-not ($full.ToLower().StartsWith(($ACCOUNTS_BASE.ToLower() + "\")))) { Fail "refusing: outside accounts base" }
  if (-not ($full.ToLower().EndsWith("\terminal"))) { Fail "refusing: not a terminal root" }
  if (-not (Test-Path -LiteralPath $full)) { Fail "terminal root does not exist" }

  $cfgDir = Join-Path $full "config"
  New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null
  $common = Join-Path $cfgDir "common.ini"

  # Read existing lines (if any), drop any prior [Experts] block, then re-append the certified capability block.
  $existing = @()
  if (Test-Path -LiteralPath $common) { $existing = Get-Content -LiteralPath $common -ErrorAction SilentlyContinue }
  $kept = New-Object System.Collections.Generic.List[string]
  $inExperts = $false
  foreach ($line in $existing) {
    if ($line -match '^\s*\[.*\]\s*$') { $inExperts = ($line -match '^\s*\[Experts\]\s*$') }
    if (-not $inExperts) { $kept.Add($line) }
  }
  $block = @("[Experts]", "AllowLiveTrading=1", "AllowDllImport=0", "Enabled=1")
  $out = @()
  $out += $kept
  if ($kept.Count -gt 0 -and $kept[$kept.Count-1] -ne "") { $out += "" }
  $out += $block
  Set-Content -LiteralPath $common -Value ($out -join "`r`n") -Encoding ASCII

  # Read back and confirm the two certified keys are present with value 1.
  $back = Get-Content -LiteralPath $common
  $result.allow_live_trading = [bool]($back | Where-Object { $_ -match '^\s*AllowLiveTrading\s*=\s*1\s*$' })
  $result.enabled            = [bool]($back | Where-Object { $_ -match '^\s*Enabled\s*=\s*1\s*$' })
  $result.common_ini = $common
  if (-not ($result.allow_live_trading -and $result.enabled)) { Fail "read-back did not confirm capability keys" }
  $result.ok = $true
  $result | ConvertTo-Json -Compress
}
catch { $result.ok=$false; $result.reason="error"; $result | ConvertTo-Json -Compress; exit 1 }

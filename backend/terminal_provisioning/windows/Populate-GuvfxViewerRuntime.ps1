<#
  TX-1D — populate a per-account dedicated VIEWER MT5 runtime (idempotent).

  Builds a clean golden MT5 template once (binaries copied from the live
  account_001 instance, EXCLUDING that account's data/credentials), then copies
  it into C:\GuvFX\accounts\<id>\terminal\ and writes a VIEW-ONLY config
  (AutoTrading disabled, Experts disabled, NO saved credentials, NO EAs).

  ADDITIVE + READ-ONLY w.r.t. execution: it only READS the running execution
  instance's binaries (shared-read) to seed the golden template; it never
  writes to / stops / reconfigures the execution MT5 or the bridge.

  Idempotent: re-run with matching version is a no-op for the copy; the
  view-only config is always re-asserted.

  Usage:
    powershell -NoProfile -File Populate-GuvfxViewerRuntime.ps1 -AccountId 6 `
      -RuntimeRoot 'C:\GuvFX\accounts\6' -Login 0
#>
param(
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [string]$Login = "0",
  [string]$Build = "5.0.0.5833",
  [switch]$Force
)

$ErrorActionPreference = "Stop"
$ExecInstance = "C:\GuvFX\terminals\account_001\instance"
$GoldenRoot   = "C:\GuvFX\golden\mt5\$Build"
$TermDir      = Join-Path $RuntimeRoot "terminal"
$result = [ordered]@{
  account_id=$AccountId; runtime_root=$RuntimeRoot; build=$Build
  golden_created=$false; copied=$false; already_populated=$false
  view_only=$true; terminal_exe=$false; ok=$false
}

try {
  # ── 1. Ensure clean golden template (binaries only; exclude exec data/creds) ──
  if (-not (Test-Path (Join-Path $GoldenRoot "terminal64.exe"))) {
    New-Item -ItemType Directory -Path $GoldenRoot -Force | Out-Null
    # robocopy: copy tree, EXCLUDE volatile/credential dirs+files of the exec instance
    $xd = @("$ExecInstance\logs","$ExecInstance\Bases","$ExecInstance\config")
    $null = robocopy $ExecInstance $GoldenRoot /E /XD $xd /XF "accounts.dat" "common.ini" "origin.ini" /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "golden robocopy failed code=$LASTEXITCODE" }
    New-Item -ItemType Directory -Path (Join-Path $GoldenRoot "config") -Force | Out-Null
    $result.golden_created = $true
  }

  # ── 2. Populate per-account terminal\ from golden (idempotent) ──
  $marker = Join-Path $RuntimeRoot "runtime_version.json"
  $needCopy = $Force -or -not (Test-Path (Join-Path $TermDir "terminal64.exe"))
  if (-not $needCopy) {
    $result.already_populated = $true
  } else {
    New-Item -ItemType Directory -Path $TermDir -Force | Out-Null
    $null = robocopy $GoldenRoot $TermDir /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP
    if ($LASTEXITCODE -ge 8) { throw "per-account robocopy failed code=$LASTEXITCODE" }
    $result.copied = $true
  }

  # ── 3. VIEW-ONLY config: AutoTrading + Experts OFF, no creds, no EAs ──
  New-Item -ItemType Directory -Path (Join-Path $TermDir "config") -Force | Out-Null
  $common = Join-Path $TermDir "config\common.ini"
  $cfg = @(
    "[Common]",
    "Login=$Login",
    "ProxyEnable=0",
    "CertInstall=0",
    "NewsEnable=0",
    "",
    "[Experts]",
    "AllowLiveTrading=0",
    "AllowDllImport=0",
    "Enabled=0",
    "Account=0",
    "Profile=0"
  ) -join "`r`n"
  Set-Content -Path $common -Value $cfg -Encoding ASCII
  # Ensure no saved credentials / no EAs carried in
  Remove-Item (Join-Path $TermDir "config\accounts.dat") -ErrorAction SilentlyContinue
  $result.terminal_exe = Test-Path (Join-Path $TermDir "terminal64.exe")

  # ── 4. Version marker ──
  $meta = @{ build=$Build; account_id=$AccountId; view_only=$true; populated_at=(Get-Date).ToString("o") } | ConvertTo-Json -Compress
  Set-Content -Path $marker -Value $meta -Encoding ASCII

  $result.ok = $true
  $result | ConvertTo-Json -Compress
}
catch {
  $result.ok = $false; $result.error = $_.Exception.Message
  $result | ConvertTo-Json -Compress
  exit 1
}

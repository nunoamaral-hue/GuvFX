<#
  STREAM 10E - deterministic isolation FINGERPRINT for before/after comparison (ADR-0043). Read-only.

  Captures a stable, hashable snapshot so two runs (BEFORE vs AFTER a cert action) can be byte-compared to prove
  either (a) on the DISPOSABLE cert host: the intended W^X state is present, or (b) on the Customer Zero PRODUCTION
  host at the eventual roll-out: CZ was NOT disturbed (its terminal, policy and runtime ACL are unchanged). This is
  the "Customer Zero before/after verification" primitive; it performs NO mutation and NO broker action.

  Snapshot fields (all hashed where content matters):
    - effective AppLocker policy (canonical SHA256)
    - runtime-root DACL SDDL (SHA256) for the given -RuntimeRoot
    - config\common.ini AllowDllImport value + file SHA256
    - terminal64.exe product version + SHA256 (proves the golden binary is the expected one)
    - the interactive-session process set for -SessionUser (sorted names) - proves a live MT5 session is intact/absent
  Diff two outputs with Compare-Object on the flattened fields, or compare the top-level fingerprint_sha256.

  ASCII-ONLY (RULE 9); ParseFile()-validate first. Emits one JSON object to -OutFile and stdout.

  Usage:
    powershell -NoProfile -File Get-GuvfxIsolationFingerprint.ps1 -RuntimeRoot 'C:\GuvFX\accounts\1' `
        -SessionUser guvfx_u_1 -Label before -OutFile 'C:\GuvFX\_cert\cz_before.json'
#>
param(
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [string]$SessionUser = "",
  [Parameter(Mandatory=$true)][ValidateSet("before","after","reference")][string]$Label,
  [Parameter(Mandatory=$true)][string]$OutFile
)
$ErrorActionPreference = "Stop"

function Hash-String([string]$s) {
  $sha = [System.Security.Cryptography.SHA256]::Create()
  return ([System.BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($s))) -replace "-","")
}

try {
  $root = ($RuntimeRoot -replace "/","\").TrimEnd("\")
  if ($root -match "\.\.") { throw "path_traversal" }
  $fp = [ordered]@{ schema="guvfx.stream10e.fingerprint/1"; label=$Label; runtime_root=$root; captured_utc=(Get-Date).ToUniversalTime().ToString("o") }

  try { $eff = (Get-AppLockerPolicy -Effective -Xml); $fp.effective_policy_sha256 = Hash-String $eff } catch { $fp.effective_policy_sha256 = "unavailable" }

  try { $fp.runtime_root_dacl_sha256 = Hash-String ((Get-Acl -LiteralPath $root).Sddl) } catch { $fp.runtime_root_dacl_sha256 = "unavailable" }

  $commonIni = Join-Path $root "terminal\config\common.ini"
  if (Test-Path -LiteralPath $commonIni) {
    try { $fp.common_ini_sha256 = (Get-FileHash -LiteralPath $commonIni -Algorithm SHA256).Hash } catch { $fp.common_ini_sha256 = "unavailable" }
    try { $m = Select-String -Path $commonIni -Pattern "AllowDllImport\s*=\s*(\d)" -ErrorAction SilentlyContinue; $fp.allowdllimport = if ($m) { $m.Matches[0].Groups[1].Value } else { "" } } catch { $fp.allowdllimport = "unreadable" }
  } else { $fp.common_ini_sha256 = "absent"; $fp.allowdllimport = "absent" }

  $t64 = Join-Path $root "terminal\terminal64.exe"
  if (Test-Path -LiteralPath $t64) {
    try { $fp.terminal64_sha256 = (Get-FileHash -LiteralPath $t64 -Algorithm SHA256).Hash } catch { $fp.terminal64_sha256 = "unavailable" }
    try { $fp.terminal64_version = (Get-Item -LiteralPath $t64).VersionInfo.ProductVersion } catch { $fp.terminal64_version = "unavailable" }
  } else { $fp.terminal64_sha256 = "absent"; $fp.terminal64_version = "absent" }

  if ($SessionUser) {
    try {
      $procs = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
        $o = $null; try { $o = ($_.GetOwner()).User } catch {}
        if ($o -eq $SessionUser) { $_.Name } }) | Sort-Object -Unique
      $fp.session_user = $SessionUser
      $fp.session_process_set = $procs
      $fp.session_process_sha256 = Hash-String (($procs -join "|"))
    } catch { $fp.session_process_set = @(); $fp.session_process_sha256 = "unavailable" }
  }

  # Top-level fingerprint hash = hash of the stable content fields (excludes captured_utc/label so before/after match
  # iff the ISOLATION STATE matches, not the capture time).
  $stable = @($fp.effective_policy_sha256, $fp.runtime_root_dacl_sha256, $fp.common_ini_sha256, $fp.allowdllimport,
              $fp.terminal64_sha256, $fp.terminal64_version, $fp.session_process_sha256) -join "||"
  $fp.fingerprint_sha256 = Hash-String $stable

  New-Item -ItemType Directory -Path (Split-Path $OutFile) -Force | Out-Null
  ($fp | ConvertTo-Json -Depth 6) | Out-File -FilePath $OutFile -Encoding ASCII
  $fp | ConvertTo-Json -Depth 6 -Compress
}
catch { [ordered]@{ ok=$false; label=$Label; reason="$($_.Exception.Message)" } | ConvertTo-Json -Compress; exit 1 }

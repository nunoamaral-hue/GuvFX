<#
  Preseed-GuvfxBrokerArtefact.ps1  (Broker Catalogue V1 provisioning consumption)

  Copies ONE approved, immutable catalogue servers.dat into a tenant's FRESH runtime and read-back-verifies its
  SHA-256. It is the host half of broker_catalogue.preseed; the backend has already resolved that this broker is
  SUPPORTED and human-APPROVED for this exact SHA. This script re-asserts every safety property host-side:

    * Customer Zero (1) + the account-18 control are REFUSED (defence in depth).
    * Identity/paths are server-derived: -Username must equal guvfx_u_<AccountId>; the DEST is confined to exactly
      C:\GuvFX\accounts\<AccountId>\terminal\config\servers.dat (no traversal).
    * The SOURCE is confined to under C:\GuvFX\catalogue\versions (no traversal); it is READ-ONLY.
    * It NEVER touches the golden image, accounts.dat, or any other tenant. It logs in nothing and trades nothing.
    * FRESH-runtime only: refuses if a terminal64 for this runtime is currently RUNNING (never replace servers.dat
      under a live terminal).
    * FAIL-CLOSED: emits ok:true ONLY when the copied file's SHA-256 equals -ExpectedSha256.

  ASCII-only (RULE 9). Windows PowerShell 5.1 safe.
#>
param(
  [Parameter(Mandatory = $true)][string]$Username,
  [Parameter(Mandatory = $true)][string]$TerminalRoot,
  [Parameter(Mandatory = $true)][int]$AccountId,
  [Parameter(Mandatory = $true)][string]$BrokerId,
  [Parameter(Mandatory = $true)][string]$ExpectedSha256,
  [Parameter(Mandatory = $true)][string]$HostRelpath
)
$ErrorActionPreference = "Stop"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$CATALOGUE_BASE = "C:\GuvFX\catalogue"
$RESERVED = @(1, 18)
$result = [ordered]@{ account_id = $AccountId; broker_id = $BrokerId; copied = $false; verified_sha256 = "";
  ok = $false; reason = "" }
function Emit() { $result | ConvertTo-Json -Compress }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

try {
  if ($RESERVED -contains $AccountId) { Fail "refusing_reserved_identity" }
  if ($AccountId -le 0) { Fail "refusing_account_id_out_of_range" }
  if ($Username -ne ("guvfx_u_" + $AccountId)) { Fail "refusing_username_mismatch" }
  if ($ExpectedSha256 -notmatch '^[0-9a-f]{64}$') { Fail "refusing_sha_malformed" }

  # DEST confinement: exactly accounts\<id>\terminal\config\servers.dat
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "refusing_path_traversal" }
  $expectedRoot = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $ACCOUNTS_BASE ([string]$AccountId)) "terminal"))
  if ($full.TrimEnd('\').ToLower() -ne $expectedRoot.TrimEnd('\').ToLower()) { Fail "refusing_terminal_root_mismatch" }
  if (-not (Test-Path -LiteralPath $full)) { Fail "terminal_root_missing" }
  $destDir = Join-Path $full "config"
  if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
  $dest = Join-Path $destDir "servers.dat"

  # SOURCE confinement: under C:\GuvFX\catalogue\versions, no traversal, must be a servers.dat
  if ($HostRelpath -match '\.\.') { Fail "refusing_source_traversal" }
  $src = [System.IO.Path]::GetFullPath((Join-Path $CATALOGUE_BASE $HostRelpath))
  $verBase = [System.IO.Path]::GetFullPath((Join-Path $CATALOGUE_BASE "versions"))
  if (-not $src.ToLower().StartsWith(($verBase.TrimEnd('\') + "\").ToLower())) { Fail "refusing_source_outside_catalogue" }
  if ([System.IO.Path]::GetFileName($src).ToLower() -ne "servers.dat") { Fail "refusing_source_not_servers_dat" }
  if (-not (Test-Path -LiteralPath $src)) { Fail "source_missing" }

  # Source bytes must already match the approved SHA (the catalogue is immutable + hash-bound).
  $srcSha = (Get-FileHash -LiteralPath $src -Algorithm SHA256).Hash.ToLower()
  if ($srcSha -ne $ExpectedSha256.ToLower()) { Fail "source_sha_mismatch" }

  # FRESH-runtime only: never replace servers.dat under a live terminal for this runtime.
  $running = @(Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like ("*" + (Join-Path $ACCOUNTS_BASE ([string]$AccountId)) + "\*") })
  if ($running.Count -gt 0) { Fail "terminal_running_refuse_replace" }

  Copy-Item -LiteralPath $src -Destination $dest -Force
  $result.copied = $true
  $destSha = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash.ToLower()
  $result.verified_sha256 = $destSha
  if ($destSha -ne $ExpectedSha256.ToLower()) { Fail "readback_sha_mismatch" }

  $result.ok = $true
  $result.reason = "ok"
  Emit
  exit 0
}
catch { Fail "preseed_exception" }

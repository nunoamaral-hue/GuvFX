<#
  Attest-GuvfxCatalogueArtefact.ps1  (Broker Catalogue V3 read-only host attestation)

  Reports what is actually staged under the shared catalogue version tree so the backend can require EXACT
  agreement with the DRAFT manifest before activation. ACCOUNT-INDEPENDENT (no account, no slot). READ-ONLY:
  it computes hashes/sizes and enumerates directories; it writes NOTHING and launches NOTHING.

    * -BrokerId <slug>  -> attest exactly versions\<Label>\<BrokerId>\servers.dat: return its SHA-256 + size and,
                           when -ExpectedSha256 is given, whether it matches (ok=match).
    * -BrokerId ""      -> inventory: enumerate every broker subdir of versions\<Label> and return each
                           servers.dat's SHA-256 + size (ok=true). The backend compares this set to the manifest
                           to prove NO missing and NO extra broker/file.

  Destination is DERIVED and confined strictly under C:\GuvFX\catalogue\versions (no traversal / reparse escape).
  ASCII-only (RULE 9). Windows PowerShell 5.1 safe.
#>
param(
  [Parameter(Mandatory = $true)][string]$Label,
  [Parameter(Mandatory = $false)][string]$BrokerId = "",
  [Parameter(Mandatory = $false)][string]$ExpectedSha256 = ""
)
$ErrorActionPreference = "Stop"
$CATALOGUE_BASE = "C:\GuvFX\catalogue"
$result = [ordered]@{ label = $Label; mode = ""; ok = $false; reason = "" }
function Emit() { $result | ConvertTo-Json -Compress -Depth 5 }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

try {
  if ($Label -notmatch '^[A-Za-z0-9._\-]{1,32}$') { Fail "refusing_label_malformed" }
  if ($Label -match '\.\.') { Fail "refusing_path_traversal" }
  if ($BrokerId -ne "" -and $BrokerId -notmatch '^[a-z0-9_]{1,32}$') { Fail "refusing_broker_malformed" }
  if ($ExpectedSha256 -ne "" -and $ExpectedSha256 -notmatch '^[0-9a-f]{64}$') { Fail "refusing_sha_malformed" }

  $verBase = [System.IO.Path]::GetFullPath((Join-Path $CATALOGUE_BASE "versions"))
  $verDir = [System.IO.Path]::GetFullPath((Join-Path $verBase $Label))
  if (-not $verDir.ToLower().StartsWith(($verBase.TrimEnd('\') + "\").ToLower())) { Fail "refusing_dest_outside_catalogue" }
  foreach ($p in @($verBase, $verDir)) {
    if (Test-Path -LiteralPath $p) {
      $it = Get-Item -LiteralPath $p -Force
      if ($it.Attributes.ToString() -match 'ReparsePoint') { Fail "refusing_reparse_point" }
    }
  }

  if ($BrokerId -ne "") {
    $result.mode = "artefact"
    $result.broker_id = $BrokerId
    $f = Join-Path (Join-Path $verDir $BrokerId) "servers.dat"
    if (-not (Test-Path -LiteralPath $f)) {
      $result.present = $false; $result.verified_sha256 = ""; $result.size_bytes = 0
      $result.ok = $false; $result.reason = "artefact_missing"; Emit; exit 0
    }
    $sha = (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLower()
    $result.present = $true; $result.verified_sha256 = $sha; $result.size_bytes = (Get-Item -LiteralPath $f).Length
    if ($ExpectedSha256 -ne "") {
      $result.ok = ($sha -eq $ExpectedSha256.ToLower())
      $result.reason = $(if ($result.ok) { "ok" } else { "sha_mismatch" })
    } else {
      $result.ok = $true; $result.reason = "ok"
    }
    Emit; exit 0
  }

  # Inventory mode: enumerate every broker subdir's servers.dat under versions\<Label>.
  $result.mode = "inventory"
  $brokers = @()
  if (Test-Path -LiteralPath $verDir) {
    foreach ($d in (Get-ChildItem -LiteralPath $verDir -Directory -Force -ErrorAction SilentlyContinue)) {
      $f = Join-Path $d.FullName "servers.dat"
      $entry = [ordered]@{ broker_id = $d.Name; present = $false; sha256 = ""; size_bytes = 0 }
      if (Test-Path -LiteralPath $f) {
        $entry.present = $true
        $entry.sha256 = (Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash.ToLower()
        $entry.size_bytes = (Get-Item -LiteralPath $f).Length
      }
      $brokers += $entry
    }
  }
  $result.brokers = $brokers
  $result.ok = $true; $result.reason = "ok"
  Emit; exit 0
}
catch { Fail "attest_exception" }

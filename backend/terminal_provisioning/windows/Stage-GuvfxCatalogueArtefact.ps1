<#
  Stage-GuvfxCatalogueArtefact.ps1  (Broker Catalogue V3 governed staging)

  Writes ONE approved, immutable catalogue servers.dat into the shared catalogue version tree
  (C:\GuvFX\catalogue\versions\<Label>\<BrokerId>\servers.dat) and read-back-verifies its SHA-256.
  It is the host half of broker_catalogue.host_catalogue staging; the backend has already resolved that these
  bytes are APPROVED for this exact SHA. It is ACCOUNT-INDEPENDENT (no account, no slot) and re-asserts every
  safety property host-side:

    * The approved BYTES arrive as base64 on STDIN - never argv, never a log, never a caller path.
    * Destination is DERIVED (never supplied): versions\<Label>\<BrokerId>\servers.dat, confined strictly under
      C:\GuvFX\catalogue\versions. Traversal, path escape and reparse-point/symlink components are REFUSED.
    * The decoded bytes must match -SizeBytes AND -ExpectedSha256 before anything is written.
    * Write is atomic: bytes -> a temp file in the SAME directory -> hash read-back -> atomic move into place.
    * Idempotent ONLY on exact match: if servers.dat already exists it is overwritten ONLY when its bytes are
      byte-identical (same SHA); a different existing file is REFUSED (overwrite_mismatch), never clobbered.
    * It NEVER touches the golden image, any account workspace, accounts.dat, a terminal, a bridge or a task.
    * FAIL-CLOSED: ok:true ONLY when the on-disk file's SHA-256 equals -ExpectedSha256.

  ASCII-only (RULE 9). Windows PowerShell 5.1 safe.
#>
param(
  [Parameter(Mandatory = $true)][string]$Label,
  [Parameter(Mandatory = $true)][string]$BrokerId,
  [Parameter(Mandatory = $true)][string]$ExpectedSha256,
  [Parameter(Mandatory = $true)][int]$SizeBytes
)
$ErrorActionPreference = "Stop"
$CATALOGUE_BASE = "C:\GuvFX\catalogue"
$MAX_BYTES = 2000000
$result = [ordered]@{ label = $Label; broker_id = $BrokerId; staged_sha256 = ""; size_bytes = 0;
  idempotent = $false; path = ""; ok = $false; reason = "" }
function Emit() { $result | ConvertTo-Json -Compress }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

try {
  if ($Label -notmatch '^[A-Za-z0-9._\-]{1,32}$') { Fail "refusing_label_malformed" }
  if ($BrokerId -notmatch '^[a-z0-9_]{1,32}$') { Fail "refusing_broker_malformed" }
  if ($ExpectedSha256 -notmatch '^[0-9a-f]{64}$') { Fail "refusing_sha_malformed" }
  if ($SizeBytes -le 0 -or $SizeBytes -gt $MAX_BYTES) { Fail "refusing_size_out_of_range" }

  # Read the approved bytes as base64 from STDIN (piped by the runner; never argv).
  $b64 = [Console]::In.ReadToEnd()
  if ($null -eq $b64) { Fail "stdin_empty" }
  $b64 = $b64.Trim()
  if ($b64.Length -eq 0) { Fail "stdin_empty" }
  try { $bytes = [Convert]::FromBase64String($b64) } catch { Fail "payload_base64_invalid" }
  if ($bytes.Length -ne $SizeBytes) { Fail "payload_size_mismatch" }
  $sha = [System.BitConverter]::ToString(
    [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace("-", "").ToLower()
  if ($sha -ne $ExpectedSha256.ToLower()) { Fail "payload_sha_mismatch" }

  # DEST confinement: versions\<Label>\<BrokerId>\servers.dat under C:\GuvFX\catalogue\versions (no traversal).
  if ($Label -match '\.\.' -or $BrokerId -match '\.\.') { Fail "refusing_path_traversal" }
  $verBase = [System.IO.Path]::GetFullPath((Join-Path $CATALOGUE_BASE "versions"))
  $destDir = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $verBase $Label) $BrokerId))
  if (-not $destDir.ToLower().StartsWith(($verBase.TrimEnd('\') + "\").ToLower())) { Fail "refusing_dest_outside_catalogue" }
  $dest = Join-Path $destDir "servers.dat"

  # Refuse any reparse point / symlink in the destination chain (escape via a junction).
  foreach ($p in @($verBase, (Join-Path $verBase $Label), $destDir, $dest)) {
    if (Test-Path -LiteralPath $p) {
      $it = Get-Item -LiteralPath $p -Force
      if ($it.Attributes.ToString() -match 'ReparsePoint') { Fail "refusing_reparse_point" }
    }
  }

  # Idempotent-exact-match: an existing servers.dat is overwritten ONLY if byte-identical; otherwise refuse.
  if (Test-Path -LiteralPath $dest) {
    $existing = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash.ToLower()
    if ($existing -eq $ExpectedSha256.ToLower()) {
      $result.idempotent = $true; $result.staged_sha256 = $existing
      $result.size_bytes = (Get-Item -LiteralPath $dest).Length
      $result.path = "versions\" + $Label + "\" + $BrokerId + "\servers.dat"
      $result.ok = $true; $result.reason = "ok_idempotent"; Emit; exit 0
    }
    Fail "overwrite_mismatch"
  }

  if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Force -Path $destDir | Out-Null }
  $tmp = Join-Path $destDir ("servers.dat.staging-" + [System.Guid]::NewGuid().ToString("N"))
  try {
    [System.IO.File]::WriteAllBytes($tmp, $bytes)
    $tmpSha = (Get-FileHash -LiteralPath $tmp -Algorithm SHA256).Hash.ToLower()
    if ($tmpSha -ne $ExpectedSha256.ToLower()) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue; Fail "temp_sha_mismatch" }
    [System.IO.File]::Move($tmp, $dest)   # atomic rename on the same volume
  } catch {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    Fail "stage_write_failed"
  }

  $destSha = (Get-FileHash -LiteralPath $dest -Algorithm SHA256).Hash.ToLower()
  $result.staged_sha256 = $destSha
  $result.size_bytes = (Get-Item -LiteralPath $dest).Length
  if ($destSha -ne $ExpectedSha256.ToLower()) { Fail "readback_sha_mismatch" }
  if ($result.size_bytes -ne $SizeBytes) { Fail "readback_size_mismatch" }
  $result.path = "versions\" + $Label + "\" + $BrokerId + "\servers.dat"
  $result.ok = $true; $result.reason = "ok"
  Emit
  exit 0
}
catch { Fail "stage_exception" }

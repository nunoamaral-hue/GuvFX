<#
  Verify-GuvfxRuntimeBuild.ps1  (P0 golden-drift gate - READ-ONLY)

  Reads the materialised runtime's terminal64.exe ProductVersion and the pinned golden manifest build, and
  reports whether they match. It mutates NOTHING: no launch, no login, no file/registry change. Used by
  prepare_hosted_slot to FAIL CLOSED (PREP_GOLDEN_DRIFT) when the copied runtime build != the certified manifest
  (the exact defect behind the customer-visible MT5 update/UAC path: a 5833 runtime under a 6036 manifest).

  The manifest is the single source of truth for the certified build (``.guvfx_golden_manifest`` - first non-empty,
  non-comment line is the pinned build, e.g. 5.0.0.6036). The runtime build is the ACTUAL executable version, so
  the check can never be fooled by stale metadata. ASCII-only (RULE 9). Emits one compact JSON object.
#>
param(
  [Parameter(Mandatory = $true)][string]$TerminalRoot
)
$ErrorActionPreference = "Stop"
# Certified-build pin. Preferred: the manifest copied INTO the runtime (self-describing). Falls back to the
# active golden source. NOTE (2026-08-24): the live golden is drifted (runtimes 5833; a staged newMT5 golden is
# 6073; some manifests 6036) -- the gate stays DARK until the golden + manifest are reconciled to one approved
# build, so this ordering only needs to be deterministic, not to resolve the drift.
$GOLDEN_MANIFEST_CANDIDATES = @(
  (Join-Path $TerminalRoot ".guvfx_golden_manifest"),   # self-describing runtime (preferred once materialise copies it)
  "C:\GuvFX\golden\newMT5\.guvfx_golden_manifest",
  "C:\GuvFX\golden\.guvfx_golden_manifest"
)

$result = [ordered]@{
  ok = $false; runtime_build = ""; manifest_build = ""; build_matches_manifest = $false; reason = ""
}
function Emit() { $result | ConvertTo-Json -Compress }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

try {
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "refusing_path_traversal" }
  $exe = Join-Path $full "terminal64.exe"
  if (-not (Test-Path -LiteralPath $exe)) { Fail "terminal64_missing" }
  $result.runtime_build = [string](Get-Item -LiteralPath $exe).VersionInfo.ProductVersion

  # Pinned manifest build: first non-empty, non-comment line.
  $manifestPath = $null
  foreach ($c in $GOLDEN_MANIFEST_CANDIDATES) { if (Test-Path -LiteralPath $c) { $manifestPath = $c; break } }
  if (-not $manifestPath) { Fail "golden_manifest_missing" }
  $pinned = $null
  foreach ($line in (Get-Content -LiteralPath $manifestPath -ErrorAction Stop)) {
    $t = $line.Trim()
    if ($t -ne "" -and -not $t.StartsWith("#")) { $pinned = $t; break }
  }
  if ([string]::IsNullOrWhiteSpace($pinned)) { Fail "golden_manifest_empty" }
  $result.manifest_build = $pinned

  $result.build_matches_manifest = ($result.runtime_build -eq $result.manifest_build)
  $result.ok = $true
  $result.reason = if ($result.build_matches_manifest) { "ok" } else { "golden_build_drift" }
  Emit
  exit 0
}
catch {
  Fail "verify_runtime_build_exception"
}

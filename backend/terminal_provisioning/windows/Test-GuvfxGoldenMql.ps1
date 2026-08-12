<#
  STREAM 10D golden-image MQL5 code-dir inspection gate (ADR-0043 Decision 5). Deterministic, read-only.

  The W^X model relies on "no importing program can ever be present in a runnable code dir". This gate proves it
  for a golden (or a materialised slot) BEFORE it is promoted / locked: the MQL5 code directories
  (Experts / Indicators / Scripts / Services / Libraries) must ship VETTED-EMPTY of executable/source artefacts,
  and NO source may contain a `#import` of a non-approved library. Fails closed (exit 1) on any finding, so a
  golden that could seed a native-import EA aborts BEFORE promotion.

  Approved MQL native imports: NONE for hosted-beta (empty allowlist). A future developer tier would supply its
  own separately-certified allowlist; do not widen here.

  ASCII-ONLY (RULE 9); ParseFile()-validate before first execution. Read-only: never mutates the tree.

  Usage:
    powershell -NoProfile -File Test-GuvfxGoldenMql.ps1 -RuntimeRoot 'C:\GuvFX\golden' `
        [-CodeSubdirs '["terminal\\MQL5\\Experts",...]']
  Emits: { ok, root, offenders:[{path,reason}], scanned, reason }.
#>
param(
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [string]$CodeSubdirs = '["terminal\\MQL5\\Experts","terminal\\MQL5\\Indicators","terminal\\MQL5\\Scripts","terminal\\MQL5\\Services","terminal\\MQL5\\Libraries","terminal\\MQL5\\Include"]'
)
$ErrorActionPreference = "Stop"
# Compiled/executable + source artefacts that must NOT be present in a vetted-empty code dir.
$ARTEFACT_EXT = @(".ex5", ".ex4", ".exe", ".dll")
$SOURCE_EXT   = @(".mq5", ".mq4", ".mqh")
$APPROVED_IMPORTS = @()   # hosted-beta: none

try {
  $root = ($RuntimeRoot -replace "/","\").TrimEnd("\")
  if ($root -match "\.\.") { throw "path_traversal" }
  if (-not (Test-Path -LiteralPath $root)) { throw "root_absent" }
  $subs = @($CodeSubdirs | ConvertFrom-Json)
  $offenders = @()
  $scanned = 0
  foreach ($rel in $subs) {
    $dir = Join-Path $root $rel
    if (-not (Test-Path -LiteralPath $dir)) { continue }
    foreach ($f in Get-ChildItem -LiteralPath $dir -Recurse -File -ErrorAction SilentlyContinue) {
      $scanned += 1
      $ext = $f.Extension.ToLower()
      if ($ARTEFACT_EXT -contains $ext) {
        $offenders += [ordered]@{ path=$f.FullName; reason="unapproved_compiled_artefact" }
        continue
      }
      if ($SOURCE_EXT -contains $ext) {
        # A source file at all is unexpected in a vetted-empty code dir; a #import makes it a native-code seed.
        $reason = "unapproved_source"
        try {
          foreach ($line in [System.IO.File]::ReadAllLines($f.FullName)) {
            $m = [regex]::Match($line, '(?i)^\s*#import\s+"([^"]+)"')
            if ($m.Success) {
              $lib = $m.Groups[1].Value
              if ($APPROVED_IMPORTS -notcontains $lib) { $reason = "hash_import_of_unapproved_library:$lib"; break }
            }
          }
        } catch { $reason = "unapproved_source_unreadable" }
        $offenders += [ordered]@{ path=$f.FullName; reason=$reason }
      }
    }
  }
  $ok = ($offenders.Count -eq 0)
  [ordered]@{ ok=$ok; root=$root; offenders=$offenders; scanned=$scanned;
              reason=($(if ($ok) { "vetted_empty" } else { "golden_code_dirs_not_vetted_empty" })) } |
    ConvertTo-Json -Compress -Depth 6
  if (-not $ok) { exit 1 }
}
catch {
  [ordered]@{ ok=$false; root=$RuntimeRoot; offenders=@(); reason=("$($_.Exception.Message)") } | ConvertTo-Json -Compress -Depth 6
  exit 1
}

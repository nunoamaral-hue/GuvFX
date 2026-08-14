<#
  STREAM 10D golden-image MQL5 code-dir inspection gate (ADR-0043 Decision 5). Deterministic, fail-closed.

  The W^X model relies on "no importing program can ever be present in a runnable code dir". This gate proves it
  for a golden (or a materialised slot) BEFORE promotion / lock: the MQL5 code directories (Experts / Indicators /
  Scripts / Services / Libraries / Include) must ship VETTED-EMPTY of executable/source artefacts, and NO source
  may contain a `#import` of a non-approved library.

  RULE 11 (this gate proves a NEGATIVE, so it never trusts a clean result blindly):
    (a) POSITIVE CONTROL FIRST - it seeds a known `#import` source + a stray .ex5 into a throwaway temp dir and
        requires the SAME detector to flag BOTH; if the detector does not, it aborts (`positive_control_failed`).
        So "vetted_empty" is only ever emitted after the detector has been shown capable of a known positive.
    (b) COVERAGE - EVERY expected code subdir must EXIST; a missing one is an OFFENDER (`expected_code_dir_absent`),
        never a silent skip. (A wrong root / differently-laid golden therefore FAILS instead of passing clean.)
        A code dir that exists but is empty is the EXPECTED vetted state and legitimately scans zero files.
    (c) NO FAIL-OPEN ENUMERATION - Get-ChildItem errors are captured (`-ErrorVariable`) and recorded as offenders
        (`code_dir_unscannable`), never swallowed; a code dir that is a reparse point is rejected outright.

  Approved MQL native imports: NONE for hosted-beta (empty allowlist). A future developer tier would supply its
  own separately-certified allowlist; do not widen here.

  ASCII-ONLY (RULE 9); ParseFile()-validate before first execution. Read-only on the golden tree (the positive
  control writes ONLY under a fresh %TEMP% dir, never under -RuntimeRoot).

  Usage:
    powershell -NoProfile -File Test-GuvfxGoldenMql.ps1 -RuntimeRoot 'C:\GuvFX\golden' `
        [-CodeSubdirs '["terminal\\MQL5\\Experts",...]']
  Emits: { ok, root, offenders:[{path,reason}], scanned, positive_control, reason }.
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

# The SINGLE detector: classify one file -> $null (clean) or a reason string. Used by BOTH the real scan and the
# positive control, so a clean scan is only trusted after this exact code has flagged a known positive (RULE 11).
function Classify-File([string]$path, [string]$ext) {
  if ($ARTEFACT_EXT -contains $ext) { return "unapproved_compiled_artefact" }
  if ($SOURCE_EXT -contains $ext) {
    # A source file at all is unexpected in a vetted-empty code dir; a #import makes it a native-code seed.
    $reason = "unapproved_source"
    try {
      foreach ($line in [System.IO.File]::ReadAllLines($path)) {
        $m = [regex]::Match($line, '(?i)^\s*#import\s+"([^"]+)"')
        if ($m.Success) {
          $lib = $m.Groups[1].Value
          if ($APPROVED_IMPORTS -notcontains $lib) { return "hash_import_of_unapproved_library:$lib" }
        }
      }
    } catch { return "unapproved_source_unreadable" }
    return $reason
  }
  return $null
}

# Scan one code dir; fail CLOSED. A reparse-point dir is rejected (a junction could redirect recursion outside the
# golden). Enumeration errors are CAPTURED and recorded as offenders (never swallowed). Returns
# @{ scanned=<int>; offenders=@(...) }.
function Scan-Dir([string]$dir) {
  $off = @(); $scanned = 0
  $di = Get-Item -LiteralPath $dir -Force
  if ($di.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
    return @{ scanned = 0; offenders = @([ordered]@{ path = $dir; reason = "code_dir_is_reparse_point" }) }
  }
  $ev = $null
  # -ErrorAction SilentlyContinue overrides the global Stop for THIS call so a per-item enumeration failure does not
  # abort; -ErrorVariable captures every such failure so it becomes an offender (fail closed), not a silent skip.
  $files = @(Get-ChildItem -LiteralPath $dir -Recurse -File -Force -ErrorAction SilentlyContinue -ErrorVariable ev)
  foreach ($e in $ev) {
    $tgt = if ($e.TargetObject) { "$($e.TargetObject)" } else { $dir }
    $off += [ordered]@{ path = $tgt; reason = "code_dir_unscannable" }
  }
  foreach ($f in $files) {
    $scanned += 1
    $r = Classify-File $f.FullName $f.Extension.ToLower()
    if ($r) { $off += [ordered]@{ path = $f.FullName; reason = $r } }
  }
  return @{ scanned = $scanned; offenders = $off }
}

try {
  $root = ($RuntimeRoot -replace "/", "\").TrimEnd("\")
  if ($root -match "\.\.") { throw "path_traversal" }
  if (-not (Test-Path -LiteralPath $root)) { throw "root_absent" }
  $subs = @($CodeSubdirs | ConvertFrom-Json)
  if ($subs.Count -eq 0) { throw "no_code_subdirs" }

  # RULE 11 POSITIVE CONTROL first: prove the detector flags a known #import source AND a stray .ex5 before any
  # clean result is trusted. Runs in a fresh temp dir; NEVER writes under the golden tree.
  $pcDir = Join-Path ([System.IO.Path]::GetTempPath()) ("guvfx-golden-poscontrol-" + [Guid]::NewGuid().ToString("N"))
  New-Item -ItemType Directory -Path $pcDir -Force | Out-Null
  try {
    $badSrc = Join-Path $pcDir "poscontrol.mq5"
    "#import ""kernel32.dll""" | Out-File -FilePath $badSrc -Encoding ASCII
    $badArt = Join-Path $pcDir "poscontrol.ex5"
    "x" | Out-File -FilePath $badArt -Encoding ASCII
    $pc = Scan-Dir $pcDir
    $srcFlagged = @($pc.offenders | Where-Object { $_.path -eq $badSrc -and $_.reason -like "hash_import_of_unapproved_library:*" }).Count -ge 1
    $artFlagged = @($pc.offenders | Where-Object { $_.path -eq $badArt -and $_.reason -eq "unapproved_compiled_artefact" }).Count -ge 1
    if (-not ($srcFlagged -and $artFlagged)) { throw "positive_control_failed" }
  }
  finally {
    Remove-Item -LiteralPath $pcDir -Recurse -Force -ErrorAction SilentlyContinue
  }

  # COVERAGE + scan: every expected code dir must EXIST (missing = offender, fail closed) and be enumerable.
  $offenders = @()
  $scanned = 0
  foreach ($rel in $subs) {
    $dir = Join-Path $root $rel
    if (-not (Test-Path -LiteralPath $dir)) {
      $offenders += [ordered]@{ path = $dir; reason = "expected_code_dir_absent" }
      continue
    }
    $r = Scan-Dir $dir
    $scanned += $r.scanned
    $offenders += $r.offenders
  }

  $ok = ($offenders.Count -eq 0)
  [ordered]@{ ok = $ok; root = $root; offenders = $offenders; scanned = $scanned; positive_control = "passed";
              reason = ($(if ($ok) { "vetted_empty" } else { "golden_code_dirs_not_vetted_empty" })) } |
    ConvertTo-Json -Compress -Depth 6
  if (-not $ok) { exit 1 }
}
catch {
  [ordered]@{ ok = $false; root = $RuntimeRoot; offenders = @(); positive_control = "not_reached";
              reason = ("$($_.Exception.Message)") } | ConvertTo-Json -Compress -Depth 6
  exit 1
}

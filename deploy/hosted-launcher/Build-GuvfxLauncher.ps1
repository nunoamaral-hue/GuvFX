<#
  Build-GuvfxLauncher.ps1 -- reproducible GUI-subsystem (windowless) build of the native single-instance MT5
  launch guard (GuvfxLaunch.cs -> guvfx_launch.exe).

  WHY: the RemoteApp start-program must be a GUI-subsystem exe so Windows allocates NO console. A console-subsystem
  build makes the member see a black console window ("LAUNCH-VERDICT ...") in front of MT5. This script compiles
  /target:winexe and REFUSES to emit a binary that is not GUI subsystem, so a console launcher can never be
  produced silently. Launch diagnostics go to the Windows Event Log (built into the source), not stdout, so losing
  stdout under the GUI subsystem loses no security signal -- the process exit code stays the machine contract.

  RULE 9: ASCII-only; ParseFile()-validate before first execution.
  RULE 11: the PE-subsystem parser is proven against a KNOWN POSITIVE (explorer.exe = GUI/2) and a KNOWN NEGATIVE
  (cmd.exe = CUI/3) before its verdict on the built binary is trusted.

  It does NOT touch the live launcher: it writes ONLY the built exe under -OutDir (default: a fresh temp build
  dir) and prints, as one compact JSON object, the file SHA256 (for C:\GuvFX\launcher\.guvfx_launcher_manifest)
  and the AppLocker hash (for the FileHashRule). Installation to C:\GuvFX\launcher + manifest/AppLocker re-pin +
  ACL re-assert is a separate, reviewed, rollback-capturing host step -- deliberately NOT done here.

  Usage:
    powershell -NoProfile -ExecutionPolicy Bypass -File Build-GuvfxLauncher.ps1 `
      -Source .\GuvfxLaunch.cs -OutDir C:\Windows\Temp\guvfx_launch_build
#>
param(
  [string]$Source = (Join-Path $PSScriptRoot "GuvfxLaunch.cs"),
  [string]$OutDir = (Join-Path $env:TEMP ("guvfx_launch_build_" + [Guid]::NewGuid().ToString("N")))
)
$ErrorActionPreference = "Stop"
$GUI = 2; $CUI = 3
$result = [ordered]@{
  ok = $false; exe = ""; subsystem = ""; subsystem_is_gui = $false; sha256 = ""; applocker_hash = ""; reason = ""
}
function Emit() { $result | ConvertTo-Json -Compress }
function Fail([string]$why) { $result.reason = $why; Emit; exit 1 }

# PE Optional-Header Subsystem field from raw bytes: 2 = WINDOWS_GUI, 3 = WINDOWS_CUI. Offset = e_lfanew(0x3C) ->
# PE sig(4) + COFF(20) + Optional-Header offset 68; identical for PE32 and PE32+. Returns -1 when not a PE.
function Get-PESubsystem([string]$path) {
  try {
    $b = [System.IO.File]::ReadAllBytes($path)
    $peOff = [BitConverter]::ToInt32($b, 0x3C)
    if ($b[$peOff] -ne 0x50 -or $b[$peOff + 1] -ne 0x45) { return -1 }   # 'P','E'
    return [int][BitConverter]::ToUInt16($b, $peOff + 24 + 68)
  } catch { return -1 }
}

try {
  if (-not (Test-Path -LiteralPath $Source)) { Fail "source_missing" }

  # RULE 11 -- prove the subsystem parser on known controls before its verdict on the built binary is trusted.
  if ((Get-PESubsystem (Join-Path $env:SystemRoot "explorer.exe")) -ne $GUI) { Fail "parser_positive_control_failed" }
  if ((Get-PESubsystem (Join-Path $env:SystemRoot "System32\cmd.exe")) -ne $CUI) { Fail "parser_negative_control_failed" }

  $csc = Join-Path ([System.Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()) "csc.exe"
  if (-not (Test-Path -LiteralPath $csc)) { Fail "csc_not_found" }

  if (-not (Test-Path -LiteralPath $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }
  $exe = Join-Path $OutDir "guvfx_launch.exe"
  if (Test-Path -LiteralPath $exe) { Remove-Item -LiteralPath $exe -Force }

  # GUI subsystem (/target:winexe) -> Windows allocates no console. Matches the GuvfxLaunch.cs / README header.
  & $csc /nologo /optimize /platform:x64 /target:winexe ("/out:" + $exe) $Source | Out-Null
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $exe)) { Fail "compile_failed" }

  $result.exe = $exe
  $sub = Get-PESubsystem $exe
  $result.subsystem = "$sub"
  $result.subsystem_is_gui = ($sub -eq $GUI)
  # NEVER emit a console launcher: fail closed if the built binary is not GUI subsystem.
  if (-not $result.subsystem_is_gui) { Fail "built_binary_not_gui_subsystem" }

  $result.sha256 = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToUpper()
  try {
    $li = Get-AppLockerFileInformation -Path $exe
    if ($li -and $li.Hash) { $result.applocker_hash = ("$($li.Hash)").ToUpper() }
  } catch { $result.applocker_hash = "" }

  $result.ok = $true
  $result.reason = "ok"
  Emit
  exit 0
}
catch { Fail "build_exception" }

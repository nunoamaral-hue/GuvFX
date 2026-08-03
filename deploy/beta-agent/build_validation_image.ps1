# ADR-0027 Phase 2: GOVERNED build-5833 validation-image builder (fail-closed, ASCII-only per RULE 9).
#
# Builds the dedicated broker-login validation terminal from the operator's PROVEN IS6 build-5833 program
# files: an EXPLICIT four-file allow-list, hash-verified against the pinned certified source, copied into a
# fresh isolated destination, run-in credential-free to generate the standard MQL5 .ex5 layer, then verified
# clean (no accounts.dat / logs / account state) by the Python governance module 'validation_image.py'.
#
# It NEVER copies accounts.dat, logs, %APPDATA%, account profiles, credentials, history or an attached EA. The
# run-in layer is generated FROM THE ISOLATED IMAGE ITSELF, never copied from a production account environment.
#
# Usage (Phase 2B, on the host):
#   powershell -NoProfile -ExecutionPolicy Bypass -File build_validation_image.ps1 `
#       -SourceDir 'C:\Program Files\IS6 Technologies MT5 Terminal' `
#       -DestRoot  'C:\GuvFX\beta\validation-5833' `
#       -PythonExe 'C:\GuvFX\beta\agent-venv\Scripts\python.exe' `
#       -AgentDir  'C:\GuvFX\beta\agent'
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [Parameter(Mandatory = $true)][string]$DestRoot,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$AgentDir,
    [int]$RunInTimeoutSec = 200,
    [int]$ExpectedEx5 = 131
)
$ErrorActionPreference = 'Stop'

# Pinned SHA-256 of the certified build-5833 source allow-list (MUST match validation_image.SOURCE_HASHES).
$SOURCE = @{
    'terminal64.exe'      = 'd84fc3d891f66c1d6325c3c2d2ffc6974de1928cfb6086d1bbd11d4e1dd07d20'
    'MetaEditor64.exe'    = '64b7335854310bf2f0f84f5e51e12ee28f047de9eefd9f8a70005624f9d1df90'
    'Config\servers.dat'  = '16600f67e3c49d38e0bba29d554b0c6ae5af907f34125ef5b2e3fa3c08fb0ed1'
    'Config\terminal.lic' = '19a721d3cf93be782e6188ee5c37d52268ad92a7cf90b237c0aa152ec59359e7'
}

function Fail([string]$why) { Write-Output ("BUILD_5833_FAIL: " + $why); exit 1 }

function Assert-SafePath([string]$p) {
    if ([string]::IsNullOrWhiteSpace($p)) { Fail "empty path" }
    if ($p -match '\.\.') { Fail ("traversal in path: " + $p) }
    $full = [System.IO.Path]::GetFullPath($p)
    if ($full.TrimEnd('\').Length -le 3) { Fail ("bare-drive root refused: " + $full) }   # e.g. C:\
    return $full
}

$src  = Assert-SafePath $SourceDir
$dest = Assert-SafePath $DestRoot
$destTerm = Join-Path $dest 'terminal'
if (-not (Test-Path $src)) { Fail ("source not found: " + $src) }
# refuse to build ON TOP OF a live/forbidden root
foreach ($bad in @('C:\GuvFX\beta\slots', 'C:\GuvFX\golden', 'C:\GuvFX\beta\golden', 'C:\GuvFX\beta\accounts',
                   'C:\GuvFX\beta\validation\terminal')) {
    if ($destTerm.ToLower().StartsWith($bad.ToLower())) { Fail ("dest overlaps a forbidden root: " + $bad) }
}

# 1) verify source hashes BEFORE copying (drift => refuse; we build only from the certified build 5833)
foreach ($rel in $SOURCE.Keys) {
    $sp = Join-Path $src $rel
    if (-not (Test-Path $sp)) { Fail ("source allow-listed file missing: " + $rel) }
    $h = (Get-FileHash $sp -Algorithm SHA256).Hash.ToLower()
    if ($h -ne $SOURCE[$rel]) { Fail ("source hash drift on " + $rel) }
}
Write-Output "SOURCE_HASHES_VERIFIED"

# 2) fresh destination + copy ONLY the allow-list (no Bases/Profiles/logs/accounts.dat/%APPDATA%)
if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $destTerm 'config') -Force | Out-Null
foreach ($rel in $SOURCE.Keys) {
    $target = Join-Path $destTerm $rel
    New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
    Copy-Item (Join-Path $src $rel) $target -Force
}
Write-Output ("ALLOW_LIST_COPIED files=" + @(Get-ChildItem $destTerm -Recurse -File).Count)

# 3) least-privilege ACL: SYSTEM + Administrators full (the runner identity is a local admin). Inheritance off.
& icacls $destTerm /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

# 4) run-in credential-free: launch /portable, dismiss ONLY the account-free "Open an Account" wizard (WM_CLOSE),
#    let the standard MQL5 library extract + compile. NO login/password/server is supplied.
$exe = Join-Path $destTerm 'terminal64.exe'
Add-Type @"
using System;using System.Runtime.InteropServices;using System.Text;
public class W32 {
 public delegate bool EnumProc(IntPtr h, IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc c, IntPtr l);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
 [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
}
"@
$proc = Start-Process -FilePath $exe -ArgumentList '/portable' -PassThru
$termPid = $proc.Id
$closed = $false
for ($i = 0; $i -lt ($RunInTimeoutSec * 2); $i++) {
    Start-Sleep -Milliseconds 500
    if (-not $closed) {
        $script:hit = @()
        $cb = [W32+EnumProc] { param($h, $l) $u = 0; [void][W32]::GetWindowThreadProcessId($h, [ref]$u);
            if ($u -eq $termPid -and [W32]::IsWindowVisible($h)) { $c = New-Object Text.StringBuilder 256;
            [void][W32]::GetClassName($h, $c, 256); if ($c.ToString() -eq '#32770') { $script:hit += $h } } return $true }
        [void][W32]::EnumWindows($cb, [IntPtr]::Zero)
        foreach ($h in $script:hit) { [void][W32]::PostMessage($h, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero); $closed = $true }
    }
    $ex5 = @(Get-ChildItem $destTerm -Recurse -Filter *.ex5 -EA SilentlyContinue).Count
    if ($ex5 -ge $ExpectedEx5) { break }
}

# 5) terminate ONLY the path-verified control process, then scrub logs + any accounts.dat the run-in wrote
Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" |
    Where-Object { $_.ExecutablePath -eq $exe } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }
Start-Sleep -Seconds 2
Remove-Item (Join-Path $destTerm 'logs') -Recurse -Force -EA SilentlyContinue
Remove-Item (Join-Path $destTerm 'Logs') -Recurse -Force -EA SilentlyContinue
Remove-Item (Join-Path $destTerm 'config\accounts.dat') -Force -EA SilentlyContinue
Remove-Item (Join-Path $destTerm 'Config\accounts.dat') -Force -EA SilentlyContinue

# 6) FINAL fail-closed verification via the Python governance module (single source of truth)
$ex5 = @(Get-ChildItem $destTerm -Recurse -Filter *.ex5 -EA SilentlyContinue).Count
Write-Output ("RUN_IN_EX5=" + $ex5)
if (-not (Test-Path $PythonExe)) { Fail ("python not found: " + $PythonExe) }
$py = @"
import sys, json
sys.path.insert(0, r'$AgentDir')
import validation_image as vi
try:
    rep = vi.verify_image(r'$destTerm')
    print('IMAGE_OK ' + json.dumps({k: rep[k] for k in ('terminal_build','ex5_count','account_artefact_count','attached_ea_count','structural_fingerprint')}))
except vi.ValidationImageError as e:
    print('IMAGE_FAIL ' + str(e.reason)); sys.exit(2)
"@
$tmp = Join-Path $env:TEMP ('gvfx_vi_' + [System.IO.Path]::GetRandomFileName() + '.py')
Set-Content -Path $tmp -Value $py -Encoding ASCII
try { $out = & $PythonExe $tmp 2>&1 } finally { Remove-Item $tmp -Force -EA SilentlyContinue }
Write-Output $out
if ($LASTEXITCODE -ne 0 -or ($out -join ' ') -notmatch 'IMAGE_OK') { Fail "governed image verification failed" }
Write-Output "BUILD_5833_VALIDATION_IMAGE_BUILT"

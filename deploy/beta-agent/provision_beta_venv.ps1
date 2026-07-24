# B3P - provision the DEDICATED beta-agent Python venv.
# DARK ARTEFACT: run ONLY on the host, as Administrator, when authorised.
#
# WHY A VENV. The beta service needs pywin32, and the ONLY suitable interpreter on the host
# (C:\Program Files\Python311, 3.11.9) is the one the LIVE production bridge runs on - installing packages
# into it is production mutation. `python -m venv` copies/redirects; it does NOT modify the base
# interpreter, and pip then writes ONLY inside the venv. The base is left byte-for-byte untouched.
#
# NEVER uses C:\GuvFX\python311.exe - that path is the Python INSTALLER (OriginalFilename
# python-3.11.9-amd64.exe), and executing it launches an installer.
#
# Idempotent, and read-only by default. Pass -Apply to create the venv and install pywin32.
param(
  [string]$BaseInterpreter = "C:\Program Files\Python311\python.exe",
  [string]$VenvPath        = "C:\GuvFX\beta\agent-venv",
  [switch]$Apply
)
$ErrorActionPreference = "Stop"
$py = Join-Path $VenvPath "Scripts\python.exe"

# The base MUST be a real interpreter, proven from PE metadata BEFORE it is executed - the same discriminator
# install_service.ps1 uses. An installer reports OriginalFilename 'python-<ver>-amd64.exe'.
if (-not (Test-Path $BaseInterpreter)) { throw "base interpreter not found: $BaseInterpreter" }
$vi = (Get-Item $BaseInterpreter -Force).VersionInfo
$orig = [string]$vi.OriginalFilename
if ($orig -match '(?i)^python-.*\.exe$' -or $orig -match '(?i)\.msi$') {
  throw "refusing: base '$BaseInterpreter' is the Python INSTALLER ('$orig'), not an interpreter"
}
if ($orig -notmatch '(?i)^(python|pythonw|py|pyw)\.exe$') {
  throw "refusing: base '$BaseInterpreter' OriginalFilename '$orig' is not a CPython interpreter"
}
Write-Host "ok   base interpreter verified by metadata: '$orig' $($vi.FileVersion) (not executed)"

# Never provision on top of the live bridge's own interpreter unless it is explicitly the chosen base.
$bridgePids = @(Get-NetTCPConnection -LocalPort 8788 -State Listen -ErrorAction SilentlyContinue |
                Select-Object -Expand OwningProcess -Unique)
if ($bridgePids.Count -gt 0) {
  Write-Host "note  live bridge listening on :8788 (pid $($bridgePids -join ',')) - a venv does not touch it, but confirm the base is not being package-modified"
}

if (-not $Apply) {
  Write-Host "PLAN:  would create venv at $VenvPath from $BaseInterpreter, then pip install pywin32 (venv-scoped)."
  Write-Host "PLAN:  re-run with -Apply on the host. Nothing was changed."
  return
}

if (Test-Path $py) {
  Write-Host "note  venv already exists at $VenvPath; leaving as-is, verifying dependencies"
} else {
  Write-Host "creating venv (python -m venv; base interpreter untouched)..."
  & $BaseInterpreter -m venv $VenvPath
  if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
}
if (-not (Test-Path $py)) { throw "venv python missing after creation: $py" }

& $py -m pip install --upgrade pip | Out-Null
& $py -m pip install pywin32 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "pywin32 install failed (exit $LASTEXITCODE)" }

$post = Join-Path $VenvPath "Scripts\pywin32_postinstall.py"
if (Test-Path $post) { & $py $post -install | Out-Null }

# Verify: interpreter runs, pywin32 imports, service host present, base untouched.
$ver = & $py --version 2>&1
if ("$ver" -notmatch '(?i)^Python 3\.') { throw "venv python did not report Python 3 (got '$ver')" }
& $py -c "import win32serviceutil, win32service, win32event, servicemanager" 2>$null
if ($LASTEXITCODE -ne 0) { throw "pywin32 not importable in the venv" }
$host_exe = Join-Path $VenvPath "Lib\site-packages\win32\pythonservice.exe"
if (-not (Test-Path $host_exe)) { throw "pywin32 service host (pythonservice.exe) missing in the venv" }
if (Test-Path (Join-Path (Split-Path $BaseInterpreter) "Lib\site-packages\win32")) {
  throw "BASE interpreter now has win32 in site-packages - the base was package-modified; investigate"
}
Write-Host "ok   venv ready: $py, $ver, pywin32 importable, pythonservice.exe present, base interpreter untouched"
Write-Host "     set install_service.ps1 -Python to $py (this is already the default)."

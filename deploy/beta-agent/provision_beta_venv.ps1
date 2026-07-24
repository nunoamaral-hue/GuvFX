# B3P - provision the DEDICATED beta-agent Python venv.
# DARK ARTEFACT: run ONLY on the host, as Administrator, when authorised.
#
# WHY A VENV. The AGENT's slot-mutation code (win_slot_ops) needs pywin32 at runtime, and the ONLY suitable
# interpreter on the host (C:\Program Files\Python311, 3.11.9) is the one the LIVE production bridge runs on -
# installing packages into it is production mutation. `python -m venv` copies/redirects; it does NOT modify
# the base interpreter, and pip then writes ONLY inside the venv. The base is left byte-for-byte untouched.
# The service HOST is WinSW (see docs/B3P_SERVICE_HARNESS_COMPARISON.md), not pywin32 - so pywin32 is needed
# only by the agent, not for hosting.
#
# NO GLOBAL WRITES. This does NOT run `pywin32_postinstall -install`. That step (a) copies pywintypes311.dll
# + pythoncom311.dll into C:\Windows\System32 AND the base interpreter dir, and (b) does machine-wide COM
# registration - none of which a single venv-python child needs. pip-installed pywin32 ships a
# `pywin32_system32` folder and a bootstrap `.pth`, so `import win32security` resolves its DLLs FROM THE VENV.
# Running postinstall was the 05:38 System32 write in the 2026-07-24 incident; omitting it is the second half
# of "the beta runtime writes nothing global" (the first half is WinSW replacing pythonservice.exe).
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

# DELIBERATELY NOT RUN: `pywin32_postinstall -install`. It writes DLLs to System32 + the base interpreter and
# does machine-wide COM registration; the venv-python child needs none of that (see the header). The bootstrap
# .pth that pip drops resolves the DLLs from the venv's own pywin32_system32 folder.

# Verify: interpreter runs, the AGENT's pywin32 modules import FROM THE VENV, DLLs resolve locally, base untouched.
$ver = & $py --version 2>&1
if ("$ver" -notmatch '(?i)^Python 3\.') { throw "venv python did not report Python 3 (got '$ver')" }
# These are exactly the modules win_slot_ops imports lazily at runtime - NOT the SCM-host modules (WinSW hosts).
& $py -c "import win32security, win32ts, win32api, win32con, win32com.client, pywintypes" 2>$null
if ($LASTEXITCODE -ne 0) { throw "the agent's pywin32 modules are not importable in the venv" }
$boot = Join-Path $VenvPath "Lib\site-packages\pywin32_system32"
if (-not (Test-Path $boot)) { throw "venv is missing pywin32_system32 - DLLs cannot resolve without postinstall" }
if (Test-Path (Join-Path (Split-Path $BaseInterpreter) "Lib\site-packages\win32")) {
  throw "BASE interpreter now has win32 in site-packages - the base was package-modified; investigate"
}
Write-Host "ok   venv ready: $py, $ver, agent pywin32 modules import from the venv (pywin32_system32 present)"
Write-Host "     base interpreter untouched; pywin32_postinstall NOT run (no System32 / base-interpreter DLL writes)."
Write-Host "     NOTE (RULE 11): if postinstall was EVER run on this host, System32 already holds the DLLs, so the"
Write-Host "     import above cannot by itself prove local-only resolution - that proof requires a clean host."
Write-Host "     set install_service.ps1 -Python to $py (this is already the default)."

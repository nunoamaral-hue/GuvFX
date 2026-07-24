# CVM-Inc-3 B2/B3P-1 - install the beta provisioning agent as a real SCM-managed Windows service.
# DARK ARTEFACT: RUN ONLY in B3, on the host, as Administrator, AFTER merge. INSTALL-ONLY: it does NOT start
# the service, does NOT touch Session 3 / the prod terminal / the bridge / port 8788 / autologon / startup tasks.
# Dry-run by default; pass -Apply to perform the install. The first manual start waits for explicit approval.
#
# Verification fixes: B-5 (pywin32 service wrapper, not a raw python binPath that would 1053 at start),
# interpreter/account defaults corrected to the verified host facts, recovery-disabled verified via sc qfailure.
param(
  [string]$ServiceName = "GuvFXBetaAgent",
  [string]$AgentDir    = "C:\GuvFX\beta\agent",
  [string]$StateDir    = "C:\GuvFX\beta\agent-state",
  # The DEDICATED beta interpreter (workstream B), NOT C:\GuvFX\python311.exe (that path is the Python
  # INSTALLER - OriginalFilename python-3.11.9-amd64.exe - and executing it launches an installer) and NOT
  # C:\Program Files\Python311 (the interpreter the LIVE bridge runs on). A venv leaves both untouched.
  [string]$Python      = "C:\GuvFX\beta\agent-venv\Scripts\python.exe",
  [string]$RunAsUser   = "NT SERVICE\GuvFXBetaAgent",           # virtual service account: no password, stable SID
  # B3P-2 (install-only review F1): the pool model uses ...\beta\slots\<n>, NOT the legacy
  # ...\beta\accounts\<uuid> layout. The service account needs Modify on its own state dir and on the
  # tombstone root (it moves runtimes there); it needs only ReadAndExecute on its OWN code, and NOTHING on
  # the golden image or the slot directories - the slot IDENTITY owns those, not the agent.
  [string]$SlotsRoot   = "C:\GuvFX\beta\slots",
  [string]$BetaTombstones = "C:\GuvFX\beta\tombstones",
  [string]$GoldenDir   = "C:\GuvFX\golden\newMT5",
  [switch]$Apply
)
$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "==> $m" }
function DoIt($desc, [scriptblock]$block) {
  if ($Apply) { Step "APPLY: $desc"; & $block } else { Step "PLAN:  $desc" }
}

# 0. Preconditions (checked in both dry-run and apply)
if (-not (Test-Path (Join-Path $AgentDir "agent.py")))   { throw "agent.py not found under $AgentDir" }
if (-not (Test-Path (Join-Path $AgentDir "service.py"))) { throw "service.py not found under $AgentDir" }
# INTERPRETER IDENTITY IS VERIFIED BY METADATA BEFORE THE BINARY IS EVER EXECUTED. The previous preflight
# ran `& $Python -c "import ..."` unconditionally - in PLAN as well as APPLY - which, pointed at the Python
# INSTALLER (C:\GuvFX\python311.exe, OriginalFilename python-3.11.9-amd64.exe), launched an installer on the
# production host from a dry run. `Test-Path` cannot tell an interpreter from an installer and neither can an
# exit code (a detaching bootstrapper leaves $LASTEXITCODE $null). So identity is proven from PE metadata,
# statically, and the interpreter is EXECUTED ONLY UNDER -Apply.
function Test-GuvfxInterpreterIdentity {
  <# STATIC. Proves $Path is a CPython interpreter and NOT an installer, from PE metadata alone. Never
     executes the file. A CPython interpreter ships OriginalFilename 'python.exe'/'pythonw.exe'; the
     redistributable installer ships 'python-<ver>-amd64.exe'. That is the positive discriminator. #>
  param([Parameter(Mandatory)][string]$Path)
  if (-not (Test-Path $Path)) { throw "interpreter not found: $Path (run the beta-venv provisioning first)" }
  if ((Get-Item $Path -Force).PSIsContainer) { throw "interpreter path is a directory: $Path" }
  $vi = (Get-Item $Path -Force).VersionInfo
  $orig = [string]$vi.OriginalFilename
  $desc = [string]$vi.FileDescription
  if ($orig -match '(?i)^python-.*\.exe$' -or $orig -match '(?i)\.msi$') {
    throw "refusing: '$Path' is the Python INSTALLER (OriginalFilename '$orig'), not an interpreter"
  }
  if ($orig -notmatch '(?i)^python(w)?\.exe$') {
    throw "refusing: '$Path' OriginalFilename is '$orig'; a CPython interpreter reports 'python.exe'"
  }
  if ($desc -notmatch '(?i)python') {
    throw "refusing: '$Path' FileDescription is '$desc'; expected a Python interpreter"
  }
  Write-Host "ok   interpreter identity (metadata, not executed): OriginalFilename '$orig', '$desc' $($vi.FileVersion)"
}

function Test-GuvfxInterpreterRuntime {
  <# EXECUTES the interpreter. Called ONLY under -Apply, and only after the static identity check passed. #>
  param([Parameter(Mandatory)][string]$Path)
  $ver = & $Path --version 2>&1
  if ($LASTEXITCODE -ne 0 -or "$ver" -notmatch '(?i)^Python 3\.') {
    throw "interpreter '$Path' did not report a Python 3 version (got '$ver', exit $LASTEXITCODE)"
  }
  & $Path -c "import win32serviceutil, win32service, win32event, servicemanager" 2>$null
  if ($LASTEXITCODE -ne 0) {
    throw "pywin32 (win32serviceutil/win32service/win32event/servicemanager) not importable by '$Path' - provision the beta venv"
  }
  Write-Host "ok   interpreter runtime: $ver, pywin32 importable"
}

Test-GuvfxInterpreterIdentity -Path $Python
if ($Apply) { Test-GuvfxInterpreterRuntime -Path $Python }
else { Write-Host "PLAN:  interpreter runtime + pywin32 checks DEFERRED to -Apply (PLAN never executes a candidate binary)" }
Write-Host "ok   preconditions: agent.py, service.py present; interpreter identity verified"

# 1. State dir (durable nonce/idempotency/logs), SEPARATE from the code dir so updates never clobber it.
DoIt "create state dir $StateDir (+ logs)" { New-Item -ItemType Directory -Force -Path $StateDir, (Join-Path $StateDir "logs") | Out-Null }

# 2. Scoped NTFS ACLs for the service account. LEAST PRIVILEGE - but least privilege means the MINIMUM the
#    agent actually needs, and the agent does the staging work itself. It is NOT true that "the agent only
#    triggers tasks": win_slot_ops runs robocopy from the service process (copy_golden), opens the ownership
#    marker for writing inside the slot (write_owner_tag), walks the golden tree to digest it, and renames
#    the slot directory into the tombstone root (move_dir, which needs DELETE on the slot directory).
#    Granting nothing on golden\ and slots\ would leave the pool provisioned and permanently unusable:
#    the first MATERIALISE would fail and TOMBSTONE could never complete.
#      - Modify   on the state dir, the tombstone root and the SLOT ROOT (stage, mark, move);
#      - ReadAndExecute on the golden image (read-only: if the agent could write it, one compromised slot
#        would compromise every future slot) and on its OWN code dir (so it cannot rewrite the bundle it is
#        integrity-checked against).

function Get-GuvfxServiceSid {
  <# The service SID is DERIVED from the service name, so it exists as a value before the service does.
     Needed because this script grants ACLs BEFORE `sc create` runs (step 3), and at that point
     "NT SERVICE\GuvFXBetaAgent" has no name mapping: icacls fails with 1332 - for the name AND for the
     raw SID, because it reverse-resolves - and aborts the install at its first grant. #>
  param([Parameter(Mandatory)][string]$ServiceName)
  if ($ServiceName -ne "GuvFXBetaAgent") {
    throw "refusing service SID lookup for '$ServiceName': fixed to the beta agent service account"
  }
  $m = (& sc.exe showsid $ServiceName) | Select-String -Pattern "SERVICE SID:\s*(S-1-5-80-\S+)"
  if (-not $m) { throw "could not compute the service SID for '$ServiceName'" }
  $v = $m.Matches.Groups[1].Value
  if ($v -notmatch "^S-1-5-80-\d+-\d+-\d+-\d+-\d+$") { throw "refusing: '$v' is not a service SID" }
  return $v
}

function Grant-GuvfxServiceAcl {
  <# Grant one right set to the service SID on one directory, with container+object inheritance, using
     Set-Acl so no name resolution is involved. Post-checked by reading the DACL back AS SIDs. #>
  param([Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][ValidateSet("Modify","ReadAndExecute")][string]$Rights,
        [Parameter(Mandatory)][string]$ServiceSid)
  $sid  = New-Object System.Security.Principal.SecurityIdentifier($ServiceSid)
  $acl  = Get-Acl -Path $Path
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
            $sid, [System.Security.AccessControl.FileSystemRights]::$Rights,
            ([System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
             [System.Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [System.Security.AccessControl.PropagationFlags]::None, "Allow")
  $acl.AddAccessRule($rule)
  Set-Acl -Path $Path -AclObject $acl
  $rules = (Get-Acl -Path $Path).GetAccessRules($true, $false,
             [System.Security.Principal.SecurityIdentifier])
  if (@($rules | Where-Object { $_.IdentityReference.Value -eq $ServiceSid }).Count -eq 0) {
    throw "post-check failed: service SID $ServiceSid is not on $Path"
  }
  Write-Host "evidence acl path=$Path service_sid=$ServiceSid rights=$Rights result=granted"
}

$ServiceSid = Get-GuvfxServiceSid -ServiceName $ServiceName
Write-Host "ok   service SID computed before the service exists: $ServiceSid"

foreach ($d in @($StateDir, $BetaTombstones, $SlotsRoot)) {
  DoIt "grant '$RunAsUser' Modify on $d (inherit)" {
    Grant-GuvfxServiceAcl -Path $d -Rights Modify -ServiceSid $ServiceSid
  }
}
foreach ($d in @($AgentDir, $GoldenDir)) {
  DoIt "grant '$RunAsUser' ReadAndExecute on $d (inherit)" {
    Grant-GuvfxServiceAcl -Path $d -Rights ReadAndExecute -ServiceSid $ServiceSid
  }
}
if (-not (Test-Path $SlotsRoot)) {
  throw "slot pool not provisioned at $SlotsRoot - run install_pool.ps1 -Apply FIRST (install-only review F1/F2)"
}

# 3. Install the pywin32 service, MANUAL start, under the virtual account. (No auto-start; no start here.)
DoIt "install service '$ServiceName' (pywin32, startup=manual)" { & $Python (Join-Path $AgentDir "service.py") "--startup=manual" "install" }
# password= "" is required by sc.exe when assigning an NT SERVICE virtual account (harmless otherwise); without
# it the obj= assignment can silently fail and leave the service running as over-privileged LocalSystem.
DoIt "set service logon to '$RunAsUser' (no password) + start=demand" { sc.exe config $ServiceName obj= "$RunAsUser" password= "" start= demand | Out-Null }

# 4. Recovery DISABLED for the first install (nothing may auto-restart before approval).
DoIt "disable service recovery actions" { sc.exe failure $ServiceName reset= 0 actions= "" | Out-Null }

# 5. Verify (no start).
if ($Apply) {
  Step "VERIFY service configuration (expect STOPPED, start=demand, correct identity, no recovery actions)"
  sc.exe qc $ServiceName
  sc.exe qfailure $ServiceName
  $svc = Get-Service $ServiceName
  if ($svc.Status -ne "Stopped") { throw "service is $($svc.Status); expected Stopped (install-only)" }
  $startName = (Get-CimInstance Win32_Service -Filter "Name='$ServiceName'").StartName
  if ("$startName" -notmatch [regex]::Escape($RunAsUser)) {
    throw "service identity is '$startName', expected '$RunAsUser' - LocalSystem means the obj= assignment failed; do NOT start"
  }
  Write-Host "ok   service identity = $startName"
  # Assert the DACLs actually took. icacls is a native command: $ErrorActionPreference does not apply to it,
  # so a silently failed grant would otherwise surface as a first-MATERIALISE failure on the live host.
  foreach ($d in @($StateDir, $BetaTombstones, $SlotsRoot, $AgentDir, $GoldenDir)) {
    $sids = @((Get-Acl -Path $d).GetAccessRules($true, $false,
                [System.Security.Principal.SecurityIdentifier]) |
              ForEach-Object { $_.IdentityReference.Value })
    if ($sids -notcontains $ServiceSid) {
      throw "no ACE for '$RunAsUser' ($ServiceSid) on $d - the grant did not take; do NOT start"
    }
    Write-Host "ok   DACL on $d carries an ACE for $RunAsUser"
  }
  Write-Host "ok   service installed STOPPED. Firewall: run firewall.ps1 -Apply. Do NOT start until approval."
  Write-Host ""
  Write-Host "REQUIRED environment for the slot-pool model (set before the first start, not now):"
  Write-Host "  BETA_AGENT_EXECUTION_MODEL=slot_pool   BETA_AGENT_SLOT_POOL_SIZE=4"
  Write-Host "  BETA_AGENT_GOLDEN_DIR / _DIGEST / _MANIFEST_VERSION   (all three; empty values are refused)"
  Write-Host "  BETA_AGENT_APPROVED_TASKS=C:\GuvFX\beta\agent-state\approved_tasks.json  (launch gate, F3)"
  Write-Host "  BETA_AGENT_DRAIN_TIMEOUT_S=45          (must exceed the 30s settle window, or startup refuses)"
} else {
  Write-Host "PLAN complete. Re-run with -Apply on the host to perform the install (install-only, no start)."
}

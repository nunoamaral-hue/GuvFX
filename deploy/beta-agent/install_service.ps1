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
  [string]$Python      = "C:\GuvFX\python311.exe",              # verified bundled 3.11.9 interpreter
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
if (-not (Test-Path $Python)) { throw "interpreter not found: $Python" }
& $Python -c "import win32serviceutil" 2>$null
if ($LASTEXITCODE -ne 0) { throw "pywin32 not importable by $Python - install pywin32 into the agent interpreter first" }
Write-Host "ok   preconditions: agent.py, service.py, interpreter + pywin32 present"

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
foreach ($d in @($StateDir, $BetaTombstones, $SlotsRoot)) {
  DoIt "grant '$RunAsUser' Modify on $d (inherit)" {
    icacls $d /grant ("{0}:(OI)(CI)M" -f $RunAsUser) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed granting Modify on $d (exit $LASTEXITCODE)" }
  }
}
foreach ($d in @($AgentDir, $GoldenDir)) {
  DoIt "grant '$RunAsUser' ReadAndExecute on $d (inherit)" {
    icacls $d /grant ("{0}:(OI)(CI)RX" -f $RunAsUser) | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed granting ReadAndExecute on $d (exit $LASTEXITCODE)" }
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
    $acl = (icacls $d) -join "`n"
    if ($acl -notmatch [regex]::Escape($RunAsUser)) {
      throw "no ACE for '$RunAsUser' on $d - the grant did not take; do NOT start"
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

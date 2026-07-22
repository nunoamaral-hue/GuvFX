# CVM-Inc-3 B3P-2 — provision the pre-provisioned SLOT POOL: identities, rights, directories, ACLs, tasks.
# DARK ARTEFACT: RUN ONLY on the host, as Administrator, AFTER the Install Authorisation gate.
# INSTALL-ONLY: it creates objects and stops. It does NOT start the service, does NOT enable any task, does
# NOT trigger anything, does NOT launch MT5, does NOT stage a runtime into a slot, and never touches
# Session 3 / the prod terminal / the bridge (:8788) / :8787 / autologon / any GuvFX_* task.
#
# Dry-run by default. Pass -Apply to perform the provisioning.
#
# Addresses install-only review findings F1 (pool paths + ACLs), F2 (identities, rights, tasks, golden) and
# supplies the approved-task definitions the agent's launch gate (F3) asserts against.
#
# PASSWORDS ARE NOT PARAMETERS. They are prompted for interactively as SecureString, so they never appear in
# a command line, a process listing, a shell history, a transcript or a scheduled-task argument. There is
# deliberately no switch to supply them non-interactively.
param(
  [int]$PoolSize            = 4,
  [string]$SlotsRoot        = "C:\GuvFX\beta\slots",
  [string]$TombstonesRoot   = "C:\GuvFX\beta\tombstones",
  [string]$GoldenDir        = "C:\GuvFX\beta\golden",
  [string]$IdentityPrefix   = "guvfx_b_slot",
  [string]$LaunchPrefix     = "GuvFXBetaRuntime-",
  [string]$StopPrefix       = "GuvFXBetaRuntimeStop-",
  [string]$ApprovedTasksOut = "C:\GuvFX\beta\agent-state\approved_tasks.json",
  [switch]$Apply
)
$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "==> $m" }
function DoIt($desc, [scriptblock]$block) {
  if ($Apply) { Step "APPLY: $desc"; & $block } else { Step "PLAN:  $desc" }
}

# ── 0. Refusals. These are the estate objects this script must never come near. Checked BEFORE anything.
$ForbiddenTasks = @("GuvFX_Autostart","GuvFX_SignalBridge","GuvFX_BridgeWatchdog","GuvFX_LaunchMT5","GFX_LaunchIS6")
$ForbiddenPaths = @("C:\GuvFX\accounts","C:\GuvFX\terminals")
foreach ($p in @($SlotsRoot, $TombstonesRoot, $GoldenDir)) {
  foreach ($f in $ForbiddenPaths) {
    if ($p -like "$f*") { throw "refusing: '$p' is inside the operator's estate ('$f')" }
  }
  if ($p -notlike "C:\GuvFX\beta\*") { throw "refusing: '$p' is outside C:\GuvFX\beta\" }
}
if ($LaunchPrefix -notlike "GuvFXBetaRuntime*" -or $StopPrefix -notlike "GuvFXBetaRuntime*") {
  throw "refusing: task prefixes must be in the beta task namespace"
}
if ($IdentityPrefix -ne "guvfx_b_slot") {
  throw "refusing: identity prefix must match win_primitives.RUNTIME_IDENTITY_PREFIX"
}
Write-Host "ok   namespace refusals pass (estate paths, estate tasks, identity + task prefixes)"

# ── 1. Preconditions.
if (-not (Test-Path $GoldenDir)) { throw "golden image not staged at $GoldenDir — stage it before provisioning slots" }
if (-not (Test-Path (Join-Path $GoldenDir "terminal64.exe"))) { throw "no terminal64.exe under $GoldenDir" }
foreach ($marker in @(".guvfx_golden_manifest", ".guvfx_portable")) {
  if (-not (Test-Path (Join-Path $GoldenDir $marker))) { throw "golden image missing required marker $marker" }
}
# Per-instance state must NOT be inherited from the golden image: it would carry one runtime's broker login
# into every slot. MetaQuotes documents these as the terminal's own data directories.
foreach ($leak in @("config\accounts.dat","config\servers.dat","bases","logs","MQL5\Logs","MQL5\Profiles")) {
  if (Test-Path (Join-Path $GoldenDir $leak)) { throw "golden image contains per-instance state '$leak' — clean it before provisioning" }
}
Write-Host "ok   golden image present, marked, and free of per-instance state"

# ── 2. Identities. Created here because the agent cannot: it has no user-creation method and holds no
#      credential. Passwords are prompted, never parameters.
for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  if (Get-LocalUser -Name $user -ErrorAction SilentlyContinue) {
    Write-Host "note identity '$user' already exists; leaving as-is"
    continue
  }
  DoIt "create non-admin identity '$user' (password prompted, never a parameter)" {
    $pw = Read-Host -AsSecureString "Password for $user (not echoed, not logged)"
    New-LocalUser -Name $user -Password $pw -PasswordNeverExpires -AccountNeverExpires `
                  -Description "GuvFX beta slot $n runtime identity" | Out-Null
    Remove-Variable pw
    Add-LocalGroupMember -Group "Users" -Member $user
  }
}
# Assert group membership in BOTH directions: in Users, and in nothing privileged.
if ($Apply) {
  for ($n = 1; $n -le $PoolSize; $n++) {
    $user = "$IdentityPrefix$n"
    foreach ($g in @("Administrators","Remote Desktop Users","Backup Operators")) {
      $m = Get-LocalGroupMember -Group $g -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -like "*\$user" }
      if ($m) { throw "identity '$user' is a member of '$g' — refusing to continue" }
    }
    Write-Host "ok   $user is non-admin"
  }
}

# ── 3. SeBatchLogonRight. A LOGON RIGHT, not a privilege: it will not appear in `whoami /priv`.
#      Granted explicitly so the documented ambiguity about auto-grant on task registration is irrelevant.
DoIt "grant SeBatchLogonRight to $IdentityPrefix1..$PoolSize (via secedit)" {
  $tmp = New-TemporaryFile
  secedit /export /areas USER_RIGHTS /cfg "$tmp" | Out-Null
  $cfg = Get-Content "$tmp"
  $sids = @()
  for ($n = 1; $n -le $PoolSize; $n++) {
    $sids += "*" + (Get-LocalUser -Name "$IdentityPrefix$n").SID.Value
  }
  $line = ($cfg | Where-Object { $_ -match "^SeBatchLogonRight" })
  $existing = if ($line) { ($line -split "=", 2)[1].Trim() } else { "" }
  $merged = (@($existing -split "," | Where-Object { $_ }) + $sids | Select-Object -Unique) -join ","
  $new = if ($line) { $cfg -replace "^SeBatchLogonRight.*", "SeBatchLogonRight = $merged" }
         else { $cfg -replace "^\[Privilege Rights\]", "[Privilege Rights]`r`nSeBatchLogonRight = $merged" }
  Set-Content -Path "$tmp" -Value $new
  secedit /configure /db "$env:windir\security\local.sdb" /cfg "$tmp" /areas USER_RIGHTS | Out-Null
  Remove-Item "$tmp" -Force
}

# ── 4. Directories. Slot directories MUST pre-exist: stage_copy refuses when real_path(slot dir) is null,
#      which is what stops the agent materialising into a slot nobody provisioned (robocopy would otherwise
#      create the whole chain — no identity, no ACL, no tasks).
DoIt "create slot + tombstone directories" {
  for ($n = 1; $n -le $PoolSize; $n++) {
    New-Item -ItemType Directory -Force -Path (Join-Path $SlotsRoot "$n"), (Join-Path $TombstonesRoot "$n") | Out-Null
  }
}

# ── 5. ACLs. Each slot identity gets Modify on ITS OWN slot directory and nothing else; read+execute on the
#      golden image (if a runtime could write it, one compromised slot would compromise every future slot).
#      Inheritance is broken at the beta root so nothing upstream can widen these.
DoIt "break inheritance on C:\GuvFX\beta and set explicit ACLs" {
  icacls "C:\GuvFX\beta" /inheritance:r | Out-Null
  icacls "C:\GuvFX\beta" /grant "*S-1-5-32-544:(OI)(CI)F" /grant "*S-1-5-18:(OI)(CI)F" | Out-Null
}
for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  $slot = Join-Path $SlotsRoot "$n"
  DoIt "grant '$user' Modify on $slot only" { icacls $slot /grant ("{0}:(OI)(CI)M" -f $user) | Out-Null }
  DoIt "grant '$user' ReadAndExecute on $GoldenDir" { icacls $GoldenDir /grant ("{0}:(OI)(CI)RX" -f $user) | Out-Null }
}
DoIt "restrict $TombstonesRoot to Administrators + SYSTEM" {
  icacls $TombstonesRoot /inheritance:r | Out-Null
  icacls $TombstonesRoot /grant "*S-1-5-32-544:(OI)(CI)F" /grant "*S-1-5-18:(OI)(CI)F" | Out-Null
}

# ── 6. Scheduled tasks. Registered DISABLED, with NO trigger, on-demand only. TASK_LOGON_PASSWORD stores
#      the credential in the Task Scheduler credential store — the agent never sees it and cannot read it.
#      S4U was rejected: it stores no password but grants no network access, which MT5 needs.
$Approved = @{}
for ($n = 1; $n -le $PoolSize; $n++) {
  $user   = "$IdentityPrefix$n"
  $slot   = Join-Path $SlotsRoot "$n"
  $work   = Join-Path $slot "terminal"
  $exe    = Join-Path $work "terminal64.exe"
  $launch = "$LaunchPrefix$n"
  $stop   = "$StopPrefix$n"

  foreach ($t in @($launch, $stop)) {
    if ($ForbiddenTasks -contains $t) { throw "refusing: '$t' collides with an estate task" }
  }

  DoIt "register '$launch' (disabled, no trigger, /portable, runs as $user)" {
    $pw = Read-Host -AsSecureString "Password for $user (task registration; not echoed, not logged)"
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
               [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
    $action    = New-ScheduledTaskAction -Execute $exe -Argument "/portable" -WorkingDirectory $work
    $settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
                   -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $launch -Action $action -Settings $settings `
      -User $user -Password $plain -RunLevel Limited | Out-Null
    Disable-ScheduledTask -TaskName $launch | Out-Null
    Remove-Variable plain, pw
  }
  DoIt "register '$stop' (disabled, no trigger, terminates ONLY this slot's image)" {
    $pw = Read-Host -AsSecureString "Password for $user (stop task; not echoed, not logged)"
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
               [Runtime.InteropServices.Marshal]::SecureStringToBSTR($pw))
    # taskkill scoped by IMAGE PATH is not available, so the stop task is scoped by running as the SLOT
    # IDENTITY: it can only terminate processes that identity owns, which is exactly this slot's runtime.
    # It can never reach the operator's terminal, which runs as a different account.
    $action   = New-ScheduledTaskAction -Execute "taskkill.exe" -Argument "/IM terminal64.exe /T /F"
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(5))
    Register-ScheduledTask -TaskName $stop -Action $action -Settings $settings `
      -User $user -Password $plain -RunLevel Limited | Out-Null
    Disable-ScheduledTask -TaskName $stop | Out-Null
    Remove-Variable plain, pw
  }

  # The approved definition the agent's launch gate asserts against. Seven fields, matching
  # occupancy.TASK_IDENTITY_FIELDS exactly. `enabled` is true because the gate runs at FIRST START, by
  # which point the operator has enabled the tasks under a separate approval.
  $Approved[$launch] = [ordered]@{
    task_name = $launch; run_as_identity = $user; executable = $exe; working_directory = $work
    logon_type = 1; run_level = 0; enabled = $true
  }
}

# ── 7. Emit the approved-task definitions the agent loads at startup (F3). Without this file the agent
#      refuses to start in slot_pool mode, and no launch can proceed.
DoIt "write approved task definitions to $ApprovedTasksOut" {
  New-Item -ItemType Directory -Force -Path (Split-Path $ApprovedTasksOut) | Out-Null
  $Approved | ConvertTo-Json -Depth 4 | Set-Content -Path $ApprovedTasksOut -Encoding UTF8
  icacls $ApprovedTasksOut /inheritance:r | Out-Null
  icacls $ApprovedTasksOut /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F" | Out-Null
}

# ── 8. Verify (no start, no trigger, no enable).
if ($Apply) {
  Step "VERIFY pool (expect: identities non-admin, tasks present and DISABLED, no triggers)"
  for ($n = 1; $n -le $PoolSize; $n++) {
    foreach ($t in @("$LaunchPrefix$n", "$StopPrefix$n")) {
      $task = Get-ScheduledTask -TaskName $t -ErrorAction Stop
      if ($task.State -ne "Disabled") { throw "task '$t' is $($task.State); expected Disabled (install-only)" }
      if (@($task.Triggers).Count -gt 0) { throw "task '$t' has a trigger; expected on-demand only" }
      $principal = $task.Principal
      if ($principal.UserId -notlike "*$IdentityPrefix$n") { throw "task '$t' principal is $($principal.UserId)" }
      if ($principal.RunLevel -ne "Limited") { throw "task '$t' RunLevel is $($principal.RunLevel); expected Limited" }
      Write-Host "ok   $t disabled, no trigger, principal $($principal.UserId), RunLevel Limited"
    }
  }
  Step "VERIFY estate untouched"
  foreach ($t in $ForbiddenTasks) {
    $e = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    if ($e) { Write-Host "ok   estate task '$t' present and untouched (state $($e.State))" }
  }
  Write-Host ""
  Write-Host "ok   pool provisioned. Tasks are DISABLED. Nothing has been started, triggered or staged."
  Write-Host "     Next: install_service.ps1 -Apply, then firewall.ps1 -Apply. Do NOT start until approval."
} else {
  Write-Host ""
  Write-Host "PLAN complete. Re-run with -Apply on the host to provision the pool (install-only, no start)."
}

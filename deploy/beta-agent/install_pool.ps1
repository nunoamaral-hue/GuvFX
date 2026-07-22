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
# ONE prompt per identity, confirmed by re-entry, held for that identity's whole provisioning. Prompting
# separately for the account and for each task registration invited a typo that creates a working account
# whose tasks can never log it on — a failure that would only surface at first start.
$Secrets = @{}
function Get-SlotSecret($user) {
  if ($Secrets.ContainsKey($user)) { return $Secrets[$user] }
  while ($true) {
    $a = Read-Host -AsSecureString "Password for $user (not echoed, not logged)"
    $b = Read-Host -AsSecureString "Confirm password for $user"
    $pa = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($a))
    $pb = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($b))
    $same = ($pa -ceq $pb)
    Remove-Variable pa, pb
    if ($same) { $Secrets[$user] = $a; return $a }
    Write-Host "     passwords did not match; try again"
  }
}

for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  if (Get-LocalUser -Name $user -ErrorAction SilentlyContinue) {
    Write-Host "note identity '$user' already exists; leaving as-is (its password is still needed below)"
    continue
  }
  DoIt "create non-admin identity '$user' (password prompted, never a parameter)" {
    New-LocalUser -Name $user -Password (Get-SlotSecret $user) -PasswordNeverExpires -AccountNeverExpires `
                  -Description "GuvFX beta slot $n runtime identity" | Out-Null
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
DoIt "grant SeBatchLogonRight to ${IdentityPrefix}1..$PoolSize (via secedit)" {
  $tmp = New-TemporaryFile
  secedit /export /areas USER_RIGHTS /cfg "$tmp" | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "secedit /export failed (exit $LASTEXITCODE)" }
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
  # secedit EXPORTS UTF-16LE and the template declares Unicode=yes; Set-Content's 5.1 default is ANSI, which
  # would corrupt a machine-wide security template. -Encoding Unicode is mandatory, not cosmetic.
  Set-Content -Path "$tmp" -Value $new -Encoding Unicode
  secedit /configure /db "$env:windir\security\local.sdb" /cfg "$tmp" /areas USER_RIGHTS | Out-Null
  # secedit is a native command: $ErrorActionPreference does not apply, so a failed apply is silent.
  if ($LASTEXITCODE -ne 0) { throw "secedit /configure failed (exit $LASTEXITCODE) — SeBatchLogonRight NOT granted" }
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
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
               [Runtime.InteropServices.Marshal]::SecureStringToBSTR((Get-SlotSecret $user)))
    $action    = New-ScheduledTaskAction -Execute $exe -Argument "/portable" -WorkingDirectory $work
    $settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
                   -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $launch -Action $action -Settings $settings `
      -User $user -Password $plain -RunLevel Limited -Force | Out-Null
    Disable-ScheduledTask -TaskName $launch | Out-Null
    Remove-Variable plain
  }
  DoIt "register '$stop' (disabled, no trigger, terminates ONLY this slot's image)" {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
               [Runtime.InteropServices.Marshal]::SecureStringToBSTR((Get-SlotSecret $user)))
    # SCOPED BY IMAGE PATH, not by image name. `taskkill /IM terminal64.exe` would match by NAME — and the
    # operator's production terminal has the SAME name, because a slot is a copy of the same MT5 image.
    # Relying on "it runs as the slot identity so it can only kill its own processes" would make a
    # not-fully-verifiable OS access-control assumption load-bearing on the one action that could stop live
    # trading. The agent's own code refuses to match a process by name for exactly this reason; the task it
    # triggers must not do what the agent is forbidden to do.
    #
    # Get-Process .Path on another account's process yields nothing readable to a non-admin, and a null
    # path can never equal this slot's path — so the filter fails safe in both directions.
    $kill = "Get-Process -Name terminal64 -ErrorAction SilentlyContinue | " +
            "Where-Object { `$_.Path -eq '$exe' } | Stop-Process -Force"
    $action   = New-ScheduledTaskAction -Execute "powershell.exe" `
                  -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"$kill`"")
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(5))
    Register-ScheduledTask -TaskName $stop -Action $action -Settings $settings `
      -User $user -Password $plain -RunLevel Limited -Force | Out-Null
    Disable-ScheduledTask -TaskName $stop | Out-Null
    Remove-Variable plain
  }

  # The approved definition the agent's launch gate asserts against. Read back through the SAME COM
  # interface the agent uses (Schedule.Service), not from the values we intended: Task Scheduler may
  # normalise the principal to a qualified form or a SID, and the gate compares the 7-field digest by exact
  # equality. Pinning what we MEANT to register, while the agent reads what IS registered, would make every
  # first launch fail task_definition_drift — permanently, since the agent never repairs a task.
  if ($Apply) {
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    $reg = $svc.GetFolder("\").GetTask($launch)
    $p   = $reg.Definition.Principal
    $act = $reg.Definition.Actions.Item(1)
    $Approved[$launch] = [ordered]@{
      task_name         = [string]$reg.Name
      run_as_identity   = [string]$p.UserId
      executable        = [string]$act.Path
      working_directory = [string]$act.WorkingDirectory
      arguments         = [string]$act.Arguments
      logon_type        = [int]$p.LogonType
      run_level         = [int]$p.RunLevel
      # The gate runs at FIRST START, by which point the operator has enabled the tasks under a separate
      # approval — so the approval pins enabled=true even though the task is disabled right now.
      enabled           = $true
    }
    if ($Approved[$launch].arguments -notmatch "(^|\s)/portable(\s|$)") {
      throw "registered task '$launch' does not carry /portable — refusing to approve it"
    }
  }
}

# ── 7. Emit the approved-task definitions the agent loads at startup (F3). Without this file the agent
#      refuses to start in slot_pool mode, and no launch can proceed.
DoIt "write approved task definitions to $ApprovedTasksOut" {
  New-Item -ItemType Directory -Force -Path (Split-Path $ApprovedTasksOut) | Out-Null
  # WriteAllText with UTF8Encoding($false), NOT Set-Content -Encoding UTF8: under Windows PowerShell 5.1
  # the latter emits a BOM, and the agent's json.loads would reject "\ufeff{" as malformed — reported as
  # "tampering or a bad edit" during the one window when the operator is judging whether to trust the host.
  [IO.File]::WriteAllText($ApprovedTasksOut, ($Approved | ConvertTo-Json -Depth 4),
                          (New-Object Text.UTF8Encoding $false))
  icacls $ApprovedTasksOut /inheritance:r | Out-Null
  # The service account needs READ — it loads this file at startup and refuses to start without it. Read
  # only: the agent must never be able to rewrite its own approvals. Inheritance is stripped, so the
  # inheritable grant on the state dir cannot reach this file; the ACE has to be explicit.
  icacls $ApprovedTasksOut /grant "*S-1-5-32-544:F" /grant "*S-1-5-18:F" `
                           /grant "NT SERVICE\GuvFXBetaAgent:(R)" | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "icacls failed on $ApprovedTasksOut (exit $LASTEXITCODE)" }
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

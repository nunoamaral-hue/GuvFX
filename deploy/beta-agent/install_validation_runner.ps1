# ADR-0027 task-launch remediation — installer for the validation-runner scheduled task + secure handoff.
#
# Root cause (2026-08-02): MT5 GUI/MDI creation fails when the terminal is launched IN-PROCESS by the WinSW
# Agent service (a non-interactive service window station); it succeeds via a scheduled task. This installs
# the ONE pre-approved, single-instance task the Agent triggers (by name only, no arguments/secrets) and the
# ACL-restricted handoff directory the sealed request crosses through.
#
# ASCII-only (RULE 9). Validate with [Parser]::ParseFile before running on the host. Idempotent.
[CmdletBinding()]
param(
  [string]$TaskName   = "GvfxValidationRunner",
  [string]$Python     = "C:\GuvFX\beta\agent-venv\Scripts\python.exe",
  [string]$Runner     = "C:\GuvFX\beta\agent\validation_runner.py",
  [string]$HandoffDir = "C:\GuvFX\beta\agent-state\validation-handoff",
  # Task identity. SYSTEM is host-PROVEN (Experiment E, 2026-08-02) to give MT5 a GUI-capable window station
  # via a scheduled task, unlike the WinSW service. A dedicated low-privilege account is the least-privilege
  # follow-up; the task command is fixed and takes no arguments, so its blast radius is one validation probe.
  [string]$RunAs      = "SYSTEM",
  # The least-privilege agent service that TRIGGERS this task via the Task Scheduler COM API (task.Run).
  # It must hold READ+EXECUTE on the task's security descriptor, exactly as install_pool.ps1 grants the
  # beta-agent SID on the GuvFXBetaRuntime-* slot tasks. Without this the agent's win.run_task() is denied
  # and the scheduler returns SCHED_S_TASK_HAS_NOT_RUN (0x00041303).
  [string]$AgentService = "GuvFXBetaAgent",
  [int]$TimeoutMin    = 5,
  [switch]$Apply
)

#: Task Scheduler READ+EXECUTE (open + read definition + run). GENERIC_READ(0x120089)|FILE_EXECUTE(0x20).
#: Identical mask to install_pool.ps1 — the service may find and run its own task, nothing more (no write,
#: delete or change-permissions). RULE 11: read back and assert EXACTLY this mask, nothing broader.
$GuvfxTaskReadRunMask = 0x1200a9

function Get-AgentServiceSid([string]$Name) {
  # A service SID is DERIVED from the fixed name and is stable. Refuse any name but the expected agent.
  if ($Name -ne "GuvFXBetaAgent") { throw "refusing service SID lookup for '$Name'" }
  $shown = & sc.exe showsid $Name 2>&1
  $m = $shown | Select-String -Pattern "SERVICE SID:\s*(S-1-5-80-\S+)"
  if (-not $m) { throw "could not compute service SID for '$Name'" }
  $v = $m.Matches.Groups[1].Value
  if ($v -notmatch "^S-1-5-80-\d+-\d+-\d+-\d+-\d+$") { throw "refusing non-service SID '$v'" }
  return $v
}

function Grant-TaskReadRun([string]$Task, [string]$ServiceSid) {
  # Grant the agent service SID READ+EXECUTE on ONE validation task via the COM security descriptor.
  # Idempotent (removes any prior ACE for this SID first); read-back-verified to be EXACTLY the run mask.
  if ($ServiceSid -notmatch "^S-1-5-80-\d+-\d+-\d+-\d+-\d+$") { throw "refusing task grant to non-service SID" }
  if ($Task -ne "GvfxValidationRunner") { throw "refusing task grant to non-validation task '$Task'" }
  $svc = New-Object -ComObject Schedule.Service; $svc.Connect()
  $t = $svc.GetFolder("\").GetTask($Task)
  $sd = New-Object System.Security.AccessControl.RawSecurityDescriptor($t.GetSecurityDescriptor(7))
  $sid = New-Object System.Security.Principal.SecurityIdentifier($ServiceSid)
  for ($i = $sd.DiscretionaryAcl.Count - 1; $i -ge 0; $i--) {
    if ($sd.DiscretionaryAcl[$i].SecurityIdentifier -eq $sid) { $sd.DiscretionaryAcl.RemoveAce($i) }
  }
  $ace = New-Object System.Security.AccessControl.CommonAce(
      [System.Security.AccessControl.AceFlags]::None, [System.Security.AccessControl.AceQualifier]::AccessAllowed,
      $GuvfxTaskReadRunMask, $sid, $false, $null)
  $sd.DiscretionaryAcl.InsertAce($sd.DiscretionaryAcl.Count, $ace)
  $t.SetSecurityDescriptor($sd.GetSddlForm([System.Security.AccessControl.AccessControlSections]::All), 0)
  $back = New-Object System.Security.AccessControl.RawSecurityDescriptor(($svc.GetFolder("\").GetTask($Task)).GetSecurityDescriptor(7))
  $aces = @($back.DiscretionaryAcl | Where-Object { $_.SecurityIdentifier -eq $sid })
  if ($aces.Count -ne 1) { throw "task '$Task': expected exactly ONE service ACE, found $($aces.Count)" }
  if ([int]$aces[0].AccessMask -ne $GuvfxTaskReadRunMask) {
    throw ("task '$Task': service ACE mask 0x{0:X} != authorised 0x{1:X}" -f [int]$aces[0].AccessMask, $GuvfxTaskReadRunMask)
  }
  return $ServiceSid
}
$ErrorActionPreference = "Stop"

function Say($m) { Write-Output ("[install-validation-runner] " + $m) }

if (-not (Test-Path $Python)) { throw "python not found: $Python" }
if (-not (Test-Path $Runner)) { throw "runner not found: $Runner" }

Say ("PLAN: task=" + $TaskName + " runAs=" + $RunAs + " handoff=" + $HandoffDir + " timeout=" + $TimeoutMin + "m")
if (-not $Apply) { Say "DRY RUN (pass -Apply to make changes)"; return }

# 1) handoff directory with a RESTRICTIVE ACL: SYSTEM + Administrators only (inheritance disabled). The
#    sealed request (ciphertext) crosses here; no other local user may read or write it.
New-Item -ItemType Directory -Force -Path $HandoffDir | Out-Null
icacls $HandoffDir /inheritance:r /Q | Out-Null
icacls $HandoffDir /grant:r "NT AUTHORITY\SYSTEM:(OI)(CI)(F)" "BUILTIN\Administrators:(OI)(CI)(F)" /Q | Out-Null
Say ("handoff ACL set; entries: " + ((icacls $HandoffDir | Select-String 'Allow|:\(') -join '; '))

# 2) the single-instance, allow-listed scheduled task: runs ONLY the runner, with NO arguments.
$action = New-ScheduledTaskAction -Execute $Python -Argument $Runner
if ($RunAs -eq "SYSTEM") {
  $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
} else {
  $principal = New-ScheduledTaskPrincipal -UserId $RunAs -LogonType Password -RunLevel Highest
}
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
              -ExecutionTimeLimit (New-TimeSpan -Minutes $TimeoutMin) `
              -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable:$false
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings -Force | Out-Null
$t = Get-ScheduledTask -TaskName $TaskName
Say ("task registered: state=" + $t.State + " exec=" + $t.Actions[0].Execute + " args=" + $t.Actions[0].Arguments)

# 3) grant the least-privilege Agent service Read+Execute (run) on the task, so its COM task.Run() is not
#    denied (the defect: default task security allowed only Administrators + SYSTEM to run it, so the Agent's
#    win.run_task() failed with SCHED_S_TASK_HAS_NOT_RUN). Mirrors install_pool.ps1's slot-task grant exactly.
$svcSid = Get-AgentServiceSid $AgentService
$granted = Grant-TaskReadRun $TaskName $svcSid
Say ("task run-ACL granted to " + $AgentService + " (sid=" + $granted + ", mask=0x{0:X})" -f $GuvfxTaskReadRunMask)
Say "DONE. Set BETA_AGENT_VALIDATION_TASK_NAME + BETA_AGENT_VALIDATION_HANDOFF_DIR for the Agent, then restart it."

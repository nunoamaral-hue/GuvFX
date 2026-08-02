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
  [int]$TimeoutMin    = 5,
  [switch]$Apply
)
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
Say "DONE. Set BETA_AGENT_VALIDATION_TASK_NAME + BETA_AGENT_VALIDATION_HANDOFF_DIR for the Agent, then restart it."

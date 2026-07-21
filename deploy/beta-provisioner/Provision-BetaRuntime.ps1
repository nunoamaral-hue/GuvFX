<#
GFX-BETA-HEADLESS Increment 2 — Windows beta-runtime provisioner (process lifecycle).

Idempotent operations for ONE isolated beta MT5 runtime on the existing host, run inside the existing
Administrator autologon Session 1 (Option A). This is the box-side capability the backend driver
(terminal_provisioning/provisioner.py) orchestrates over the authenticated management channel.

PROVEN CAPABILITY: the Materialise/Start(Session-1 LogonType-Interactive task)/Verify/Teardown steps
were experimentally validated on 2026-07-20 (6 concurrent runtimes coexisted in Session 1, crash-isolated,
with Nuno's production terminal + bridge on :8788 completely unaffected; the host returned to baseline).

NON-NEGOTIABLE SAFETY (enforced by convention here; the caller must not violate it):
  * Operates ONLY under C:\GuvFX\beta\accounts\<RuntimeUuid>\  — never Nuno's C:\GuvFX\accounts\* or terminals\*.
  * NEVER touches Nuno's production terminal (a separate process/session) or his bridge on :8788.
  * NO broker credential is EVER passed on the command line or written to a log here — broker login is
    performed later by the per-runtime beta bridge via the MT5 API using creds delivered over the secure
    channel (a separate increment). This script only manages the terminal PROCESS lifecycle.
  * The scheduled task launches in the interactive Session 1 via LogonType Interactive (no password).

Usage:  powershell -File Provision-BetaRuntime.ps1 -Op <Materialise|Start|Verify|Stop|Teardown> -RuntimeUuid <uuid> [-GoldenBuild 5.0.0.5833]
Emits a single JSON line on stdout: {"ok":bool,"op":...,"running":bool,"pid":int|null,"session":int|null,...}
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("Materialise","Start","Verify","Stop","Teardown")][string]$Op,
  [Parameter(Mandatory=$true)][ValidatePattern('^[0-9a-fA-F-]{36}$')][string]$RuntimeUuid,  # UUID-only: traversal guard
  [string]$GoldenBuild = "5.0.0.5833",
  [string]$AutomationUser = "Administrator"
)
$ErrorActionPreference = "Stop"
$BetaBase   = "C:\GuvFX\beta\accounts"
$GoldenSrc  = "C:\GuvFX\golden\mt5\$GoldenBuild"
$RuntimeDir = Join-Path $BetaBase $RuntimeUuid
$TermDir    = Join-Path $RuntimeDir "terminal"
$TermExe    = Join-Path $TermDir "terminal64.exe"
$TaskName   = "GuvfxBeta_$RuntimeUuid"

function Out-Json($obj) { $obj | ConvertTo-Json -Compress }

function Get-RuntimeProcess {
  # The one terminal64 whose image path is under THIS runtime dir (never Nuno's).
  Get-Process terminal64 -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$TermDir*" } | Select-Object -First 1
}

try {
  switch ($Op) {
    "Materialise" {
      if (-not (Test-Path $GoldenSrc)) { throw "golden build $GoldenBuild not found" }
      New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
      # Idempotent copy of the clean golden image (no saved login) into the isolated portable dir.
      robocopy $GoldenSrc $TermDir /E /NFL /NDL /NJH /NJS /NP | Out-Null
      Out-Json @{ ok = (Test-Path $TermExe); op = $Op; runtime_root = $TermDir }
    }
    "Start" {
      if (-not (Test-Path $TermExe)) { throw "runtime not materialised" }
      $existing = Get-RuntimeProcess
      if (-not $existing) {
        $a = New-ScheduledTaskAction -Execute $TermExe -Argument "/portable"
        $p = New-ScheduledTaskPrincipal -UserId $AutomationUser -LogonType Interactive -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $a -Principal $p -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 6
      }
      $proc = Get-RuntimeProcess
      Out-Json @{ ok = [bool]$proc; op = $Op; pid = $(if ($proc) { $proc.Id } else { $null });
                  session = $(if ($proc) { $proc.SessionId } else { $null }) }
    }
    "Verify" {
      $proc = Get-RuntimeProcess
      # logged_in / login / server are reported by the beta bridge (MT5 API), not this script; the
      # backend combines this process check with the bridge's broker-identity check before RUNNING.
      Out-Json @{ ok = [bool]$proc; op = $Op; running = [bool]$proc;
                  pid = $(if ($proc) { $proc.Id } else { $null });
                  session = $(if ($proc) { $proc.SessionId } else { $null }) }
    }
    "Stop" {
      $proc = Get-RuntimeProcess
      if ($proc) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
      if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
      }
      Out-Json @{ ok = $true; op = $Op }
    }
    "Teardown" {
      $proc = Get-RuntimeProcess
      if ($proc) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }
      if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
      }
      # Remove ONLY this runtime's isolated dir (path guarded to the beta base + a UUID segment).
      if ($RuntimeDir -like "$BetaBase\*") { Remove-Item -Recurse -Force $RuntimeDir -ErrorAction SilentlyContinue }
      Out-Json @{ ok = (-not (Test-Path $RuntimeDir)); op = $Op }
    }
  }
} catch {
  Out-Json @{ ok = $false; op = $Op; error = "$($_.Exception.Message)" }
  exit 1
}

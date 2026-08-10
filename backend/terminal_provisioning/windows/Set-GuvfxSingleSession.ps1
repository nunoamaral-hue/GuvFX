<#
  Set-GuvfxSingleSession.ps1 -- RDS single-session-per-user invariant for hosted workspaces.

  WHY: Each Guacamole RemoteApp connection is a fresh RDP logon for the fixed hosted Windows
  user (guvfx_u_<id>). With "restrict each user to a single session" DISABLED
  (fSingleSessionPerUser = 0), Windows mints a NEW session per connection instead of
  reconnecting to the existing disconnected one, and each session auto-launches the published
  terminal64 RemoteApp against the SAME portable data directory. Result: duplicate hosted MT5
  runtimes contending for one data dir (config\accounts.dat, journal) for a single workspace.

  The backend delivery layer already uses a stable per-workspace connection id and the fixed
  Windows username precisely to deep-link a reconnect back to the SAME session; that intent is
  only defeated at the Windows layer by fSingleSessionPerUser = 0.

  GUARANTEE: fSingleSessionPerUser = 1 makes a reconnect REJOIN the existing per-user session
  (rejoining the already-running terminal64). It is per-USER, so multi-tenant is preserved:
  different guvfx_u_<id> users still get one session each.

  Defense in depth: MT5 portable also holds a single-instance lock per data directory.

  This script ONLY reads/writes the single HKLM DWORD below. It does not touch sessions,
  processes, ACLs, or any account. It is ASCII-only so it parses identically under Windows
  PowerShell 5.1 with or without a BOM (RULE 9). Parse-validate on the target before first
  execution:
    [System.Management.Automation.Language.Parser]::ParseFile('Set-GuvfxSingleSession.ps1',[ref]$null,[ref]$null)

  Usage:
    powershell -NoProfile -File Set-GuvfxSingleSession.ps1 -Mode Verify
    powershell -NoProfile -File Set-GuvfxSingleSession.ps1 -Mode Enforce
    powershell -NoProfile -File Set-GuvfxSingleSession.ps1 -Mode Rollback
#>
param(
  [ValidateSet('Verify','Enforce','Rollback')]
  [string]$Mode = 'Verify'
)
$ErrorActionPreference = 'Stop'
$Path = 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server'
$Name = 'fSingleSessionPerUser'

function Get-Val { (Get-ItemProperty -Path $Path -Name $Name -ErrorAction SilentlyContinue).$Name }

try {
  $before = Get-Val
  if ($Mode -eq 'Enforce')  { Set-ItemProperty -Path $Path -Name $Name -Value 1 -Type DWord }
  if ($Mode -eq 'Rollback') { Set-ItemProperty -Path $Path -Name $Name -Value 0 -Type DWord }
  $after = Get-Val
  if     ($Mode -eq 'Enforce')  { $ok = ($after -eq 1) }
  elseif ($Mode -eq 'Rollback') { $ok = ($after -eq 0) }
  else                          { $ok = $true }
  [ordered]@{
    setting  = $Name
    path     = $Path
    mode     = $Mode
    before   = $before
    after    = $after
    enforced = ($after -eq 1)
    ok       = [bool]$ok
  } | ConvertTo-Json -Compress
  if (-not $ok) { exit 1 }
}
catch {
  [ordered]@{ ok = $false; mode = $Mode; error = $_.Exception.Message } | ConvertTo-Json -Compress
  exit 1
}

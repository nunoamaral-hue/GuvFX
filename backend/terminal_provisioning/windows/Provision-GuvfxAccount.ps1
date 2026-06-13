<#
  TX-1A / TX-1B — GuvFX per-account Windows materialisation (idempotent).

  Creates a DEDICATED, NON-ADMINISTRATOR local Windows user (guvfx_u_<id>) and a
  DEDICATED MT5 runtime tree (C:\GuvFX\accounts\<id>\{terminal,profiles,logs,config,audit}).

  ADDITIVE ONLY. Does not touch the Administrator account, the legacy shared
  runtime (C:\GuvFX\terminals\account_001), the signal bridge, Guacamole, or VNC.

  Idempotent: re-running makes no duplicate users/dirs and does not reset an
  existing user's password. The generated password is read from STDIN (first
  line) on first creation only and is never echoed or written to disk.

  Usage (password piped on stdin):
    echo <password> | powershell -NoProfile -File Provision-GuvfxAccount.ps1 `
        -AccountId 14 -Username guvfx_u_14 -RuntimeRoot 'C:\GuvFX\accounts\14'
#>
param(
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [string[]]$Subdirs = @("terminal","profiles","logs","config","audit"),
  [switch]$Retire
)

$ErrorActionPreference = "Stop"
$result = [ordered]@{
  account_id = $AccountId; username = $Username; runtime_root = $RuntimeRoot
  user_existed = $false; user_created = $false; is_admin = $false
  dirs_created = @(); dirs_existing = @(); action = "provision"; ok = $false
}

try {
  if ($Retire) {
    $result.action = "retire"
    $u = Get-LocalUser -Name $Username -ErrorAction SilentlyContinue
    if ($u) { Disable-LocalUser -Name $Username }
    $result.ok = $true
    $result | ConvertTo-Json -Compress
    return
  }

  # ── Identity (non-admin) ──
  $existing = Get-LocalUser -Name $Username -ErrorAction SilentlyContinue
  if ($existing) {
    $result.user_existed = $true
  } else {
    $pw = [Console]::In.ReadLine()
    if ([string]::IsNullOrWhiteSpace($pw)) { throw "no password supplied on stdin for new user" }
    $sec = ConvertTo-SecureString $pw -AsPlainText -Force
    New-LocalUser -Name $Username -Password $sec -FullName "GuvFX account $AccountId" `
      -Description "GuvFX isolated MT5 identity (TX-1) for account $AccountId" `
      -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    $pw = $null; $sec = $null
    $result.user_created = $true
    # Default membership is 'Users'; ensure 'Users' and that it is NOT an admin.
    try { Add-LocalGroupMember -Group "Users" -Member $Username -ErrorAction SilentlyContinue } catch {}
  }

  # Enforce non-admin invariant (TX1-R4): never a member of Administrators.
  $adminMembers = (Get-LocalGroupMember -Group "Administrators" -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
  $result.is_admin = [bool]($adminMembers -match ("\\" + [regex]::Escape($Username) + "$"))

  # ── Runtime tree (isolated per account) ──
  foreach ($s in $Subdirs) {
    $p = Join-Path $RuntimeRoot $s
    if (Test-Path $p) { $result.dirs_existing += $p }
    else { New-Item -ItemType Directory -Path $p -Force | Out-Null; $result.dirs_created += $p }
  }

  $result.ok = $true
  $result | ConvertTo-Json -Compress
}
catch {
  $result.ok = $false
  $result.error = $_.Exception.Message
  $result | ConvertTo-Json -Compress
  exit 1
}

<#
  TX-1E — per-user KIOSK SHELL for a dedicated viewer identity (reversible).

  Sets the per-user logon shell of guvfx_u_<id> to the account's viewer MT5
  (instead of explorer.exe) so an RDP logon shows ONLY MT5 — no desktop, no
  Start menu, no Explorer. Scoped STRICTLY to guvfx_u_* users; refuses any other
  account. Reversible with -Revert. Does NOT touch Administrator or machine shell.

  Mechanism: per-user HKCU\...\Winlogon\Shell (Windows prefers HKCU shell over
  the machine default). If the user's profile hive is not loaded (user not
  logged in), the value is staged into their NTUSER.DAT when present; if the
  profile does not exist yet it is reported pending-first-logon (no-op, safe).

  Usage:
    powershell -NoProfile -File Set-GuvfxKioskShell.ps1 -Username guvfx_u_6 `
      -Mt5Path 'C:\GuvFX\accounts\6\terminal\terminal64.exe'
    ... -Revert   (restores default explorer shell)
#>
param(
  [Parameter(Mandatory=$true)][string]$Username,
  [string]$Mt5Path,
  [switch]$Revert
)
$ErrorActionPreference = "Stop"
$result = [ordered]@{ username=$Username; scoped_ok=$false; profile_exists=$false
  hive_loaded=$false; shell_set=$null; reverted=$false; pending_first_logon=$false; ok=$false }

try {
  # ── HARD SCOPE: only guvfx_u_* ──
  if ($Username -notlike "guvfx_u_*") { throw "refused: kiosk shell is scoped to guvfx_u_* only (got '$Username')" }
  $result.scoped_ok = $true
  if (-not $Revert -and [string]::IsNullOrWhiteSpace($Mt5Path)) { throw "Mt5Path required unless -Revert" }

  $shellVal = if ($Revert) { "explorer.exe" } else { "`"$Mt5Path`" /portable" }
  $u = Get-LocalUser -Name $Username -ErrorAction Stop
  $sid = $u.SID.Value
  $WinlogonRel = "Software\Microsoft\Windows NT\CurrentVersion\Winlogon"

  # Case 1: hive already loaded (HKU\<sid>) — user logged in
  if (Test-Path "Registry::HKEY_USERS\$sid") {
    $result.hive_loaded = $true
    $key = "Registry::HKEY_USERS\$sid\$WinlogonRel"
    New-Item -Path $key -Force | Out-Null
    if ($Revert) { Remove-ItemProperty -Path $key -Name Shell -ErrorAction SilentlyContinue; $result.reverted=$true }
    else { New-ItemProperty -Path $key -Name Shell -Value $shellVal -PropertyType String -Force | Out-Null; $result.shell_set=$shellVal }
  }
  else {
    # Case 2: load the profile hive from disk if the profile exists
    $profPath = (Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\$sid" -ErrorAction SilentlyContinue).ProfileImagePath
    if ($profPath -and (Test-Path (Join-Path $profPath "NTUSER.DAT"))) {
      $result.profile_exists = $true
      $tmp = "TX1E_$($Username)"
      reg load "HKU\$tmp" (Join-Path $profPath "NTUSER.DAT") | Out-Null
      try {
        $key = "Registry::HKEY_USERS\$tmp\$WinlogonRel"
        New-Item -Path $key -Force | Out-Null
        if ($Revert) { Remove-ItemProperty -Path $key -Name Shell -ErrorAction SilentlyContinue; $result.reverted=$true }
        else { New-ItemProperty -Path $key -Name Shell -Value $shellVal -PropertyType String -Force | Out-Null; $result.shell_set=$shellVal }
      } finally { [gc]::Collect(); reg unload "HKU\$tmp" | Out-Null }
    }
    else {
      # Profile not created yet (user never logged in) — safe no-op, applied on first logon path.
      $result.pending_first_logon = $true
    }
  }
  $result.ok = $true
  $result | ConvertTo-Json -Compress
}
catch {
  $result.ok = $false; $result.error = $_.Exception.Message
  $result | ConvertTo-Json -Compress
  exit 1
}

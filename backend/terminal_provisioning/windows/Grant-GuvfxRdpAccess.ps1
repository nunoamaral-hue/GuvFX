<#
  TX-1E — grant/revoke RDP logon for a dedicated viewer identity (reversible).

  Adds guvfx_u_<id> to the local "Remote Desktop Users" group so a dedicated RDP
  viewer session is possible. Scoped STRICTLY to guvfx_u_*; never grants admin.
  Reversible with -Revoke. Does not change fDenyTSConnections or any global RDP
  policy, and never touches Administrator.

  NOTE: without the RDS role (forbidden this phase), the host permits only the
  standard 2 concurrent admin-mode sessions; this only authorises the identity —
  it does not change the concurrency ceiling.
#>
param(
  [Parameter(Mandatory=$true)][string]$Username,
  [switch]$Revoke
)
$ErrorActionPreference = "Stop"
$result = [ordered]@{ username=$Username; scoped_ok=$false; in_rdp_users=$false; action=$null; ok=$false }
try {
  if ($Username -notlike "guvfx_u_*") { throw "refused: scoped to guvfx_u_* only (got '$Username')" }
  $result.scoped_ok = $true
  Get-LocalUser -Name $Username -ErrorAction Stop | Out-Null
  $grp = "Remote Desktop Users"
  $isMember = [bool](Get-LocalGroupMember -Group $grp -Member $Username -ErrorAction SilentlyContinue)
  if ($Revoke) {
    if ($isMember) { Remove-LocalGroupMember -Group $grp -Member $Username }
    $result.action = "revoked"
  } else {
    if (-not $isMember) { Add-LocalGroupMember -Group $grp -Member $Username }
    $result.action = "granted"
  }
  $result.in_rdp_users = [bool](Get-LocalGroupMember -Group $grp -Member $Username -ErrorAction SilentlyContinue)
  $result.ok = $true
  $result | ConvertTo-Json -Compress
}
catch { $result.ok=$false; $result.error=$_.Exception.Message; $result | ConvertTo-Json -Compress; exit 1 }

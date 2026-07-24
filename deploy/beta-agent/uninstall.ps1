# CVM-Inc-3 B2/B3P-1 - teardown: remove the beta agent service, its firewall rule, its ACL grants and any
# launch tasks. RETAINS runtime + tombstone data (audit). Leaves Nuno's estate untouched.
# DARK ARTEFACT: RUN ONLY in B3, on the host, as Administrator. Dry-run by default; pass -Apply to remove.
#
# Verification (rollback/uninstall safety): the prior stub only stopped+deleted the service. This removes the
# firewall rule, the service-account ACL grants and any launch task(s) too, so no orphaned rule/grant/task is
# left behind. (B3P-2 adds: drain any RUNNING beta runtimes first, and remove the per-<uuid> runtime identities;
# in B3P-1 no runtime can have been launched yet.)
param(
  [string]$ServiceName = "GuvFXBetaAgent",
  [string]$RuleName    = "GuvFX-Beta-Agent-In",
  [string]$RunAsUser   = "NT SERVICE\GuvFXBetaAgent",
  [string]$AgentDir    = "C:\GuvFX\beta\agent",
  [string]$StateDir    = "C:\GuvFX\beta\agent-state",
  [string]$SlotsRoot   = "C:\GuvFX\beta\slots",
  [string]$BetaTombstones = "C:\GuvFX\beta\tombstones",
  [string]$GoldenDir   = "C:\GuvFX\golden\newMT5",
  [string]$WinSwDir    = "C:\GuvFX\beta\agent-winsw",   # WinSW wrapper dir install_service.ps1 grants + stages
  [string]$VenvDir     = "C:\GuvFX\beta\agent-venv",    # granted RX by install_service.ps1; revoke it too
  [string]$LaunchTaskPrefix = "GuvFXBetaRuntime-",
  [string]$StopTaskPrefix   = "GuvFXBetaRuntimeStop-",
  [string]$IdentityPrefix   = "guvfx_b_slot",
  [int]$PoolSize            = 4,
  # Identities are DISABLED by default, not deleted: deletion orphans anything they own and destroys the
  # ability to attribute retained tombstone evidence. -RemoveIdentities is an explicit operator choice.
  [switch]$RemoveIdentities,
  [switch]$Apply
)
$ErrorActionPreference = "Stop"
# Symmetric with install_pool.ps1: a teardown pointed at another identity namespace is refused up front,
# before it can disable an account or touch a user right.
if ($IdentityPrefix -ne "guvfx_b_slot") {
  throw "refusing: identity prefix must match win_primitives.RUNTIME_IDENTITY_PREFIX"
}
function DoIt($desc, [scriptblock]$block) {
  if ($Apply) { Write-Host "APPLY: $desc"; & $block } else { Write-Host "PLAN:  $desc" }
}

# 0. Revoke the service account's ACL grants BEFORE the service is deleted: once the SCM registration is
#    gone, "NT SERVICE\<name>" may no longer resolve and the ACEs would be orphaned on retained data.
#    Mirrors install_service.ps1's grant set exactly (incl. the WinSW wrapper dir and the venv).
foreach ($d in @($AgentDir, $StateDir, $BetaTombstones, $SlotsRoot, $GoldenDir, $WinSwDir, $VenvDir)) {
  DoIt "revoke '$RunAsUser' ACL grant on $d" {
    if (Test-Path $d) { icacls $d /remove:g "$RunAsUser" | Out-Null }
  }
}

# 1. Stop + delete the service. The service host is a WinSW WRAPPER, not pywin32: prefer WinSW's own
#    stop+uninstall (it signals the child for a graceful drain, then removes the SCM registration). If the
#    staged wrapper is already gone, sc.exe delete still removes the registration (proven in the 2026-07-24
#    recovery). The legacy pywin32 `service.py remove` path no longer applies and is removed.
DoIt "stop + remove service '$ServiceName' (WinSW-aware)" {
  $svcExe = Join-Path $WinSwDir "$ServiceName.exe"
  sc.exe stop $ServiceName 2>$null | Out-Null
  if (Test-Path $svcExe) {
    & $svcExe stop 2>$null | Out-Null
    & $svcExe uninstall 2>$null | Out-Null
  }
  sc.exe delete $ServiceName 2>$null | Out-Null
}

# 1b. Remove the staged WinSW wrapper + config so no orphaned binary or ACE (for the now-deleted virtual
#     account SID) is left on retained data. The agent-winsw dir holds only the exe + xml; WinSW's captured
#     child logs live under agent-state\logs, which is deliberately RETAINED as evidence.
DoIt "remove staged WinSW dir $WinSwDir" {
  if (Test-Path $WinSwDir) { Remove-Item -Recurse -Force $WinSwDir }
}

# 2. Remove the firewall rule (leaves :8788/:8787 rules untouched).
DoIt "remove firewall rule '$RuleName'" {
  if (Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue) {
    Remove-NetFirewallRule -DisplayName $RuleName
  }
}

# 3. Remove BOTH task families. The B2 version removed only the launch prefix, which left the terminate
#    tasks - and their stored credentials - behind (install-only review F4). Unregistering a task removes
#    its credential with it.
foreach ($prefix in @($LaunchTaskPrefix, $StopTaskPrefix)) {
  DoIt "unregister tasks '$prefix*'" {
    Get-ScheduledTask -TaskName "$prefix*" -ErrorAction SilentlyContinue |
      Unregister-ScheduledTask -Confirm:$false
  }
}

# 5. Remove each slot identity's grants, revoke SeBatchLogonRight, and disable (not delete) the account.
#    SIDs are collected FIRST: Remove-LocalUser inside the loop would make them unresolvable by the time the
#    revoke block runs, so the right would silently keep four orphaned SIDs - and a future account created
#    with the same RID would inherit a batch-logon grant nobody intended. The revoke (step 6) consumes this
#    list, so it works whether the accounts were disabled, deleted, or left alone.
$SlotIdentities = @()
for ($n = 1; $n -le $PoolSize; $n++) {
  $u = Get-LocalUser -Name "$IdentityPrefix$n" -ErrorAction SilentlyContinue
  if ($u) { $SlotIdentities += [pscustomobject]@{ Name = $u.Name; Sid = $u.SID } }
}
for ($n = 1; $n -le $PoolSize; $n++) {
  $user = "$IdentityPrefix$n"
  if (-not (Get-LocalUser -Name $user -ErrorAction SilentlyContinue)) { continue }
  foreach ($d in @((Join-Path $SlotsRoot "$n"), $GoldenDir)) {
    DoIt "revoke '$user' ACL grant on $d" {
      if (Test-Path $d) { icacls $d /remove:g "$user" | Out-Null }
    }
  }
  DoIt "disable identity '$user' (NOT deleted unless -RemoveIdentities)" {
    Disable-LocalUser -Name $user
  }
  if ($RemoveIdentities) {
    DoIt "DELETE identity '$user' (explicitly requested)" { Remove-LocalUser -Name $user }
  }
}
# 6. Revoke SeBatchLogonRight via the LSA policy API - NOT secedit.
#    LsaRemoveAccountRights removes ONE right from ONE account. It cannot narrow anyone else's rights,
#    which is what made the secedit rewrite dangerous: that path rebuilt the complete machine-wide
#    assignment line by string-filtering, so any parse imperfection silently removed principals unrelated
#    to beta. There is no such line here.
if (-not ('GuvfxLsaU' -as [type])) {
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class GuvfxLsaU {
  [StructLayout(LayoutKind.Sequential)]
  public struct LSA_UNICODE_STRING { public ushort Length; public ushort MaximumLength; public IntPtr Buffer; }
  [StructLayout(LayoutKind.Sequential)]
  public struct LSA_OBJECT_ATTRIBUTES {
    public int Length; public IntPtr RootDirectory; public IntPtr ObjectName;
    public int Attributes; public IntPtr SecurityDescriptor; public IntPtr SecurityQualityOfService; }
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern uint LsaOpenPolicy(IntPtr SystemName, ref LSA_OBJECT_ATTRIBUTES oa, uint access, out IntPtr handle);
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern uint LsaRemoveAccountRights(IntPtr handle, byte[] sid,
    [MarshalAs(UnmanagedType.U1)] bool allRights, LSA_UNICODE_STRING[] rights, uint count);
  [DllImport("advapi32.dll", SetLastError=true)]
  public static extern uint LsaEnumerateAccountRights(IntPtr handle, byte[] sid, out IntPtr rights, out uint count);
  [DllImport("advapi32.dll")] public static extern uint LsaClose(IntPtr handle);
  [DllImport("advapi32.dll")] public static extern uint LsaFreeMemory(IntPtr buffer);
  [DllImport("advapi32.dll")] public static extern int LsaNtStatusToWinError(uint status);
}
'@ -ErrorAction Stop
}   # guarded: PLAN then APPLY in one console must not die on 'type already exists'

$GuvfxRight = "SeBatchLogonRight"
# PowerShell parses an 8-hex-digit literal as Int32, so 0xC0000034 WRAPS to -1073741772 while the LSA
# return is UInt32 3221225524 - the comparison would be False for ever, turning the benign "this account
# holds no rights" status into a hard failure. [uint32]0xC0000034 does not help: the literal has already
# wrapped, and the cast then throws. The decimal form is the only one that survives.
$STATUS_OBJECT_NAME_NOT_FOUND = [uint32]3221225524   # 0xC0000034 STATUS_OBJECT_NAME_NOT_FOUND

function Get-ApprovedSlotSidBytesU {
  param([Parameter(Mandatory)][string]$AccountName, [Parameter(Mandatory)]$Sid)
  # Uninstall must work for accounts that are about to be, or have been, disabled - so the SID is captured
  # BEFORE the loop and passed in. The namespace guard is unchanged: this function cannot be pointed at
  # Administrators or any principal outside the beta-slot namespace.
  if ($AccountName -notmatch '^guvfx_b_slot[1-9][0-9]*$') {
    throw "refusing user-right operation on '$AccountName': outside the beta-slot identity namespace"
  }
  $bytes = New-Object byte[] $Sid.BinaryLength
  $Sid.GetBinaryForm($bytes, 0)
  return $bytes
}

function Open-GuvfxLsaPolicyU {
  param([uint32]$Access)
  $oa = New-Object GuvfxLsaU+LSA_OBJECT_ATTRIBUTES
  $oa.Length = [Runtime.InteropServices.Marshal]::SizeOf($oa)
  $h = [IntPtr]::Zero
  $st = [GuvfxLsaU]::LsaOpenPolicy([IntPtr]::Zero, [ref]$oa, $Access, [ref]$h)
  if ($st -ne 0) { throw "LsaOpenPolicy failed: NTSTATUS 0x$('{0:X8}' -f $st)" }
  return $h
}

function Get-GuvfxAccountRightsU {
  param([byte[]]$SidBytes)
  $h = Open-GuvfxLsaPolicyU -Access 0x00000801
  try {
    $ptr = [IntPtr]::Zero; $count = [uint32]0
    $st = [GuvfxLsaU]::LsaEnumerateAccountRights($h, $SidBytes, [ref]$ptr, [ref]$count)
    if ($st -eq $STATUS_OBJECT_NAME_NOT_FOUND) { return @() }
    if ($st -ne 0) { throw "LsaEnumerateAccountRights failed: NTSTATUS 0x$('{0:X8}' -f $st)" }
    $out = @()
    $size = [Runtime.InteropServices.Marshal]::SizeOf([type][GuvfxLsaU+LSA_UNICODE_STRING])
    for ($i = 0; $i -lt $count; $i++) {
      $item = [Runtime.InteropServices.Marshal]::PtrToStructure(
                [IntPtr]($ptr.ToInt64() + ($i * $size)), [type][GuvfxLsaU+LSA_UNICODE_STRING])
      $out += [Runtime.InteropServices.Marshal]::PtrToStringUni($item.Buffer, $item.Length / 2)
    }
    [void][GuvfxLsaU]::LsaFreeMemory($ptr)
    return $out
  } finally { [void][GuvfxLsaU]::LsaClose($h) }
}

# F3: a revoke that finds no resolvable identity is a SILENT NO-OP - indistinguishable from a clean
# teardown while four SIDs keep the right for ever, inheritable by a future account with the same RID.
# Say so loudly rather than printing the usual epilogue.
# Null-safe by construction, as DEFENCE IN DEPTH - not because a live defect was found here. $SlotIdentities
# is initialised to @() above and only ever appended to, so it cannot be $null today and `@($x).Count` was
# correct. It is written this way because @($null).Count is 1: if a later edit ever let this variable be
# $null, "no identity resolved" would count as one and this warning - the one that says four SIDs may keep
# the right for ever - would be skipped SILENTLY. The 2026-07-23 APPLY failure was the same idiom in
# install_pool.ps1, where the value genuinely could be $null.
$slotIdentityCount = if ($null -eq $SlotIdentities) { 0 } else { @($SlotIdentities).Count }
if ($slotIdentityCount -lt $PoolSize) {
  $found = @($SlotIdentities | ForEach-Object { $_.Name })
  for ($n = 1; $n -le $PoolSize; $n++) {
    if ($found -notcontains "$IdentityPrefix$n") {
      Write-Host "WARNING: '$IdentityPrefix$n' could not be resolved - its $GuvfxRight grant CANNOT be verified as revoked."
      Write-Host "         If that account was deleted while still holding the right, the grant is orphaned on its SID."
      Write-Host "         Recover the SID from the install evidence and revoke it explicitly before reusing the pool."
    }
  }
}

foreach ($entry in $SlotIdentities) {
  $name = $entry.Name
  $slotSidBytes = Get-ApprovedSlotSidBytesU -AccountName $name -Sid $entry.Sid
  DoIt "revoke $GuvfxRight from '$name' (LSA; no other principal is touched)" {
    $before = Get-GuvfxAccountRightsU -SidBytes $slotSidBytes
    if ($before -notcontains $GuvfxRight) {
      Write-Host "evidence right=$GuvfxRight sid=$($entry.Sid.Value) account=$name op=remove result=not_held"
    } else {
      $h = Open-GuvfxLsaPolicyU -Access 0x00000811
      try {
        $u = New-Object GuvfxLsaU+LSA_UNICODE_STRING
        $u.Buffer        = [Runtime.InteropServices.Marshal]::StringToHGlobalUni($GuvfxRight)
        $u.Length        = [uint16]($GuvfxRight.Length * 2)
        $u.MaximumLength = [uint16](($GuvfxRight.Length + 1) * 2)
        # allRights = $false: remove ONLY the named right, never every right the account holds.
        $st = [GuvfxLsaU]::LsaRemoveAccountRights($h, $slotSidBytes, $false, @($u), 1)
        [Runtime.InteropServices.Marshal]::FreeHGlobal($u.Buffer)
        if ($st -ne 0) {
          Write-Host "evidence right=$GuvfxRight sid=$($entry.Sid.Value) account=$name op=remove result=failed ntstatus=0x$('{0:X8}' -f $st)"
          throw "LsaRemoveAccountRights failed for $name : NTSTATUS 0x$('{0:X8}' -f $st)"
        }
      } finally { [void][GuvfxLsaU]::LsaClose($h) }
      $after = Get-GuvfxAccountRightsU -SidBytes $slotSidBytes
      if ($after -contains $GuvfxRight) { throw "post-check failed: $name still holds $GuvfxRight" }
      foreach ($r in $before) {
        if ($r -ne $GuvfxRight -and $after -notcontains $r) { throw "user-right regression: $name lost '$r'" }
      }
      Write-Host "evidence right=$GuvfxRight sid=$($entry.Sid.Value) account=$name op=remove result=revoked other_rights_preserved=$($after.Count)"
    }
  }
}

Write-Host ""
Write-Host "RETAINED (never deleted): slot dirs under $SlotsRoot, tombstones under $BetaTombstones,"
Write-Host "                          and $StateDir (nonce/idempotency/slot/audit stores = the evidence chain)."
Write-Host "UNTOUCHED: Nuno's terminal (Session 3), bridge (:8788), :8787, autologon, startup tasks."
if (-not $Apply) { Write-Host "PLAN complete. Re-run with -Apply on the host to perform the teardown." }

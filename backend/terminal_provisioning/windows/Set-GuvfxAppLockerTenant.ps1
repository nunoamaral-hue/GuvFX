<#
  Stream 6 (M1) - tenant-safe AppLocker MERGE / REMOVE (additive, isolated, idempotent, reversible).

  The certified base policy (Set-GuvfxAppLocker.ps1) stays untouched. For account N this op adds ONLY N's
  shell/escape DENY rules, scoped to guvfx_u_<N>'s SID, with deterministic rule Ids tagged with the account:
      <accountId:08x>-0000-4d54-0000-<seq:012x>
  (the '4d54' 3rd group marks a GuvFX tenant rule; the 1st group is the account id). This exactly mirrors
  hosted_workspace.applocker_policy (the authoritative Python model + tests).

    -Mode Merge  : Set-AppLockerPolicy -Merge with N's fragment -> ADDS N's denies; touches no other rule.
    -Mode Remove : get the effective policy, strip ONLY N's tenant rules, re-apply -> removes N's contribution
                   without wiping the machine policy or any other tenant. REFUSES Customer Zero (account 1).

  Never replaces the whole policy, never removes Customer Zero or another account's rules, never resets
  TSAppAllowList. ASCII-only (RULE 9). Emits a single compact JSON object.

  Usage:
    powershell -NoProfile -File Set-GuvfxAppLockerTenant.ps1 -Mode Merge  -AccountId 14 -HostedUser guvfx_u_14
    powershell -NoProfile -File Set-GuvfxAppLockerTenant.ps1 -Mode Remove -AccountId 14 -HostedUser guvfx_u_14
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("Merge","Remove")][string]$Mode,
  [Parameter(Mandatory=$true)][int]$AccountId,
  [Parameter(Mandatory=$true)][string]$HostedUser
)
$ErrorActionPreference = "Stop"
$MARKER = "4d54"
$DENY = @("cmd.exe","powershell.exe","powershell_ise.exe","pwsh.exe","explorer.exe","regedit.exe",
          "mmc.exe","taskmgr.exe","wscript.exe","cscript.exe","mshta.exe","control.exe")
$result = [ordered]@{ mode=$Mode; account_id=$AccountId; hosted_user=$HostedUser; merged=0; removed=0; ok=$false; reason="" }
function Fail([string]$why) { $result.ok=$false; $result.reason=$why; $result | ConvertTo-Json -Compress; exit 1 }
function TenantId([int]$acct,[int]$seq) { return ('{0:x8}-0000-{1}-0000-{2:x12}' -f $acct, $MARKER, $seq) }
function IsTenantRule([string]$id,[int]$acct) {
  $p = $id.Split('-'); if ($p.Count -ne 5 -or $p[2].ToLower() -ne $MARKER) { return $false }
  return ([Convert]::ToInt64($p[0],16) -eq $acct)
}

try {
  if ($AccountId -le 0) { Fail "account_id out of range" }
  if ($HostedUser -notmatch "^guvfx_u_[1-9][0-9]*$") { Fail "not a hosted identity" }
  # Identity<->account binding: HostedUser MUST be guvfx_u_<AccountId>.
  if ($HostedUser -ne ("guvfx_u_" + $AccountId)) { Fail "hosted_user does not match account_id" }

  if ($Mode -eq "Remove") {
    if ($AccountId -eq 1) { Fail "refusing: Customer Zero removal forbidden" }
    [xml]$pol = (Get-AppLockerPolicy -Effective -Xml)
    $removed = 0
    foreach ($coll in @($pol.AppLockerPolicy.RuleCollection)) {
      foreach ($rule in @($coll.ChildNodes)) {
        if ($rule.Id -and (IsTenantRule $rule.Id $AccountId)) { [void]$coll.RemoveChild($rule); $removed++ }
      }
    }
    $tmp = Join-Path $env:TEMP ("guvfx-applocker-" + $AccountId + ".xml")
    $pol.Save($tmp)
    Set-AppLockerPolicy -XmlPolicy $tmp
    $result.removed = $removed; $result.ok = $true; $result.reason = "removed"
    $result | ConvertTo-Json -Compress; return
  }

  # Mode Merge: resolve the SID and build N's fragment, then -Merge (additive).
  $sid = (Get-LocalUser -Name $HostedUser).SID.Value
  if ([string]::IsNullOrWhiteSpace($sid)) { Fail "could not resolve SID for $HostedUser" }
  if ($sid -eq "S-1-1-0" -or $sid -eq "S-1-5-32-544") { Fail "refusing: shared principal SID" }

  $rules = New-Object System.Text.StringBuilder
  for ($i = 0; $i -lt $DENY.Count; $i++) {
    $id = TenantId $AccountId (16 + $i)
    [void]$rules.Append(('<FilePathRule Id="{0}" Name="(Hosted acct {1}) Deny {2}" Description="acct={1}" UserOrGroupSid="{3}" Action="Deny"><Conditions><FilePathCondition Path="*\{2}" /></Conditions></FilePathRule>' -f $id, $AccountId, $DENY[$i], $sid))
  }
  # EnforcementMode="NotConfigured" so the -Merge contributes N's rules WITHOUT changing the target Exe
  # collection's mode (never downgrades Customer Zero's Enforced collection to audit). N's denies are evaluated
  # under whatever mode the machine collection already carries.
  $frag = '<AppLockerPolicy Version="1"><RuleCollection Type="Exe" EnforcementMode="NotConfigured">' + $rules.ToString() + '</RuleCollection></AppLockerPolicy>'
  $tmp = Join-Path $env:TEMP ("guvfx-applocker-tenant-" + $AccountId + ".xml")
  $frag | Out-File -FilePath $tmp -Encoding ASCII
  Set-AppLockerPolicy -Merge -XmlPolicy $tmp

  # Read back: exactly this account's tenant rules present.
  [xml]$eff = (Get-AppLockerPolicy -Effective -Xml)
  $present = 0
  foreach ($coll in @($eff.AppLockerPolicy.RuleCollection)) {
    foreach ($rule in @($coll.ChildNodes)) { if ($rule.Id -and (IsTenantRule $rule.Id $AccountId)) { $present++ } }
  }
  $result.merged = $present
  if ($present -lt $DENY.Count) { Fail "read-back did not confirm the tenant fragment merged" }
  $result.ok = $true; $result.reason = "merged"
  $result | ConvertTo-Json -Compress
}
catch { $result.ok=$false; $result.reason="error"; $result | ConvertTo-Json -Compress; exit 1 }

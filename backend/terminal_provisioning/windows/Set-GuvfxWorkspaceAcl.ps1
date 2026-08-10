<#
  G5 - GuvFX per-user hosted-workspace NTFS ACL engine (idempotent, reversible, read-back verified).

  Closes the ADR-0033 cross-tenant hard blocker: the TX-1 per-user runtime tree
  (C:\GuvFX\accounts\<id>) is created with folders but no explicit ACL, so it inherits BUILTIN\Users read
  and a second hosted identity could read another customer's accounts.dat. This op breaks inheritance and
  grants ONLY SYSTEM + Administrators (Full) + the workspace user guvfx_u_<id> (Modify), then reads the DACL
  back AS SIDs and proves the exact three-principal set. On any read-back mismatch it restores the snapshot
  and fails closed.

  ASCII-ONLY by construction (RULE 9): written with plain ASCII so it parses identically under Windows
  PowerShell 5.1 with or without a BOM. The caller MUST ParseFile() it before first execution.

  This is a fixed-slot host primitive: it is handed ONLY a slot identity (guvfx_u_<id>) and a fixed
  runtime_root under the accounts base. It knows nothing of workspace UUIDs, generations or jobs.

  Usage:
    powershell -NoProfile -File Set-GuvfxWorkspaceAcl.ps1 -Mode Apply    -Username guvfx_u_14 `
        -RuntimeRoot 'C:\GuvFX\accounts\14' -SnapshotPath 'C:\GuvFX\accounts\14\audit\acl_snapshot.sddl'
    powershell -NoProfile -File Set-GuvfxWorkspaceAcl.ps1 -Mode Verify   -Username guvfx_u_14 -RuntimeRoot ...
    powershell -NoProfile -File Set-GuvfxWorkspaceAcl.ps1 -Mode Rollback -Username guvfx_u_14 -RuntimeRoot ... -SnapshotPath ...

  Emits a single compact JSON object: { ok, action, user_sid, protected, rows:[{sid,type,rights,inherited}], reason }.
  rows/user_sid/protected feed hosted_workspace.workspace_acl.verify_workspace_acl (the authoritative verdict).
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("Apply","Verify","Rollback")][string]$Mode,
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [string]$SnapshotPath
)

$ErrorActionPreference = "Stop"
$SYSTEM_SID = "S-1-5-18"
$ADMIN_SID  = "S-1-5-32-544"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"

$result = [ordered]@{
  ok = $false; action = $Mode; user_sid = ""; protected = $false; rows = @(); reason = ""
}

function Fail([string]$why) {
  $result.ok = $false; $result.reason = $why
  $result | ConvertTo-Json -Compress -Depth 5
  exit 1
}

function Read-DaclRows([string]$path) {
  # Read the DACL back AS SIDs (a name lookup here would re-introduce the failure mode install_pool documents).
  $acl = Get-Acl -Path $path
  $rows = @()
  foreach ($ace in $acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
    $rows += [ordered]@{
      sid       = $ace.IdentityReference.Value
      type      = $ace.AccessControlType.ToString()
      rights    = $ace.FileSystemRights.ToString()
      inherited = [bool]$ace.IsInherited
    }
  }
  return @{ protected = [bool]$acl.AreAccessRulesProtected; rows = $rows }
}

try {
  # -- Refuse anything that is not a hosted guvfx_u_<id> identity under the accounts base (no admin, no traversal).
  if ($Username -notmatch "^guvfx_u_[1-9][0-9]*$") { Fail "refusing: not a hosted guvfx_u_<id> identity" }
  $full = [System.IO.Path]::GetFullPath($RuntimeRoot)
  if ($full -like "*..*") { Fail "refusing: path traversal in runtime_root" }
  if (-not $full.ToLower().StartsWith(($ACCOUNTS_BASE.ToLower() + "\"))) {
    Fail "refusing: runtime_root outside the hosted accounts base"
  }
  if (-not (Test-Path -LiteralPath $full)) { Fail "runtime_root does not exist" }

  # -- Resolve the workspace user SID (value exists before any grant; refuse if it cannot resolve).
  try {
    $userSid = (New-Object System.Security.Principal.NTAccount($Username)).Translate(
                 [System.Security.Principal.SecurityIdentifier]).Value
  } catch { Fail "could not resolve SID for $Username" }
  $result.user_sid = $userSid

  if ($Mode -eq "Rollback") {
    if (-not $SnapshotPath -or -not (Test-Path -LiteralPath $SnapshotPath)) { Fail "no snapshot to roll back to" }
    $saved = (Get-Content -LiteralPath $SnapshotPath -Raw).Trim()
    if (-not $saved) { Fail "empty snapshot" }
    $sd = New-Object System.Security.AccessControl.DirectorySecurity
    $sd.SetSecurityDescriptorSddlForm($saved)
    Set-Acl -Path $full -AclObject $sd
    $back = Read-DaclRows $full
    $result.protected = $back.protected; $result.rows = $back.rows
    $result.ok = $true; $result.reason = "rolled_back"
    $result | ConvertTo-Json -Compress -Depth 5
    return
  }

  if ($Mode -eq "Verify") {
    $back = Read-DaclRows $full
    $result.protected = $back.protected; $result.rows = $back.rows
    $result.ok = $true; $result.reason = "read_back"
    $result | ConvertTo-Json -Compress -Depth 5
    return
  }

  # -- Mode Apply ------------------------------------------------------------------------------------------
  if (-not $SnapshotPath) { Fail "Apply requires -SnapshotPath (for reversible rollback)" }

  # Snapshot the current DACL for rollback BEFORE mutating anything.
  $snapDir = Split-Path -Parent $SnapshotPath
  if ($snapDir -and -not (Test-Path -LiteralPath $snapDir)) {
    New-Item -ItemType Directory -Force -Path $snapDir | Out-Null
  }
  (Get-Acl -Path $full).Sddl | Set-Content -LiteralPath $SnapshotPath -Encoding Ascii -NoNewline

  # Rebuild the DACL to EXACTLY three principals via a PROTECTED descriptor (not additive icacls): disable
  # inheritance WITHOUT copying inherited ACEs, remove every pre-existing explicit ACE, then add SID-typed
  # SYSTEM(F), Administrators(F), user(M). Additive `icacls /grant` would leave a pre-existing explicit
  # BUILTIN\Users read in place; rebuilding from a cleared, protected DACL makes that impossible.
  $acl = Get-Acl -Path $full
  $acl.SetAccessRuleProtection($true, $false)
  foreach ($rule in @($acl.GetAccessRules($true, $false, [System.Security.Principal.SecurityIdentifier]))) {
    [void]$acl.RemoveAccessRule($rule)
  }
  $inh   = [System.Security.AccessControl.InheritanceFlags]"ContainerInherit, ObjectInherit"
  $prop  = [System.Security.AccessControl.PropagationFlags]::None
  $allow = [System.Security.AccessControl.AccessControlType]::Allow
  $rFull   = [System.Security.AccessControl.FileSystemRights]::FullControl
  $rModify = [System.Security.AccessControl.FileSystemRights]::Modify
  foreach ($grant in @(,@($SYSTEM_SID, $rFull)) + @(,@($ADMIN_SID, $rFull)) + @(,@($userSid, $rModify))) {
    $sidObj = New-Object System.Security.Principal.SecurityIdentifier($grant[0])
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
        $sidObj, $grant[1], $inh, $prop, $allow)))
  }
  Set-Acl -Path $full -AclObject $acl

  $back = Read-DaclRows $full
  $result.protected = $back.protected; $result.rows = $back.rows

  # RULE 11 positive + negative control: the read-back parser MUST see the two principals we just granted by SID
  # AND MUST NOT see any Allow principal outside the exact target set. Either a missing known-present SID (a
  # false clean) or an unexpected principal (a leak) restores the snapshot and refuses to certify.
  $seen = @($back.rows | Where-Object { $_.type -eq "Allow" } | ForEach-Object { $_.sid })
  $expected = @($SYSTEM_SID, $ADMIN_SID, $userSid)
  $extra = @($seen | Where-Object { $expected -notcontains $_ })
  if (($seen -notcontains $userSid) -or ($seen -notcontains $ADMIN_SID) -or ($extra.Count -ne 0)) {
    # Restore and fail: measurement path did not observe exactly the target set.
    $saved = (Get-Content -LiteralPath $SnapshotPath -Raw).Trim()
    if ($saved) {
      $sd = New-Object System.Security.AccessControl.DirectorySecurity
      $sd.SetSecurityDescriptorSddlForm($saved); Set-Acl -Path $full -AclObject $sd
    }
    Fail "read-back control failed (missing or unexpected principal) - refusing to certify the ACL"
  }

  $result.ok = $true; $result.reason = "applied"
  $result | ConvertTo-Json -Compress -Depth 5
}
catch {
  $result.ok = $false; $result.reason = "error"
  $result | ConvertTo-Json -Compress -Depth 5
  exit 1
}

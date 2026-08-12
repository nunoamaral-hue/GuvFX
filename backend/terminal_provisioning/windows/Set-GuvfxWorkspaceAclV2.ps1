<#
  G5v2 - GuvFX hosted-workspace INVERTED (W^X) NTFS ACL applier (ADR-0043). Idempotent, reversible, read-back.

  The canonical invariant TENANT-WRITABLE => NON-EXECUTABLE is enforced at the NTFS layer here:
    - break inheritance on the runtime root;
    - root (and, by inheritance, the MQL5 CODE dirs): SYSTEM Full, Administrators Full, tenant Read+Execute ONLY;
    - each enumerated DATA subdir: tenant Modify granted back (write allowed there; AppLocker execute-denies it);
    - config\common.ini + each code dir: an explicit tenant DENY (Write/Append/Delete) ACE.
  Then read the DACL back AS SIDs for the root + every listed subdir and emit it for the authoritative Python
  verifier hosted_workspace.workspace_acl.verify_workspace_acl_v2. On any read-back failure the caller restores
  the snapshot (-Mode Rollback) and fails closed.

  Decision 2 (single source of truth): this script is a DUMB applier - it NEVER embeds the policy. The backend
  (build_workspace_acl_plan_v2, from the canonical HOSTED_WRITABLE_SUBDIRS/HOSTED_CODE_SUBDIRS) passes the exact
  relative paths as JSON, so NTFS and AppLocker can never diverge.

  ASCII-ONLY by construction (RULE 9): parses identically under Windows PowerShell 5.1 with or without a BOM.
  The caller MUST ParseFile() it before first execution:
    [System.Management.Automation.Language.Parser]::ParseFile('Set-GuvfxWorkspaceAclV2.ps1',[ref]$null,[ref]$null)

  This is a fixed-slot host primitive: handed ONLY a slot identity (guvfx_u_<id>) + a fixed runtime_root under the
  accounts base + the plan paths. It knows nothing of workspace UUIDs, generations or jobs (win_ops boundary).

  Usage:
    powershell -NoProfile -File Set-GuvfxWorkspaceAclV2.ps1 -Mode Apply -Username guvfx_u_14 `
        -RuntimeRoot 'C:\GuvFX\accounts\14' -WritableSubdirs '["terminal\\config",...]' `
        -CodeDenyPaths '["terminal\\MQL5\\Experts",...,"terminal\\config\\common.ini"]' `
        -SnapshotPath 'C:\GuvFX\accounts\14\audit\acl_v2_snapshot.sddl'
    -Mode Verify    (read-back only)   |   -Mode Rollback  (restore snapshot)

  Emits one compact JSON: { ok, action, user_sid, path_dacls:{ "<rel>": [{sid,type,rights,inherited}], "":[...] },
  reason }. path_dacls (with "" = root) feeds verify_workspace_acl_v2.
#>
param(
  [Parameter(Mandatory=$true)][ValidateSet("Apply","Verify","Rollback")][string]$Mode,
  [Parameter(Mandatory=$true)][string]$Username,
  [Parameter(Mandatory=$true)][string]$RuntimeRoot,
  [string]$WritableSubdirs = "[]",
  [string]$CodeDenyPaths = "[]",
  [string]$SnapshotPath
)
$ErrorActionPreference = "Stop"
$USERNAME_RE = "^guvfx_u_[1-9][0-9]*$"

function Fail($reason) { [ordered]@{ ok=$false; action=$Mode; reason="$reason" } | ConvertTo-Json -Compress -Depth 6; exit 1 }

function Resolve-Sid($name) {
  try { return (New-Object System.Security.Principal.NTAccount($name)).Translate([System.Security.Principal.SecurityIdentifier]).Value }
  catch { return $null }
}

function Read-Dacl($path) {
  $acl = Get-Acl -LiteralPath $path
  $rows = @()
  foreach ($ace in $acl.Access) {
    $sid = $ace.IdentityReference
    try { $sid = $ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $sid = "$($ace.IdentityReference)" }
    $rows += [ordered]@{ sid="$sid"; type="$($ace.AccessControlType)"; rights="$($ace.FileSystemRights)"; inherited=[bool]$ace.IsInherited }
  }
  return ,$rows
}

try {
  if ($Username -notmatch $USERNAME_RE) { Fail "not_a_hosted_identity" }
  $acctId = ($Username -split "_")[-1]
  $root = ($RuntimeRoot -replace "/","\").TrimEnd("\")
  if ($root -match "\.\.") { Fail "path_traversal" }
  $expected = "C:\GuvFX\accounts\$acctId"
  if ($root.ToLower() -ne $expected.ToLower()) { Fail "runtime_root_identity_mismatch" }
  if (-not (Test-Path -LiteralPath $root)) { Fail "runtime_root_absent" }
  $userSid = Resolve-Sid $Username
  if (-not $userSid) { Fail "user_sid_unresolved" }
  $writable = @($WritableSubdirs | ConvertFrom-Json)
  $codeDeny = @($CodeDenyPaths | ConvertFrom-Json)

  if ($Mode -eq "Rollback") {
    if (-not ($SnapshotPath -and (Test-Path -LiteralPath $SnapshotPath))) { Fail "no_snapshot" }
    $snap = Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json
    $restored = @()
    # Restore every captured path's SDDL (root + all mutated children), not only the root.
    foreach ($prop in $snap.PSObject.Properties) {
      $rel = $prop.Name
      $p = if ($rel -eq "") { $root } else { Join-Path $root $rel }
      if (Test-Path -LiteralPath $p) {
        $acl = Get-Acl -LiteralPath $p
        $acl.SetSecurityDescriptorSddlForm("$($prop.Value)")
        Set-Acl -LiteralPath $p -AclObject $acl
        $restored += $rel
      }
    }
    [ordered]@{ ok=$true; action="Rollback"; user_sid=$userSid; restored=$restored; reason="restored" } | ConvertTo-Json -Compress -Depth 6
    exit 0
  }

  if ($Mode -eq "Apply") {
    # Apply is fail-CLOSED on a missing snapshot target: it mutates the whole tree, so a rollback artifact MUST
    # exist first (else a failed step leaves an irreversibly re-ACL'd tree). Refuse before any mutation.
    if (-not $SnapshotPath) { Fail "apply_requires_snapshot" }
    # Snapshot the SDDL of EVERY path Apply mutates (root + each writable subdir + each code/common.ini path), once,
    # so -Mode Rollback can restore them ALL, not only the root. "" = the runtime root.
    if (-not (Test-Path -LiteralPath $SnapshotPath)) {
      New-Item -ItemType Directory -Path (Split-Path $SnapshotPath) -Force | Out-Null
      $snap = [ordered]@{}
      $snap[""] = (Get-Acl -LiteralPath $root).Sddl
      foreach ($rel in ($writable + $codeDeny)) {
        $p = Join-Path $root $rel
        if (Test-Path -LiteralPath $p) { $snap[$rel] = (Get-Acl -LiteralPath $p).Sddl }
      }
      ($snap | ConvertTo-Json -Compress -Depth 6) | Out-File -FilePath $SnapshotPath -Encoding ASCII
    }
    $sys  = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-18")
    $admn = New-Object System.Security.Principal.SecurityIdentifier("S-1-5-32-544")
    $usid = New-Object System.Security.Principal.SecurityIdentifier($userSid)
    $CI_OI = [System.Security.AccessControl.InheritanceFlags]"ContainerInherit,ObjectInherit"
    $NONE  = [System.Security.AccessControl.PropagationFlags]::None
    # Root: break inheritance, protected; SYSTEM/Admins Full; tenant Read+Execute ONLY.
    $acl = Get-Acl -LiteralPath $root
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($r in @($acl.Access)) { [void]$acl.RemoveAccessRule($r) }
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sys,  "FullControl",   $CI_OI, $NONE, "Allow")))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($admn, "FullControl",   $CI_OI, $NONE, "Allow")))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($usid, "ReadAndExecute", $CI_OI, $NONE, "Allow")))
    Set-Acl -LiteralPath $root -AclObject $acl
    # Data subdirs: grant tenant Modify back.
    foreach ($rel in $writable) {
      $p = Join-Path $root $rel
      if (Test-Path -LiteralPath $p) {
        $a = Get-Acl -LiteralPath $p
        $a.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($usid, "Modify", $CI_OI, $NONE, "Allow")))
        Set-Acl -LiteralPath $p -AclObject $a
      }
    }
    # Code dirs + common.ini: explicit tenant DENY Write/Append/Delete (immutable AllowDllImport, no .ex5 plant).
    foreach ($rel in $codeDeny) {
      $p = Join-Path $root $rel
      if (Test-Path -LiteralPath $p) {
        $a = Get-Acl -LiteralPath $p
        $a.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($usid, "Write,AppendData,Delete", $CI_OI, $NONE, "Deny")))
        Set-Acl -LiteralPath $p -AclObject $a
      }
    }
  }

  # Verify (also the tail of Apply): read back root + every listed path AS SIDs for the Python verifier.
  $dacls = [ordered]@{}
  $dacls[""] = Read-Dacl $root
  foreach ($rel in ($writable + $codeDeny)) {
    $p = Join-Path $root $rel
    if (Test-Path -LiteralPath $p) { $dacls[$rel] = Read-Dacl $p } else { Fail "path_absent:$rel" }
  }
  [ordered]@{ ok=$true; action=$Mode; user_sid=$userSid; path_dacls=$dacls; reason="read_back" } | ConvertTo-Json -Compress -Depth 8
}
catch {
  [ordered]@{ ok=$false; action=$Mode; reason=("$($_.Exception.Message)") } | ConvertTo-Json -Compress -Depth 6
  exit 1
}

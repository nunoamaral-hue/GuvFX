<#
  Verify-GuvfxNativeLauncher.ps1  (P0 native single-instance launcher gate - READ-ONLY)

  Read-only verification that the certified native launcher is safe to be a tenant's RemoteApp start-program.
  The launcher (C:\GuvFX\launcher\guvfx_launch.exe) is the interactive-session-certified single-instance MT5
  launch guard: it derives the tenant identity from the Windows token (never an argument), refuses reserved ids
  {1,18}, and makes a browser refresh / reconnect IDEMPOTENT (one tenant terminal64 /portable) instead of forking
  a duplicate that stalls onboarding at "Detecting your account...". This gate lets prepare_hosted_slot FAIL
  CLOSED (PREP_LAUNCHER_FAILED) if the launcher is absent, tampered, tenant-writable, or not AppLocker-allow-
  listed, so no tenant is ever pointed at an unsafe launcher.

  Verifies, mutating NOTHING (no launch, no login, no file/registry change):
    - launcher_exists          : the launcher exe is present at the fixed non-tenant path;
    - sha256_matches           : its SHA256 equals the pinned launcher manifest (.guvfx_launcher_manifest);
    - acl_safe                 : no Allow ACE grants write to Users/Everyone/any non-admin (SYSTEM/Admins only +
                                 Users Read+Execute) -- the tenant cannot replace the launcher binary;
    - applocker_allow_present  : an AppLocker ALLOW rule for the launcher is present (deny-by-default);
    - runtime_exists           : the tenant runtime terminal64.exe exists under -TerminalRoot.

  The manifest is the single source of truth for the certified launcher hash (first non-empty, non-comment line
  of ``C:\GuvFX\launcher\.guvfx_launcher_manifest`` -- self-describing, next to the launcher). ASCII-only
  (RULE 9); ParseFile()-validate before first host execution. Emits one compact JSON object.
#>
param(
  [Parameter(Mandatory = $true)][string]$TerminalRoot
)
$ErrorActionPreference = "Stop"
$LAUNCHER = "C:\GuvFX\launcher\guvfx_launch.exe"
$MANIFEST = "C:\GuvFX\launcher\.guvfx_launcher_manifest"
$ALLOW_RULE_PREFIX = "GuvFX-NativeLauncher"

$result = [ordered]@{
  ok = $false; launcher_exists = $false; sha256_matches = $false; acl_safe = $false;
  applocker_allow_present = $false; runtime_exists = $false; reason = ""
}
function Emit() { $result | ConvertTo-Json -Compress }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

# Only Allow ACEs granting a WRITE-class right to a non-admin principal make the launcher tenant-replaceable.
# Bits: WriteData 0x2, AppendData 0x4, WriteEA 0x10, WriteAttributes 0x100, DeleteChild 0x40, Delete 0x10000,
# ChangePermissions 0x40000, TakeOwnership 0x80000. (ReadAndExecute shares no bit with these.)
function Has-WriteBits([int]$rights) {
  return (($rights -band (0x2 -bor 0x4 -bor 0x10 -bor 0x100 -bor 0x40 -bor 0x10000 -bor 0x40000 -bor 0x80000)) -ne 0)
}

try {
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "refusing_path_traversal" }

  # launcher_exists
  $result.launcher_exists = (Test-Path -LiteralPath $LAUNCHER)
  if (-not $result.launcher_exists) { Fail "launcher_missing" }

  # sha256_matches (pinned manifest)
  if (-not (Test-Path -LiteralPath $MANIFEST)) { Fail "launcher_manifest_missing" }
  $pinned = $null
  foreach ($line in (Get-Content -LiteralPath $MANIFEST -ErrorAction Stop)) {
    $t = $line.Trim()
    if ($t -ne "" -and -not $t.StartsWith("#")) { $pinned = $t; break }
  }
  if ([string]::IsNullOrWhiteSpace($pinned)) { Fail "launcher_manifest_empty" }
  $actual = (Get-FileHash -LiteralPath $LAUNCHER -Algorithm SHA256).Hash
  $result.sha256_matches = ($actual.ToUpper() -eq $pinned.ToUpper())

  # acl_safe (non-tenant-writable)
  $writable = $false
  foreach ($ace in (Get-Acl -LiteralPath $LAUNCHER).Access) {
    if ($ace.AccessControlType -ne "Allow") { continue }
    $sid = $null
    try { $sid = $ace.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { $sid = "" }
    # SYSTEM (S-1-5-18) and Administrators (S-1-5-32-544) are permitted Full; any OTHER principal with write bits
    # (Users S-1-5-32-545, Everyone S-1-1-0, a tenant SID, Authenticated Users, ...) makes the binary replaceable.
    if ($sid -eq "S-1-5-18" -or $sid -eq "S-1-5-32-544") { continue }
    if (Has-WriteBits ([int]$ace.FileSystemRights)) { $writable = $true }
  }
  $result.acl_safe = (-not $writable)

  # applocker_allow_present (deny-by-default -> the launcher needs an explicit ALLOW). Read the effective policy
  # and look in the Exe collection for an Allow rule for the launcher: by the canonical arming-step rule-name
  # prefix, or by the launcher's own file hash in an Allow FileHashRule (robust to a renamed rule).
  $allow = $false
  try {
    [xml]$eff = Get-AppLockerPolicy -Effective -Xml
    $li = $null
    try { $li = Get-AppLockerFileInformation -Path $LAUNCHER } catch { $li = $null }
    $lhash = $null
    if ($li -and $li.Hash) { $lhash = ("$($li.Hash)").ToUpper() }
    foreach ($coll in @($eff.AppLockerPolicy.RuleCollection)) {
      if ($coll.Type -ne "Exe") { continue }
      foreach ($rule in @($coll.ChildNodes)) {
        if ($rule.Action -ne "Allow") { continue }
        if ($rule.Name -and ($rule.Name -like ($ALLOW_RULE_PREFIX + "*"))) { $allow = $true }
        if ($lhash) {
          foreach ($fh in @($rule.Conditions.FileHashCondition.FileHash)) {
            if ($fh -and (("$($fh.Data)").ToUpper() -eq $lhash)) { $allow = $true }
          }
        }
      }
    }
  } catch { $allow = $false }
  $result.applocker_allow_present = $allow

  # runtime_exists (tenant runtime materialised)
  $exe = Join-Path $full "terminal64.exe"
  $result.runtime_exists = (Test-Path -LiteralPath $exe)

  $result.ok = $true
  $allTrue = ($result.launcher_exists -and $result.sha256_matches -and $result.acl_safe -and `
              $result.applocker_allow_present -and $result.runtime_exists)
  $result.reason = if ($allTrue) { "ok" } else { "native_launcher_invalid" }
  Emit
  exit 0
}
catch {
  Fail "verify_native_launcher_exception"
}

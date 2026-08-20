<#
  Contain-GuvfxLiveUpdate.ps1  (P0 pre-beta reliability gate - PROACTIVE LiveUpdate containment)

  Applies the certified Variant-A LiveUpdate containment to a FRESH hosted tenant BEFORE the customer's first
  MT5 launch, so the proven first-launch LiveUpdate terminal-fork can never strand a real beta customer.

  Proven failure (acct 29, 2026-08-20): a fresh tenant opened MT5 /portable for the first time, MT5's LiveUpdate
  staged an update and forked a SECOND, non-portable terminal that carried the broker login, while the GuvFX
  bridge stayed pinned to the login-less /portable instance - account_info hung and onboarding stalled forever
  at "Detecting your account...". The reactive AJ#6.4 recovery (Relaunch-GuvfxTerminal.ps1) fixes this AFTER the
  customer is already stranded; this primitive prevents it PROACTIVELY at provisioning time.

  What it does (LocalSystem / executor context):
    1. Confinement guards (identical to Relaunch-GuvfxTerminal.ps1): refuse reserved ids (Customer Zero),
       username must be guvfx_u_<AccountId>, TerminalRoot must be the canonical accounts\<id>\terminal.
    2. Resolve the tenant SID from Win32_UserAccount (a local query; the identity exists after PROVISION_IDENTITY
       even though the profile does not).
    3. Ensure the tenant profile EXISTS via userenv!CreateProfile - a pure API call that materialises the profile
       directory + hive from the Default profile. It creates NO interactive session and launches NO process
       (in particular it NEVER launches MT5). Idempotent (ALREADY_EXISTS is success).
    4. Apply the certified Variant-A containment (Apply-LiveUpdateContainment - byte-identical to the certified
       body in Relaunch-GuvfxTerminal.ps1, guarded by tests_liveupdate_containment_provisioning): Deny-write the
       tenant's OWN roaming update-staging (%APPDATA%\MetaQuotes\WebInstall + per-hash Terminal\<hash>\liveupdate)
       for the tenant SID only, reparse-safe, read-back verified.

  It NEVER launches MT5, closes a terminal, logs in, changes a broker account, arms a strategy, or places an
  order. It only touches the tenant's OWN profile staging - never the runtime dir, another tenant's profile, or
  the operator estate. Customer Zero is refused up front. Fail-closed: ok:true ONLY when the profile is present
  and the Deny-write is read-back-verified on every target. All identity/path values are server-derived.
  ASCII-only (RULE 9 corollary; validate with ParseFile before first run).
#>
param(
  [Parameter(Mandatory = $true)][string]$Username,
  [Parameter(Mandatory = $true)][string]$TerminalRoot,
  [Parameter(Mandatory = $true)][int]$AccountId
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$ACCOUNTS_BASE = "C:\GuvFX\accounts"
$RESERVED_ACCOUNT_IDS = @(1, 18)      # SACRED, NEVER contained here: Customer Zero (1) + the account-18 control

$result = [ordered]@{
  account_id = $AccountId; username = $Username; profile_present_before = $false; profile_created = $false;
  contained = $false; ok = $false; reason = ""
}
function Emit() { $result | ConvertTo-Json -Compress }
function Fail([string]$why) { $result.ok = $false; $result.reason = $why; Emit; exit 1 }

# ===== BEGIN certified shared containment (byte-identical to Relaunch-GuvfxTerminal.ps1; divergence-guarded) ==
# True iff NO component from $anchor down to $full (which must be under $anchor) is a reparse point / junction.
# Uses Get-Item (not Test-Path) for the presence test: a junction whose target has been deleted (dangling) can
# make Test-Path return false and silently skip the reparse check - Get-Item -Force still returns the link with
# its ReparsePoint attribute set.
function Test-ChainReparseFree([string]$anchor, [string]$full) {
  $a = $anchor.TrimEnd("\"); $f = $full.TrimEnd("\")
  if (-not $f.ToLower().StartsWith(($a + "\").ToLower())) { return $false }
  $cur = $a
  $it = Get-Item -LiteralPath $cur -Force -ErrorAction SilentlyContinue
  if ($it -and (($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { return $false }
  foreach ($part in $f.Substring($a.Length).Trim("\").Split("\")) {
    if ($part -eq "") { continue }
    $cur = Join-Path $cur $part
    $it = Get-Item -LiteralPath $cur -Force -ErrorAction SilentlyContinue
    if ($it -and (($it.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0)) { return $false }
  }
  return $true
}

# Certified Variant-A LiveUpdate containment, run as LocalSystem on the tenant's OWN update staging. Returns
# $true only when the Deny(Write) is read-back verified on every target; fail-closed on any reparse point.
function Apply-LiveUpdateContainment($tenant) {
  $sid = New-Object System.Security.Principal.SecurityIdentifier($tenant.sid)
  $mqRoot = Join-Path (Join-Path $tenant.profile "AppData\Roaming") "MetaQuotes"
  # 1) Kill this tenant's OWN stuck updater (terminal64 whose exe lives under the tenant profile liveupdate).
  foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='terminal64.exe'" -ErrorAction SilentlyContinue)) {
    $exe = ""
    if ($p.ExecutablePath) { $exe = [string]$p.ExecutablePath }
    if (($exe -match "(?i)\\liveupdate\\") -and ($exe.ToLower().StartsWith(($tenant.profile.TrimEnd("\") + "\").ToLower()))) {
      try { Stop-Process -Id ([int]$p.ProcessId) -Force -ErrorAction Stop } catch {}
    }
  }
  Start-Sleep -Seconds 2
  # 2) Deny-write + purge the tenant's OWN staging (WebInstall = load-bearing chokepoint; per-hash liveupdate).
  $targets = New-Object System.Collections.Generic.List[string]
  $targets.Add((Join-Path $mqRoot "WebInstall"))
  $terminalRoot = Join-Path $mqRoot "Terminal"
  if ((Test-Path -LiteralPath $terminalRoot) -and (Test-ChainReparseFree $tenant.profile $terminalRoot)) {
    foreach ($d in (Get-ChildItem -LiteralPath $terminalRoot -Directory -ErrorAction SilentlyContinue)) {
      if (($d.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq 0) { $targets.Add((Join-Path $d.FullName "liveupdate")) }
    }
  }
  $writeRights = [System.Security.AccessControl.FileSystemRights]::Write
  $inherit = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
  foreach ($t in $targets) {
    if (-not (Test-ChainReparseFree $tenant.profile $t)) { return $false }
    if (-not (Test-Path -LiteralPath $t)) { New-Item -ItemType Directory -Force -Path $t | Out-Null }
    # Reparse-safe purge: a junction child is removed as a LINK, never recursed through.
    foreach ($c in (Get-ChildItem -LiteralPath $t -Force -ErrorAction SilentlyContinue)) {
      if (($c.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { $c.Delete() }
      else { Remove-Item -LiteralPath $c.FullName -Recurse -Force -ErrorAction Stop }
    }
    # Re-check immediately before the DACL mutate (shrink the TOCTOU window).
    if (((Get-Item -LiteralPath $t -Force).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { return $false }
    $acl = Get-Acl -LiteralPath $t
    $deny = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, $writeRights, $inherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)
    [void]$acl.RemoveAccessRule($deny)
    [void]$acl.AddAccessRule($deny)
    Set-Acl -LiteralPath $t -AclObject $acl
    $ok = $false
    foreach ($r in (Get-Acl -LiteralPath $t).GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
      if ($r.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny -and
          (-not $r.IsInherited) -and
          $r.IdentityReference.Value -eq $sid.Value -and
          (([int]$r.FileSystemRights -band [int]$writeRights) -eq [int]$writeRights)) { $ok = $true }
    }
    if (-not $ok) { return $false }
  }
  return $true
}
# ===== END certified shared containment ====================================================================

# Resolve the tenant SID WITHOUT NTAccount name translation (which hangs on this workgroup host). The identity
# exists after PROVISION_IDENTITY even before the profile does. Returns the SID string or $null (fail-closed).
function Resolve-Sid([string]$user) {
  $acct = @(Get-CimInstance Win32_UserAccount -Filter ("Name='" + $user + "' AND LocalAccount=True") -ErrorAction SilentlyContinue)
  if ($acct.Count -ne 1 -or [string]::IsNullOrWhiteSpace($acct[0].SID)) { return $null }  # reject ambiguity
  return $acct[0].SID
}

# The tenant's authoritative profile path from the SID-keyed ProfileList, or $null if not present yet.
function ProfileList-Path([string]$sid) {
  try { $pi = (Get-ItemProperty ("HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList\" + $sid) -Name ProfileImagePath -ErrorAction Stop).ProfileImagePath }
  catch { return $null }
  if ([string]::IsNullOrWhiteSpace($pi)) { return $null }
  return $pi
}

# Defence in depth: the authoritative profile must live under C:\Users and end with the tenant's own name.
function Validate-Profile([string]$path, [string]$user) {
  if ([string]::IsNullOrWhiteSpace($path) -or -not (Test-Path -LiteralPath $path)) { return $false }
  $fp = [System.IO.Path]::GetFullPath($path)
  if (-not $fp.ToLower().StartsWith("c:\users\")) { return $false }
  if ([System.IO.Path]::GetFileName($fp.TrimEnd("\")).ToLower() -ne $user.ToLower()) { return $false }
  return $true
}

$__createProfileSig = @'
[DllImport("userenv.dll", CharSet=CharSet.Unicode, ExactSpelling=true, SetLastError=false)]
public static extern int CreateProfile(
    [MarshalAs(UnmanagedType.LPWStr)] string pszUserSid,
    [MarshalAs(UnmanagedType.LPWStr)] string pszUserName,
    [Out][MarshalAs(UnmanagedType.LPWStr)] System.Text.StringBuilder pszProfilePath,
    uint cchProfilePath);
'@

try {
  # ---- Confinement (defence in depth; the dispatcher already derived every value + refused reserved ids) ----
  if ($RESERVED_ACCOUNT_IDS -contains $AccountId) { Fail "refusing_reserved_identity" }
  if ($AccountId -le 0) { Fail "refusing_account_id_out_of_range" }
  if ($Username -ne ("guvfx_u_" + $AccountId)) { Fail "refusing_username_mismatch" }
  $full = [System.IO.Path]::GetFullPath($TerminalRoot)
  if ($full -like "*..*") { Fail "refusing_path_traversal" }
  $expected = [System.IO.Path]::GetFullPath((Join-Path (Join-Path $ACCOUNTS_BASE ([string]$AccountId)) "terminal"))
  if ($full.ToLower() -ne $expected.ToLower()) { Fail "refusing_terminal_root_mismatch" }

  # ---- Resolve the tenant SID (identity exists; profile may not) ----------------------------------------
  $sid = Resolve-Sid $Username
  if ($null -eq $sid) { Fail "tenant_resolution_failed" }

  # ---- Ensure the tenant profile exists (CreateProfile: no interactive session, no MT5 launch) ----------
  $existing = ProfileList-Path $sid
  if ((-not [string]::IsNullOrWhiteSpace($existing)) -and (Validate-Profile $existing $Username)) {
    $result.profile_present_before = $true
    $profilePath = [System.IO.Path]::GetFullPath($existing)
  }
  else {
    Add-Type -MemberDefinition $__createProfileSig -Namespace GuvfxProfile -Name Native -Using System.Text -ErrorAction Stop
    $sb = New-Object System.Text.StringBuilder 260
    $hr = [GuvfxProfile.Native]::CreateProfile($sid, $Username, $sb, [uint32]$sb.Capacity)
    # 0 = S_OK (created); 0x800700B7 = HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS) (idempotent). Anything else fails.
    if ($hr -ne 0 -and $hr -ne -2147024713) { Fail "profile_create_failed" }
    $created = ($hr -eq 0)
    # Re-resolve authoritatively from ProfileList (never trust a reconstructed path).
    $after = ProfileList-Path $sid
    if ([string]::IsNullOrWhiteSpace($after)) { $after = $sb.ToString() }
    if (-not (Validate-Profile $after $Username)) { Fail "profile_validate_failed" }
    $profilePath = [System.IO.Path]::GetFullPath($after)
    $result.profile_created = $created
  }

  # ---- Apply the certified Variant-A containment on the tenant's OWN roaming staging --------------------
  $tenant = @{ sid = $sid; profile = $profilePath }
  $contained = $false
  try { $contained = Apply-LiveUpdateContainment $tenant } catch { $contained = $false }
  $result.contained = $contained
  if (-not $contained) { Fail "containment_failed" }

  $result.ok = $true
  $result.reason = "ok"
  Emit
  exit 0
}
catch {
  Fail "containment_exception"
}

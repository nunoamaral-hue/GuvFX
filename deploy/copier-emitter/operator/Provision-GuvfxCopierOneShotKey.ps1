#Requires -Version 5.1
# ASCII-only (security.md RULE 9: must parse under Windows PowerShell 5.1 with no BOM). GuvFX Copy Trading -
# secure HUMAN-OPERATED host-local one-shot ingest-key PROVISIONER (ADR-0104/ADR-0101;
# GFX-COPIER-ONESHOT-KEY-PROVISIONER-112). Repository tooling: NOT installed, NOT scheduled, NOT run by any agent.
# A human operator runs it locally over RDP (as an administrator) during a single Sponsor-gated verification session,
# immediately before the signed one-shot attestation TRIGGER.
#
# WHY THIS EXISTS
#   The pre-installed, DISABLED one-shot task GuvFX_CopierEmitterOneShot_46 runs run_oneshot_attest.py, which reads the
#   ingest secret ONLY from a host-local EPHEMERAL key file (and shreds it after one use). That consumer deliberately
#   NEVER provisions the file. The interactive child-env wrapper (Invoke-GuvfxCopierOneShotAttestation.ps1) cannot help
#   the headless scheduled-task path (account 46's workspace is MT5-only, no interactive PowerShell desktop). This
#   wrapper is the ONLY reviewed, secure way to place that file: human hidden-input -> atomic, ACL-restricted write.
#
# SECURITY CONTRACT (fail-closed everywhere)
#   * FIXED scope: account 46, task GuvFX_CopierEmitterOneShot_46, key path C:\GuvFX\copier-emitter\oneshot\ingest.key,
#     principal guvfx_u_46. No caller-supplied alternate account / task / path / principal. #48, Customer Zero and every
#     reserved identity (id <= 1) are refused. It is NOT a general-purpose secret writer.
#   * Human hidden-input ONLY (Read-Host -AsSecureString). The key is NEVER accepted through argv/env/registry/pipeline/
#     transcript and NEVER emitted to stdout/logs/event-log/result/Notion/Git. It refuses to run under active PowerShell
#     transcription or Verbose/Debug tracing. It never generates or infers a real key.
#   * Validation (fail closed): secret present + length >= 32; installed task exists, DISABLED, triggerless, action points
#     at the reviewed one-shot entrypoint + the fixed key path, principal SID == guvfx_u_46 SID; destination + parent chain
#     carry no reparse point / symlink / junction; the final key file must NOT already exist (use -Remove first).
#   * Atomic protected write: a same-directory temp file is created, inheritance is broken and only {guvfx_u_46 (Read),
#     SYSTEM (Full), Administrators (Full)} are granted BEFORE the secret bytes are written, then it is atomically renamed
#     onto the fixed path (which therefore never exists with broad inherited permissions). Temp artefacts are removed on
#     success and failure.
#   * -Remove: idempotent secure cleanup of the fixed path (+ this wrapper's temp siblings); refuses a reparse target;
#     leaves the task DISABLED and triggerless; reports only redacted state.
#   * -Verify: read-only independent check (path/regular-file/no-reparse/min-length-by-size/ACL/owner/task) that NEVER
#     reads or prints the key or any key-derived value.
#   NOTE: Windows storage cannot promise forensic overwrite of a deleted file. The guarantee is bounded ephemeral
#   lifetime + restrictive ACLs + the one-use consumer deletion (run_oneshot_attest shreds after a single attempt) +
#   mandatory cleanup here - NOT an unverifiable "secure erase".
[CmdletBinding()]
param(
    [int]$AccountId = 46,
    [switch]$Remove,
    [switch]$Verify,
    [switch]$AssumeYes,                              # test-only: skip the typed confirmation (the secret is STILL prompted)
    # ---- test-only injectable seams (production uses the real defaults; NONE changes the fixed account/task/filename) ----
    [string]$FixedRootForTest = '',                 # sandbox ONLY the C:\GuvFX root for the FIXED relative key structure
    [scriptblock]$ReadTaskXml = $null,              # () -> installed-task XML string, or $null if absent
    [scriptblock]$ResolvePrincipalSid = $null,      # (ref) -> canonical SID string, or $null (fail closed)
    [scriptblock]$ReadSecret = $null,               # () -> plaintext secret (tests inject a synthetic >=32-char value)
    [scriptblock]$AfterTempForTest = $null          # test-only: invoked after the restricted temp write, before the
                                                    # atomic rename, so a test can throw to exercise catch/cleanup.
)                                                   # Production NEVER passes it (null = no-op); the source guard enforces this.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
Set-PSDebug -Off

# ---- fixed, non-negotiable scope (defence in depth: the installer + host .ps1 + consumer also pin these) ----
$PINNED_ACCOUNT = 46
$RESERVED_IDS = @(0, 1, 18)                          # Customer Zero (1), id 0, sacred 18
$EXPLICIT_REFUSE = @(48)                             # #48 is explicitly out of scope
$TASK_NAME = 'GuvFX_CopierEmitterOneShot_' + $PINNED_ACCOUNT
$PRINCIPAL_NAME = 'guvfx_u_' + $PINNED_ACCOUNT
$KEY_RELATIVE = 'copier-emitter\oneshot\ingest.key' # the FIXED relative structure; the filename is always ingest.key
$FIXED_ROOT = 'C:\GuvFX'
$MIN_SECRET_LEN = 32                                 # matches run_oneshot_attest._MIN_SECRET_LEN and verifier_window._MIN_KEY_LEN
$CONFIRM_PHRASE = 'provision-key-account-46'

function Write-Redacted($obj) { $obj | ConvertTo-Json -Compress }

# A redacted result NEVER carries key material, a key-derived digest, a slice of the key, or a raw dump of it.
function New-Result([bool]$ok, [string]$action, [string]$reason) {
    return [ordered]@{
        ok = $ok; action = $action; account_id = $PINNED_ACCOUNT; task = $TASK_NAME;
        key_file_present = $false; length_policy_satisfied = $false; acl_verified = $false;
        reason = $reason; correlation_id = ([guid]::NewGuid().ToString('N'));
        at = ((Get-Date).ToUniversalTime().ToString('o'))
    }
}

function Resolve-KeyPath() {
    $root = if ($FixedRootForTest) { $FixedRootForTest } else { $FIXED_ROOT }
    $p = Join-Path $root $KEY_RELATIVE
    # defence in depth: the leaf, its parent and grandparent are ALWAYS the fixed structure, whatever the root.
    if ((Split-Path -Leaf $p) -ne 'ingest.key') { throw 'fixed_path_leaf_violation' }
    if ((Split-Path -Leaf (Split-Path -Parent $p)) -ne 'oneshot') { throw 'fixed_path_parent_violation' }
    if ((Split-Path -Leaf (Split-Path -Parent (Split-Path -Parent $p))) -ne 'copier-emitter') { throw 'fixed_path_grandparent_violation' }
    return $p
}

# Walk the file path upward; refuse if ANY existing component is a reparse point (symlink/junction/mount).
function Test-NoReparseChain([string]$path) {
    $p = $path
    while ($p) {
        if (Test-Path -LiteralPath $p) {
            $item = Get-Item -LiteralPath $p -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq [System.IO.FileAttributes]::ReparsePoint) { return $false }
        }
        $parent = Split-Path -Parent $p
        if ([string]::IsNullOrEmpty($parent) -or $parent -eq $p) { break }
        $p = $parent
    }
    return $true
}

# Namespace-agnostic, StrictMode-safe XML reads. Task Scheduler XML carries a default namespace, so dot-navigation is
# avoided; SelectSingleNode([local-name()...]) returns $null for a missing node instead of throwing under StrictMode.
function Get-TextByPath($xml, [string]$xpath) {
    $n = $xml.SelectSingleNode($xpath)
    if ($null -eq $n) { return '' }
    return [string]$n.InnerText
}
# Null-safe trigger count: @($null).Count is 1 in PowerShell, so a triggerless task is detected by counting the
# <Triggers> ELEMENT children in the task XML, never via @($task.Triggers).Count.
function Get-TriggerElementCount($xml) {
    $t = $xml.SelectSingleNode("//*[local-name()='Task']/*[local-name()='Triggers']")
    if ($null -eq $t) { return 0 }
    return [int](@($t.ChildNodes | Where-Object { $_.NodeType -eq [System.Xml.XmlNodeType]::Element }).Count)
}

# Resolve the installed-task XML through the seam (default: real schtasks via an EAP-scoped native call so an absent
# task cannot throw a terminating error on PS 5.1 native-stderr).
function Get-TaskXmlString() {
    if ($null -ne $ReadTaskXml) { return (& $ReadTaskXml) }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = (& schtasks.exe /query /tn ('\' + $TASK_NAME) /xml ONE 2>&1 |
            Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] }) -join [Environment]::NewLine
    } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($out)) { return $null }
    return [string]$out
}

# Canonical SID resolution (SID / bare name / HOST\name -> SID) through the seam (default: authoritative Windows
# NTAccount/SecurityIdentifier translation). Fail closed (return $null) on any failure.
function Resolve-Sid([string]$ref) {
    if ($null -ne $ResolvePrincipalSid) { return (& $ResolvePrincipalSid $ref) }
    if ([string]::IsNullOrWhiteSpace($ref)) { return $null }
    try {
        if ($ref -match '^S-1-') { return (New-Object System.Security.Principal.SecurityIdentifier($ref)).Value }
        $acct = $ref
        if ($ref -notmatch '\\') { $acct = "$env:COMPUTERNAME\$ref" }
        return (New-Object System.Security.Principal.NTAccount($acct)).Translate([System.Security.Principal.SecurityIdentifier]).Value
    } catch { return $null }
}

# Validate the installed one-shot task against the reviewed account-46 contract. Returns the guvfx_u_46 SID on success,
# or throws a stable reason string. The ACL grant is tied to THIS resolved SID (the identity the task runs as).
function Assert-TaskContract() {
    $raw = Get-TaskXmlString
    if ([string]::IsNullOrWhiteSpace($raw)) { throw 'task_absent' }
    $xml = $null
    try { $xml = [xml]$raw } catch { throw 'task_xml_malformed' }
    if ($null -eq $xml.DocumentElement -or $xml.DocumentElement.LocalName -ne 'Task') { throw 'task_xml_malformed' }
    $enabled = Get-TextByPath $xml "//*[local-name()='Task']/*[local-name()='Settings']/*[local-name()='Enabled']"
    if ($enabled -ne 'false') { throw 'task_not_disabled' }
    if ((Get-TriggerElementCount $xml) -ne 0) { throw 'task_has_trigger' }
    $taskArgs = Get-TextByPath $xml "//*[local-name()='Actions']/*[local-name()='Exec']/*[local-name()='Arguments']"
    if ($taskArgs -notmatch 'run_oneshot_attest\.py' -or $taskArgs -notmatch 'attest-verify' -or
        $taskArgs -notmatch 'attest_result\.json' -or $taskArgs -match 'event-ingest') { throw 'task_action_drift' }
    if ($taskArgs -notmatch ([regex]::Escape($KEY_RELATIVE))) { throw 'task_action_keypath_drift' }
    $expectedSid = Resolve-Sid $PRINCIPAL_NAME
    $actualSid = Resolve-Sid (Get-TextByPath $xml "//*[local-name()='Principals']/*[local-name()='Principal']/*[local-name()='UserId']")
    if (-not $expectedSid -or -not $actualSid -or ($expectedSid -ne $actualSid)) { throw 'task_principal_drift' }
    return $expectedSid
}

# Break inheritance and grant ONLY {tenant:Read, SYSTEM:Full, Administrators:Full}. Owner set to Administrators
# (best-effort). No Users / Everyone / Authenticated Users / sibling-service principal retains access.
function Set-ProtectedAcl([string]$path, [string]$tenantSid) {
    $acl = Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true, $false)                      # protect + DO NOT copy inherited ACEs
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }   # drop any remaining explicit ACEs
    $sys = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
    $admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $tenant = New-Object System.Security.Principal.SecurityIdentifier($tenantSid)
    $full = [System.Security.AccessControl.FileSystemRights]::FullControl
    # The tenant (guvfx_u_46) is the one-use CONSUMER: run_oneshot_attest.py runs AS guvfx_u_46 and must READ the key,
    # then OVERWRITE + DELETE it to shred it after the single attempt. That needs Modify (read + write + delete), not a
    # bare Read grant -- a Read-only grant would silently break the one-use shred and leave the secret on disk.
    $tenantRights = [System.Security.AccessControl.FileSystemRights]::Modify
    $none = [System.Security.AccessControl.InheritanceFlags]::None
    $noprop = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sys, $full, $none, $noprop, $allow)))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($admins, $full, $none, $noprop, $allow)))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($tenant, $tenantRights, $none, $noprop, $allow)))
    try { $acl.SetOwner($admins) } catch { }                         # best-effort; verifier accepts {SYSTEM,Admins,tenant}
    Set-Acl -LiteralPath $path -AclObject $acl
}

# ACL for a directory WE create: inheritance broken; SYSTEM + Administrators Full; the tenant gets ReadAndExecute only
# (traverse + list, so guvfx_u_46 can reach the key file by name) but NO write -- it cannot plant a junction or tamper.
function Set-ProtectedDirAcl([string]$path, [string]$tenantSid) {
    $acl = Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
    $sys = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
    $admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $tenant = New-Object System.Security.Principal.SecurityIdentifier($tenantSid)
    $full = [System.Security.AccessControl.FileSystemRights]::FullControl
    $rx = [System.Security.AccessControl.FileSystemRights]::ReadAndExecute
    $none = [System.Security.AccessControl.InheritanceFlags]::None
    $noprop = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sys, $full, $none, $noprop, $allow)))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($admins, $full, $none, $noprop, $allow)))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($tenant, $rx, $none, $noprop, $allow)))
    try { $acl.SetOwner($admins) } catch { }
    Set-Acl -LiteralPath $path -AclObject $acl
}

# Read-only ACL check: inheritance disabled AND the allowed principals are EXACTLY {tenant, SYSTEM, Administrators}.
function Test-ProtectedAcl([string]$path, [string]$tenantSid) {
    $acl = Get-Acl -LiteralPath $path
    if (-not $acl.AreAccessRulesProtected) { return $false }
    $allowed = @('S-1-5-18', 'S-1-5-32-544', $tenantSid)
    foreach ($rule in $acl.Access) {
        if ($rule.IsInherited) { return $false }
        $sid = $null
        try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { return $false }
        if ($allowed -notcontains $sid) { return $false }
        if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { return $false }
    }
    $ownerSid = $null
    try { $ownerSid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value } catch { $ownerSid = $null }
    if ($ownerSid -and ($allowed -notcontains $ownerSid)) { return $false }
    return $true
}

# Best-effort overwrite-then-delete of ONE file. The overwrite never raises (Windows cannot promise a forensic wipe);
# the DELETE is the guarantee, so a failed delete is surfaced by the caller re-checking Test-Path.
function Remove-SecretFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    try {
        $size = [int](Get-Item -LiteralPath $path -Force).Length
        $fs = [System.IO.File]::Open($path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Write)
        try { $zero = New-Object byte[] ([Math]::Max($size, 1)); $fs.Write($zero, 0, $zero.Length); $fs.Flush() }
        finally { $fs.Close() }
    } catch { }
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
}

function Remove-TempSiblings([string]$dir) {
    try {
        if (Test-Path -LiteralPath $dir) {
            Get-ChildItem -LiteralPath $dir -Filter '.ingest.key.tmp.*' -Force -ErrorAction SilentlyContinue |
                ForEach-Object { try { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue } catch { } }
        }
    } catch { }
}

# ---- transcription / tracing guard: fail closed if a capture mode could record the secret ----
function Assert-SafeCaptureMode() {
    if ($VerbosePreference -ne 'SilentlyContinue' -or $DebugPreference -ne 'SilentlyContinue') {
        throw 'tracing_active'
    }
    $txPolicy = $null
    try {
        $txPolicy = (Get-ItemProperty -Path 'HKLM:\Software\Policies\Microsoft\Windows\PowerShell\Transcription' `
                -Name 'EnableTranscripting' -ErrorAction SilentlyContinue).EnableTranscripting
    } catch { $txPolicy = $null }
    if ($txPolicy -eq 1) { throw 'transcription_active' }
    try { if (Get-Variable -Name 'Transcript' -Scope Global -ErrorAction SilentlyContinue) { throw 'transcription_active' } }
    catch { if ($_.Exception.Message -eq 'transcription_active') { throw } }
    try { Stop-Transcript | Out-Null } catch { }
}

# ---- account pin (shared by every mode) ----
function Assert-AccountPinned() {
    if ($RESERVED_IDS -contains $AccountId) { throw 'account_refused_reserved' }
    if ($EXPLICIT_REFUSE -contains $AccountId) { throw 'account_refused_explicit' }
    if ($AccountId -ne $PINNED_ACCOUNT) { throw 'account_not_pinned' }
}

# =====================================================================================================================
# Test seams are REFUSED in production. Any seam (the sandbox root, the task/SID/secret/failure injectors, or the
# confirmation skip) is honoured ONLY when the self-test signal is set -- a signal the production launch path (the
# runbook + the operator) never sets. This keeps the fail-closed contract un-bypassable from the command line: a
# caller cannot forge the task contract, redirect the path, or skip the human confirmation in production.
$seamUsed = ($FixedRootForTest -ne '' -or $null -ne $ReadTaskXml -or $null -ne $ResolvePrincipalSid -or
    $null -ne $ReadSecret -or $null -ne $AfterTempForTest -or $AssumeYes)
if ($seamUsed -and $env:GUVFX_PROVISIONER_SELFTEST -ne '1') {
    Write-Redacted (New-Result $false 'guard' 'test_seam_in_production'); exit 2
}

if ($Remove -and $Verify) { Write-Redacted (New-Result $false 'provision' 'mode_conflict'); exit 2 }

$mode = if ($Remove) { 'remove' } elseif ($Verify) { 'verify' } else { 'provision' }
$result = New-Result $false $mode 'not_started'
$keyPath = $null
$plain = $null
$secure = $null

try {
    Assert-AccountPinned
    $keyPath = Resolve-KeyPath
    $dir = Split-Path -Parent $keyPath

    if (-not (Test-NoReparseChain $keyPath)) { $result.reason = 'reparse_point_detected'; Write-Redacted $result; exit 2 }

    # ----------------------------------------- VERIFY (read-only) -----------------------------------------
    if ($mode -eq 'verify') {
        $tenantSid = Assert-TaskContract                               # task must still be the compliant #46 contract
        if (-not (Test-Path -LiteralPath $keyPath)) { $result.reason = 'key_file_absent'; Write-Redacted $result; exit 2 }
        $item = Get-Item -LiteralPath $keyPath -Force
        if ($item -is [System.IO.DirectoryInfo]) { $result.reason = 'key_not_regular_file'; Write-Redacted $result; exit 2 }
        $result.key_file_present = $true
        $result.length_policy_satisfied = ([int]$item.Length -ge $MIN_SECRET_LEN)   # by SIZE; content never read
        $result.acl_verified = (Test-ProtectedAcl $keyPath $tenantSid)
        $ok = ($result.length_policy_satisfied -and $result.acl_verified)
        $result.ok = $ok
        $result.reason = if ($ok) { 'verified' } else { 'verify_failed' }
        Write-Redacted $result
        exit ([int](-not $ok))
    }

    # ----------------------------------------- REMOVE (idempotent shred+delete) ---------------------------
    if ($mode -eq 'remove') {
        # account-46 scope comes from the account pin + the FIXED path (not the task): -Remove must stay robust for abort
        # cleanup even if the task is absent or in an odd state. It NEVER touches the task, so the task stays DISABLED.
        if (Test-Path -LiteralPath $keyPath) {
            $item = Get-Item -LiteralPath $keyPath -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq [System.IO.FileAttributes]::ReparsePoint) {
                $result.reason = 'remove_target_reparse'; Write-Redacted $result; exit 2
            }
            Remove-SecretFile $keyPath
        }
        Remove-TempSiblings $dir
        $result.key_file_present = (Test-Path -LiteralPath $keyPath)
        $ok = (-not $result.key_file_present)
        $result.ok = $ok
        $result.reason = if ($ok) { 'removed' } else { 'remove_failed' }
        Write-Redacted $result
        exit ([int](-not $ok))
    }

    # ----------------------------------------- PROVISION ------------------------------------------------
    Assert-SafeCaptureMode
    $tenantSid = Assert-TaskContract                                   # task exists, DISABLED, triggerless, action+principal OK
    if (Test-Path -LiteralPath $keyPath) { $result.reason = 'key_file_exists'; Write-Redacted $result; exit 2 }

    if (-not $AssumeYes) {
        Write-Output ('HUMAN-OPERATED one-shot ingest-key provisioning for GuvFX account ' + $PINNED_ACCOUNT + '.')
        Write-Output 'This writes ONE ephemeral, ACL-restricted key file. It does NOT enable, trigger or run any task.'
        $c = Read-Host ('To proceed type EXACTLY: ' + $CONFIRM_PHRASE)
        if ($c -ne $CONFIRM_PHRASE) { $result.reason = 'confirmation_mismatch'; Write-Redacted $result; exit 2 }
    }

    if ($null -ne $ReadSecret) {
        $plain = [string](& $ReadSecret)                              # tests inject a synthetic >=32-char value
    } else {
        $secure = Read-Host -AsSecureString 'Enter the ingest key for account 46 (hidden)'
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if ([string]::IsNullOrEmpty($plain) -or $plain.Length -lt $MIN_SECRET_LEN) {
        $plain = $null; $result.reason = 'secret_too_short'; Write-Redacted $result; exit 2
    }
    $result.length_policy_satisfied = $true

    # Create the fixed directory tree (admin-controlled) if missing, then re-verify the FULL path chain carries NO
    # reparse point immediately before writing -- closing any junction/symlink planted during the human-input window.
    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Set-ProtectedDirAcl $dir $tenantSid                          # a dir WE create: admin-owned; tenant traverse-only
    }
    if (-not (Test-NoReparseChain $keyPath)) { $plain = $null; $result.reason = 'reparse_point_detected'; Write-Redacted $result; exit 2 }
    if (Test-Path -LiteralPath $keyPath) { $plain = $null; $result.reason = 'key_file_exists'; Write-Redacted $result; exit 2 }

    # atomic protected write: restrict the same-dir temp BEFORE the secret bytes land, then rename onto the fixed path.
    $tmp = Join-Path $dir ('.ingest.key.tmp.' + [guid]::NewGuid().ToString('N'))
    $utf8 = New-Object System.Text.UTF8Encoding($false)               # UTF-8, no BOM; verbatim (no normalisation)
    try {
        [System.IO.File]::WriteAllText($tmp, '', $utf8)               # create empty (no secret yet)
        Set-ProtectedAcl $tmp $tenantSid                              # restrict BEFORE writing the secret
        [System.IO.File]::WriteAllText($tmp, $plain, $utf8)           # secret bytes into the already-restricted temp
        $plain = $null
        if ($null -ne $AfterTempForTest) { & $AfterTempForTest }      # test-only failure injection (production: null)
        [System.IO.File]::Move($tmp, $keyPath)                       # atomic rename; fails closed if the dest appeared
        $tmp = $null
        Set-ProtectedAcl $keyPath $tenantSid                          # re-assert on the final path (defensive)
    } catch {
        $plain = $null
        if ($tmp -and (Test-Path -LiteralPath $tmp)) { Remove-SecretFile $tmp }
        Remove-TempSiblings $dir
        $result.reason = 'write_failed'; Write-Redacted $result; exit 2
    }

    $result.key_file_present = (Test-Path -LiteralPath $keyPath)
    $result.acl_verified = (Test-ProtectedAcl $keyPath $tenantSid)
    $ok = ($result.key_file_present -and $result.acl_verified)
    if (-not $ok) { Remove-SecretFile $keyPath }                      # never leave a weakly-protected secret on disk
    $result.ok = $ok
    $result.reason = if ($ok) { 'provisioned' } else { 'acl_failed' }
    Write-Redacted $result
    exit ([int](-not $ok))
}
catch {
    $plain = $null
    $reason = [string]$_.Exception.Message
    if ([string]::IsNullOrWhiteSpace($reason)) { $reason = 'error' }
    $result.ok = $false; $result.reason = $reason
    Write-Redacted $result
    exit 2
}
finally {
    $plain = $null
    if ($secure) { try { $secure.Dispose() } catch { } }
    [System.GC]::Collect()
}

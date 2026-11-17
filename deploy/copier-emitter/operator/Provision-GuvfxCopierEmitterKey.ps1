#Requires -Version 5.1
# ASCII-only (security.md RULE 9: must parse under Windows PowerShell 5.1 with no BOM). GuvFX Copy Trading -
# secure HUMAN-OPERATED host-local STANDING immediate-emitter ingest-key PROVISIONER
# (GFX-COPIER-LIFECYCLE-ARMING-SAFETY-CORRECTION-148, ADR-0114). Repository tooling: NOT installed, NOT scheduled,
# NOT run by any agent. A human operator runs it locally over RDP (as an administrator) during a single
# Sponsor-gated demo session, immediately before the atomic lifecycle arming - and -Remove at teardown.
#
# WHY THIS EXISTS
#   The pre-installed, DISABLED standing emitter task GuvFX_CopierEmitter_46 runs run_emitter.py, which signs the
#   <=1s event-ingest emissions with the ingest HMAC key named by the config (COPIER_INGEST_HMAC_KEY). A scheduled
#   task does not inherit a shell env, so run_emitter.py auto-loads that key ONCE at startup from a protected
#   host-local file next to the config (C:\GuvFX\copier-emitter\46\ingest.key). The one-shot provisioner
#   (Provision-GuvfxCopierOneShotKey.ps1) is FIXED to the one-shot attestation task + a one-use-shred path and
#   cannot place the STANDING key. This wrapper is the ONLY reviewed, secure way to place it: human hidden-input
#   -> atomic, ACL-restricted write. Distinct from the one-shot: the standing key persists for the bounded demo
#   (the emitter only READS it) and is removed at teardown via -Remove - it is NOT shredded after one use.
#
# SECURITY CONTRACT (fail-closed everywhere)
#   * FIXED scope: account 46, task GuvFX_CopierEmitter_46, key path C:\GuvFX\copier-emitter\46\ingest.key,
#     principal guvfx_u_46. No caller-supplied alternate account / task / path / principal. #48, Customer Zero and
#     every reserved identity (id <= 1) are refused. It is NOT a general-purpose secret writer and can NEVER target
#     the follower #48.
#   * Human hidden-input: by default the key is read from a hidden interactive prompt (Read-Host -AsSecureString).
#     The -KeyFromStdin GOVERNED single-entry mode reads "<confirm-phrase>`n<key>" from STDIN ONLY. In BOTH modes
#     the key is NEVER accepted through argv/env/registry and NEVER emitted to stdout/logs/event-log/result/
#     Notion/Git; it refuses to run under active PowerShell transcription or Verbose/Debug tracing, and never
#     generates or infers a real key.
#   * Validation (fail closed): secret present + length >= 32; installed task exists, DISABLED, triggerless, action
#     points at run_emitter.py 'run' + the account-46 config (copier-emitter\46\config.json), NOT the one-shot
#     attest entrypoint; principal SID == guvfx_u_46 SID; destination + parent chain carry no reparse point /
#     symlink / junction; the final key file must NOT already exist (use -Remove first).
#   * Atomic protected write: a same-directory temp file is created, inheritance is broken and only
#     {guvfx_u_46 (ReadAndExecute -- the emitter only READS the key; it NEVER writes/shreds it), SYSTEM (Full),
#     Administrators (Full)} are granted BEFORE the secret bytes are written, then it is atomically renamed onto the
#     fixed path. Temp artefacts are removed on success and failure.
#   * -Remove: idempotent secure cleanup of the fixed path (+ temp siblings) at teardown; refuses a reparse target;
#     leaves the task DISABLED and triggerless; reports only redacted state.
#   * -Verify: read-only independent check (path/regular-file/no-reparse/min-length-by-size/ACL/owner/tenant-read-
#     only/task) that NEVER reads or prints the key or any key-derived value.
#   * Enables, triggers or runs NO task; opens NO verifier window; enables NO flag; contacts NO backend/broker.
#   NOTE: Windows storage cannot promise forensic overwrite of a deleted file. The guarantee is restrictive ACLs +
#   bounded demo lifetime + the mandatory -Remove at teardown - NOT an unverifiable "secure erase".
[CmdletBinding()]
param(
    [int]$AccountId = 46,
    [switch]$Remove,
    [switch]$Verify,
    [switch]$AssumeYes,                              # test-only: skip the typed confirmation (the secret is STILL prompted)
    [switch]$KeyFromStdin,                           # GOVERNED single-entry: read "<phrase>`n<key>" from STDIN only
    # ---- test-only injectable seams (production uses the real defaults; NONE changes the fixed account/task/filename) ----
    [string]$FixedRootForTest = '',
    [scriptblock]$ReadTaskXml = $null,
    [scriptblock]$ResolvePrincipalSid = $null,
    [scriptblock]$ReadSecret = $null,
    [scriptblock]$AfterTempForTest = $null
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
Set-PSDebug -Off

# ---- fixed, non-negotiable scope ----
$PINNED_ACCOUNT = 46
$RESERVED_IDS = @(0, 1, 18)                          # Customer Zero (1), id 0, sacred 18
$EXPLICIT_REFUSE = @(48)                             # #48 (the follower) is explicitly out of scope
$TASK_NAME = 'GuvFX_CopierEmitter_' + $PINNED_ACCOUNT
$PRINCIPAL_NAME = 'guvfx_u_' + $PINNED_ACCOUNT
$KEY_RELATIVE = 'copier-emitter\46\ingest.key'      # next to the account-46 config; run_emitter.py auto-loads it
$CONFIG_HINT = 'copier-emitter\46\config.json'       # the task action must point at THIS config
$FIXED_ROOT = 'C:\GuvFX'
$MIN_SECRET_LEN = 32                                 # matches emitter.app.resolve_secret _MIN_SECRET_LEN
$CONFIRM_PHRASE = 'provision-emitter-key-account-46'

function Write-Redacted($obj) { $obj | ConvertTo-Json -Compress }

function New-Result([bool]$ok, [string]$action, [string]$reason) {
    return [ordered]@{
        ok = $ok; action = $action; account_id = $PINNED_ACCOUNT; task = $TASK_NAME;
        key_file_present = $false; length_policy_satisfied = $false; acl_verified = $false;
        tenant_read_only = $false;
        reason = $reason; correlation_id = ([guid]::NewGuid().ToString('N'));
        at = ((Get-Date).ToUniversalTime().ToString('o'))
    }
}

function Resolve-KeyPath() {
    $root = if ($FixedRootForTest) { $FixedRootForTest } else { $FIXED_ROOT }
    $p = Join-Path $root $KEY_RELATIVE
    if ((Split-Path -Leaf $p) -ne 'ingest.key') { throw 'fixed_path_leaf_violation' }
    if ((Split-Path -Leaf (Split-Path -Parent $p)) -ne '46') { throw 'fixed_path_parent_violation' }
    if ((Split-Path -Leaf (Split-Path -Parent (Split-Path -Parent $p))) -ne 'copier-emitter') { throw 'fixed_path_grandparent_violation' }
    return $p
}

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

function Get-TextByPath($xml, [string]$xpath) {
    $n = $xml.SelectSingleNode($xpath)
    if ($null -eq $n) { return '' }
    return [string]$n.InnerText
}
function Get-TriggerElementCount($xml) {
    $t = $xml.SelectSingleNode("//*[local-name()='Task']/*[local-name()='Triggers']")
    if ($null -eq $t) { return 0 }
    return [int](@($t.ChildNodes | Where-Object { $_.NodeType -eq [System.Xml.XmlNodeType]::Element }).Count)
}

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

# Validate the installed STANDING emitter task against the reviewed account-46 contract. Returns the guvfx_u_46 SID
# on success, or throws a stable reason string.
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
    # standing emitter: 'run_emitter.py run --config ...\copier-emitter\46\config.json'. Must NOT be the one-shot attest.
    if ($taskArgs -notmatch 'run_emitter\.py' -or $taskArgs -notmatch '(^|\s)run(\s|$)' -or
        $taskArgs -notmatch '--config' -or $taskArgs -match 'run_oneshot_attest' -or $taskArgs -match 'attest') {
        throw 'task_action_drift'
    }
    if ($taskArgs -notmatch ([regex]::Escape($CONFIG_HINT))) { throw 'task_action_config_drift' }
    $expectedSid = Resolve-Sid $PRINCIPAL_NAME
    $actualSid = Resolve-Sid (Get-TextByPath $xml "//*[local-name()='Principals']/*[local-name()='Principal']/*[local-name()='UserId']")
    if (-not $expectedSid -or -not $actualSid -or ($expectedSid -ne $actualSid)) { throw 'task_principal_drift' }
    return $expectedSid
}

# Break inheritance and grant ONLY {tenant:ReadAndExecute, SYSTEM:Full, Administrators:Full}. Owner -> Administrators.
# The standing emitter only READS the key, so the tenant gets Read, never Modify/Write (tighter than the one-shot).
function Set-ProtectedAcl([string]$path, [string]$tenantSid) {
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

# Read-only ACL check: inheritance disabled; allowed principals EXACTLY {tenant, SYSTEM, Administrators}; and the
# tenant rule grants NO write/modify/fullcontrol (read-only). Returns ($aclOk, $tenantReadOnly).
function Test-ProtectedAcl([string]$path, [string]$tenantSid) {
    $acl = Get-Acl -LiteralPath $path
    if (-not $acl.AreAccessRulesProtected) { return @($false, $false) }
    $allowed = @('S-1-5-18', 'S-1-5-32-544', $tenantSid)
    # GRANULAR write/delete/permission bits ONLY. Must NOT include the composite Modify/FullControl rights: those
    # carry the Synchronize (0x100000) and Read bits that ReadAndExecute also sets, which would make a correctly
    # read-only tenant grant appear writable (false positive). A Modify/Write/FullControl grant still trips this
    # mask via its own WriteData/AppendData/Delete bits.
    $writeMask = ([int][System.Security.AccessControl.FileSystemRights]::WriteData -bor
                  [int][System.Security.AccessControl.FileSystemRights]::AppendData -bor
                  [int][System.Security.AccessControl.FileSystemRights]::WriteExtendedAttributes -bor
                  [int][System.Security.AccessControl.FileSystemRights]::WriteAttributes -bor
                  [int][System.Security.AccessControl.FileSystemRights]::Delete -bor
                  [int][System.Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
                  [int][System.Security.AccessControl.FileSystemRights]::ChangePermissions -bor
                  [int][System.Security.AccessControl.FileSystemRights]::TakeOwnership)
    $tenantReadOnly = $true
    foreach ($rule in $acl.Access) {
        if ($rule.IsInherited) { return @($false, $false) }
        $sid = $null
        try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { return @($false, $false) }
        if ($allowed -notcontains $sid) { return @($false, $false) }
        if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { return @($false, $false) }
        if ($sid -eq $tenantSid -and (([int]$rule.FileSystemRights -band [int]$writeMask) -ne 0)) { $tenantReadOnly = $false }
    }
    $ownerSid = $null
    try { $ownerSid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value } catch { $ownerSid = $null }
    if ($ownerSid -and ($allowed -notcontains $ownerSid)) { return @($false, $false) }
    return @($true, $tenantReadOnly)
}

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

function Assert-SafeCaptureMode() {
    if ($VerbosePreference -ne 'SilentlyContinue' -or $DebugPreference -ne 'SilentlyContinue') { throw 'tracing_active' }
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

function Assert-AccountPinned() {
    if ($RESERVED_IDS -contains $AccountId) { throw 'account_refused_reserved' }
    if ($EXPLICIT_REFUSE -contains $AccountId) { throw 'account_refused_explicit' }
    if ($AccountId -ne $PINNED_ACCOUNT) { throw 'account_not_pinned' }
}

# Test seams are REFUSED in production (un-forgeable fail-closed contract).
$seamUsed = ($FixedRootForTest -ne '' -or $null -ne $ReadTaskXml -or $null -ne $ResolvePrincipalSid -or
    $null -ne $ReadSecret -or $null -ne $AfterTempForTest -or $AssumeYes)
if ($seamUsed -and $env:GUVFX_PROVISIONER_SELFTEST -ne '1') {
    Write-Redacted (New-Result $false 'guard' 'test_seam_in_production'); exit 2
}

if ($Remove -and $Verify) { Write-Redacted (New-Result $false 'provision' 'mode_conflict'); exit 2 }
if ($KeyFromStdin -and ($Remove -or $Verify)) { Write-Redacted (New-Result $false 'provision' 'mode_conflict'); exit 2 }

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

    # ----------------------------------------- VERIFY (read-only, value-blind) ----------------------------
    if ($mode -eq 'verify') {
        $tenantSid = Assert-TaskContract
        if (-not (Test-Path -LiteralPath $keyPath)) { $result.reason = 'key_file_absent'; Write-Redacted $result; exit 2 }
        $item = Get-Item -LiteralPath $keyPath -Force
        if ($item -is [System.IO.DirectoryInfo]) { $result.reason = 'key_not_regular_file'; Write-Redacted $result; exit 2 }
        $result.key_file_present = $true
        $result.length_policy_satisfied = ([int]$item.Length -ge $MIN_SECRET_LEN)   # by SIZE; content never read
        $aclRes = Test-ProtectedAcl $keyPath $tenantSid
        $result.acl_verified = [bool]$aclRes[0]
        $result.tenant_read_only = [bool]$aclRes[1]
        $ok = ($result.length_policy_satisfied -and $result.acl_verified -and $result.tenant_read_only)
        $result.ok = $ok
        $result.reason = if ($ok) { 'verified' } else { 'verify_failed' }
        Write-Redacted $result
        exit ([int](-not $ok))
    }

    # ----------------------------------------- REMOVE (idempotent; teardown) ------------------------------
    if ($mode -eq 'remove') {
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
    $tenantSid = Assert-TaskContract
    if (Test-Path -LiteralPath $keyPath) { $result.reason = 'key_file_exists'; Write-Redacted $result; exit 2 }

    $stdinPhrase = $null; $stdinKey = $null
    if ($KeyFromStdin) {
        $stdinRaw = [Console]::In.ReadToEnd()
        if ([string]::IsNullOrEmpty($stdinRaw)) { $result.reason = 'stdin_empty'; Write-Redacted $result; exit 2 }
        $nlIdx = $stdinRaw.IndexOf("`n")
        if ($nlIdx -lt 0) { $result.reason = 'stdin_malformed'; Write-Redacted $result; exit 2 }
        $stdinPhrase = $stdinRaw.Substring(0, $nlIdx).TrimEnd("`r")
        $stdinKey = ($stdinRaw.Substring($nlIdx + 1)) -replace "(`r`n|`n|`r)+$", ""
    }

    if (-not $AssumeYes) {
        if ($KeyFromStdin) {
            if ($stdinPhrase -ne $CONFIRM_PHRASE) { $stdinKey = $null; $result.reason = 'confirmation_mismatch'; Write-Redacted $result; exit 2 }
        } else {
            Write-Output ('HUMAN-OPERATED STANDING emitter ingest-key provisioning for GuvFX account ' + $PINNED_ACCOUNT + '.')
            Write-Output 'This writes ONE ACL-restricted key file. It does NOT enable, trigger or run any task, open a window or arm a flag.'
            $c = Read-Host ('To proceed type EXACTLY: ' + $CONFIRM_PHRASE)
            if ($c -ne $CONFIRM_PHRASE) { $result.reason = 'confirmation_mismatch'; Write-Redacted $result; exit 2 }
        }
    }

    if ($KeyFromStdin) {
        $plain = [string]$stdinKey
        $stdinKey = $null
    } elseif ($null -ne $ReadSecret) {
        $plain = [string](& $ReadSecret)
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

    if (-not (Test-Path -LiteralPath $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Set-ProtectedDirAcl $dir $tenantSid
    }
    if (-not (Test-NoReparseChain $keyPath)) { $plain = $null; $result.reason = 'reparse_point_detected'; Write-Redacted $result; exit 2 }
    if (Test-Path -LiteralPath $keyPath) { $plain = $null; $result.reason = 'key_file_exists'; Write-Redacted $result; exit 2 }

    $tmp = Join-Path $dir ('.ingest.key.tmp.' + [guid]::NewGuid().ToString('N'))
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    try {
        [System.IO.File]::WriteAllText($tmp, '', $utf8)
        Set-ProtectedAcl $tmp $tenantSid
        [System.IO.File]::WriteAllText($tmp, $plain, $utf8)
        $plain = $null
        if ($null -ne $AfterTempForTest) { & $AfterTempForTest }
        [System.IO.File]::Move($tmp, $keyPath)
        $tmp = $null
        Set-ProtectedAcl $keyPath $tenantSid
    } catch {
        $plain = $null
        if ($tmp -and (Test-Path -LiteralPath $tmp)) { Remove-SecretFile $tmp }
        Remove-TempSiblings $dir
        $result.reason = 'write_failed'; Write-Redacted $result; exit 2
    }

    $result.key_file_present = (Test-Path -LiteralPath $keyPath)
    $aclRes = Test-ProtectedAcl $keyPath $tenantSid
    $result.acl_verified = [bool]$aclRes[0]
    $result.tenant_read_only = [bool]$aclRes[1]
    $ok = ($result.key_file_present -and $result.acl_verified -and $result.tenant_read_only)
    if (-not $ok) { Remove-SecretFile $keyPath }
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

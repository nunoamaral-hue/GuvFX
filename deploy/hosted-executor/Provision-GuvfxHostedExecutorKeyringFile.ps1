#Requires -Version 5.1
# ASCII-only (security.md RULE 9: must parse under Windows PowerShell 5.1 with no BOM; written ASCII-only so it
# parses identically under any encoding). GuvFX hosted signed executor - VALUE-BLIND operator that copies the
# EXISTING machine-variable HMAC keyring + active key id into ONE fixed, ACL-protected file the daemon reads
# FILE-FIRST (GFX-HOSTED-EXECUTOR-FILE-BASED-KEYRING-144 / ADR-0112). Repository tooling: NOT installed, NOT
# scheduled, NOT run by any agent. A human operator runs it locally over RDP (as an administrator).
#
# WHY THIS EXISTS
#   The Windows Service Control Manager caches the system environment block AT BOOT, so a machine-variable keyring
#   rotation is invisible to a restarted GuvFXHostedExecutor service until the host reboots (a service restart
#   inherits the SCM's stale cached block). Reading the keyring from a file the operator rewrites atomically
#   removes that reboot coupling: after this writer runs, a plain Restart-Service picks up the current keyring.
#
# SECURITY CONTRACT (fail-closed everywhere)
#   * VALUE-BLIND: the keyring + active id are read from the Windows MACHINE environment variables
#     (HOSTED_EXECUTOR_KEYRING / HOSTED_EXECUTOR_KEY_ID) and copied into the file. Neither value is EVER printed,
#     hashed, logged, returned, placed in argv, or persisted anywhere but the one protected file. The result
#     object carries ONLY safe metadata (ok, action, file_present, acl_verified, key_count, active_present,
#     correlation_id, at). It refuses to run under active transcription or Verbose/Debug tracing.
#   * Validation (fail closed): the machine ring must be non-empty JSON {key_id: non-empty-secret}; the active id
#     must be present AND a key in the ring; the destination + parent chain carry no reparse point/symlink/junction.
#   * Atomic protected write: a same-directory temp file is created, inheritance is broken and ONLY
#     {SYSTEM (Full), Administrators (Full), <service principal> (Read)} are granted BEFORE the secret bytes land;
#     the temp is read back and re-validated (key_count + active_present), then atomically renamed onto the fixed
#     path (which therefore never exists with broad inherited permissions). Temp artefacts are removed on success
#     and failure.
#   * -Verify: read-only independent check (path/regular-file/no-reparse/ACL/key_count/active_present) that NEVER
#     reads or prints any secret value; it re-parses the file ONLY to count keys + confirm the active id is a key.
#   * -Remove: idempotent best-effort overwrite+delete of the fixed file (+ this writer's temp siblings); reports
#     only redacted state. It NEVER touches the machine variable or the service.
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$Verify,
    [string]$ServicePrincipal = 'NT SERVICE\GuvFXHostedExecutor',   # the exact identity the daemon runs as; granted Read
    # ---- test-only injectable seams (production uses the real defaults) ----
    [string]$FixedPathForTest = '',                 # sandbox ONLY the destination file path (filename stays fixed)
    [scriptblock]$ReadMachineRing = $null,          # () -> the ring JSON string (tests inject synthetic JSON)
    [scriptblock]$ReadMachineActiveId = $null,      # () -> the active id string (tests inject synthetic id)
    [scriptblock]$ResolvePrincipalSid = $null,      # (ref) -> canonical SID string, or $null (fail closed)
    [scriptblock]$AfterTempForTest = $null          # test-only: invoked after the restricted temp write, before the
)                                                   # atomic rename, so a test can throw to exercise catch/cleanup.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
Set-PSDebug -Off

# ---- fixed, non-negotiable scope ----
$RING_VAR = 'HOSTED_EXECUTOR_KEYRING'
$ID_VAR = 'HOSTED_EXECUTOR_KEY_ID'
$FIXED_PATH = 'C:\GuvFX\hosted\secrets\hosted_executor_keyring.json'
$FIXED_LEAF = 'hosted_executor_keyring.json'

function Write-Redacted($obj) { $obj | ConvertTo-Json -Compress }

# A redacted result NEVER carries key material, a key-derived digest, a slice of a key, or a raw dump of it.
function New-Result([bool]$ok, [string]$action, [string]$reason) {
    return [ordered]@{
        ok = $ok; action = $action; file_present = $false; acl_verified = $false;
        key_count = 0; active_present = $false; reason = $reason;
        correlation_id = ([guid]::NewGuid().ToString('N')); at = ((Get-Date).ToUniversalTime().ToString('o'))
    }
}

function Resolve-KeyFilePath() {
    $p = if ($FixedPathForTest) { $FixedPathForTest } else { $FIXED_PATH }
    if ((Split-Path -Leaf $p) -ne $FIXED_LEAF) { throw 'fixed_path_leaf_violation' }
    return $p
}

# Walk the path upward; refuse if ANY existing component is a reparse point (symlink/junction/mount).
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

# Canonical SID resolution (SID / bare name / DOMAIN\name -> SID) through the seam (default: authoritative
# Windows NTAccount/SecurityIdentifier translation). Fail closed (return $null) on any failure.
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

# Read the MACHINE keyring var + active id (value-blind: used internally only, never emitted). Returns a
# hashtable @{Ring=<json string>; Active=<id string>}. Tests inject synthetic values through the seams.
function Read-MachineKeyring() {
    $ring = if ($null -ne $ReadMachineRing) { [string](& $ReadMachineRing) } else { [string][Environment]::GetEnvironmentVariable($RING_VAR, 'Machine') }
    $active = if ($null -ne $ReadMachineActiveId) { [string](& $ReadMachineActiveId) } else { [string][Environment]::GetEnvironmentVariable($ID_VAR, 'Machine') }
    return @{ Ring = $ring; Active = $active }
}

# Parse the ring JSON and return value-blind metadata ONLY: @{Count; Names}. Names are KEY IDS (non-secret wire
# fields), never secret values. Throws a stable reason string on any structural problem (fail closed).
function Get-RingMetadata([string]$ringJson) {
    if ([string]::IsNullOrWhiteSpace($ringJson)) { throw 'machine_ring_absent' }
    $o = $null
    try { $o = $ringJson | ConvertFrom-Json } catch { throw 'machine_ring_invalid_json' }
    if ($null -eq $o -or $o -isnot [psobject]) { throw 'machine_ring_not_object' }
    $names = @($o.PSObject.Properties | ForEach-Object { [string]$_.Name })
    if ($names.Count -lt 1) { throw 'machine_ring_empty' }
    foreach ($pr in $o.PSObject.Properties) {
        if ([string]::IsNullOrEmpty([string]$pr.Value)) { throw 'machine_ring_empty_secret' }
    }
    return @{ Count = [int]$names.Count; Names = $names }
}

# Break inheritance and grant ONLY {SYSTEM:Full, Administrators:Full, <service principal>:Read}. Owner set to
# Administrators (best-effort). No Users / Everyone / Authenticated Users retains access.
function Set-ProtectedAcl([string]$path, [string]$principalSid) {
    $acl = Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true, $false)                          # protect + DO NOT copy inherited ACEs
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
    $sys = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
    $admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $svc = New-Object System.Security.Principal.SecurityIdentifier($principalSid)
    $full = [System.Security.AccessControl.FileSystemRights]::FullControl
    $read = [System.Security.AccessControl.FileSystemRights]::Read
    $none = [System.Security.AccessControl.InheritanceFlags]::None
    $noprop = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sys, $full, $none, $noprop, $allow)))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($admins, $full, $none, $noprop, $allow)))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($svc, $read, $none, $noprop, $allow)))
    try { $acl.SetOwner($admins) } catch { }                             # best-effort; verifier accepts {SYSTEM,Admins,svc}
    Set-Acl -LiteralPath $path -AclObject $acl
}

# Read-only ACL check: inheritance disabled AND the allowed principals are EXACTLY {SYSTEM, Administrators, svc}.
function Test-ProtectedAcl([string]$path, [string]$principalSid) {
    $acl = Get-Acl -LiteralPath $path
    if (-not $acl.AreAccessRulesProtected) { return $false }
    $allowed = @('S-1-5-18', 'S-1-5-32-544', $principalSid)
    foreach ($rule in $acl.Access) {
        if ($rule.IsInherited) { return $false }
        $sid = $null
        try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { return $false }
        if ($allowed -notcontains $sid) { return $false }
        if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { return $false }
    }
    $ownerSid = $null
    try { $ownerSid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value } catch { $ownerSid = $null }
    if (-not $ownerSid -or ($allowed -notcontains $ownerSid)) { return $false }   # indeterminate owner fails closed
    return $true
}

# Read-only DIRECTORY ACL check: inheritance disabled AND the allowed principals are EXACTLY {SYSTEM,
# Administrators, svc}. Mirrors Test-ProtectedAcl (the per-principal identity is the security property; the dir
# grants the svc ReadAndExecute rather than Read, which is why rights are not matched here).
function Test-ProtectedDirAcl([string]$path, [string]$principalSid) {
    $acl = Get-Acl -LiteralPath $path
    if (-not $acl.AreAccessRulesProtected) { return $false }
    $allowed = @('S-1-5-18', 'S-1-5-32-544', $principalSid)
    foreach ($rule in $acl.Access) {
        if ($rule.IsInherited) { return $false }
        $sid = $null
        try { $sid = $rule.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value } catch { return $false }
        if ($allowed -notcontains $sid) { return $false }
        if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { return $false }
    }
    $ownerSid = $null
    try { $ownerSid = $acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value } catch { $ownerSid = $null }
    if (-not $ownerSid -or ($allowed -notcontains $ownerSid)) { return $false }
    return $true
}

# ACL for a directory WE create: inheritance broken; SYSTEM + Administrators Full; the service principal gets
# ReadAndExecute only (traverse + list) but NO write - it cannot plant a junction or tamper.
function Set-ProtectedDirAcl([string]$path, [string]$principalSid) {
    $acl = Get-Acl -LiteralPath $path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($rule in @($acl.Access)) { [void]$acl.RemoveAccessRule($rule) }
    $sys = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
    $admins = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-32-544')
    $svc = New-Object System.Security.Principal.SecurityIdentifier($principalSid)
    $full = [System.Security.AccessControl.FileSystemRights]::FullControl
    $rx = [System.Security.AccessControl.FileSystemRights]::ReadAndExecute
    $none = [System.Security.AccessControl.InheritanceFlags]::None
    $noprop = [System.Security.AccessControl.PropagationFlags]::None
    $allow = [System.Security.AccessControl.AccessControlType]::Allow
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($sys, $full, $none, $noprop, $allow)))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($admins, $full, $none, $noprop, $allow)))
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($svc, $rx, $none, $noprop, $allow)))
    try { $acl.SetOwner($admins) } catch { }
    Set-Acl -LiteralPath $path -AclObject $acl
}

# Best-effort overwrite-then-delete of ONE file. The DELETE is the guarantee.
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
            foreach ($pat in @('.hosted_executor_keyring.json.tmp.*', 'hosted_executor_keyring.json.rotating.bak')) {
                Get-ChildItem -LiteralPath $dir -Filter $pat -Force -ErrorAction SilentlyContinue |
                    ForEach-Object { try { Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue } catch { } }
            }
        }
    } catch { }
}

# ---- transcription / tracing guard: fail closed if a capture mode could record the secret ----
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

# =====================================================================================================================
# Test seams are REFUSED in production. Any seam (sandbox path, the machine-var/SID injectors, or the failure
# injector) is honoured ONLY when the self-test signal is set - a signal the production launch path never sets.
$seamUsed = ($FixedPathForTest -ne '' -or $null -ne $ReadMachineRing -or $null -ne $ReadMachineActiveId -or
    $null -ne $ResolvePrincipalSid -or $null -ne $AfterTempForTest)
if ($seamUsed -and $env:GUVFX_PROVISIONER_SELFTEST -ne '1') {
    Write-Redacted (New-Result $false 'guard' 'test_seam_in_production'); exit 2
}
if ($Remove -and $Verify) { Write-Redacted (New-Result $false 'provision' 'mode_conflict'); exit 2 }

$mode = if ($Remove) { 'remove' } elseif ($Verify) { 'verify' } else { 'provision' }
$result = New-Result $false $mode 'not_started'
$keyPath = $null
$ring = $null

try {
    $keyPath = Resolve-KeyFilePath
    $dir = Split-Path -Parent $keyPath
    if (-not (Test-NoReparseChain $keyPath)) { $result.reason = 'reparse_point_detected'; Write-Redacted $result; exit 2 }

    $principalSid = Resolve-Sid $ServicePrincipal
    if (-not $principalSid) { $result.reason = 'service_principal_unresolved'; Write-Redacted $result; exit 2 }

    # ----------------------------------------- VERIFY (read-only) -----------------------------------------
    if ($mode -eq 'verify') {
        if (-not (Test-Path -LiteralPath $keyPath)) { $result.reason = 'file_absent'; Write-Redacted $result; exit 2 }
        $item = Get-Item -LiteralPath $keyPath -Force
        if ($item -is [System.IO.DirectoryInfo]) { $result.reason = 'not_regular_file'; Write-Redacted $result; exit 2 }
        $result.file_present = $true
        $result.acl_verified = (Test-ProtectedAcl $keyPath $principalSid)
        # value-blind content sanity: parse, count keys, confirm active id is a key. No value is read/emitted.
        try {
            $doc = (Get-Content -LiteralPath $keyPath -Raw) | ConvertFrom-Json
            $names = @($doc.keyring.PSObject.Properties | ForEach-Object { [string]$_.Name })
            $result.key_count = [int]$names.Count
            $result.active_present = ($names -contains [string]$doc.key_id -and -not [string]::IsNullOrWhiteSpace([string]$doc.key_id))
        } catch { $result.key_count = 0; $result.active_present = $false }
        $ok = ($result.acl_verified -and $result.key_count -ge 1 -and $result.active_present)
        $result.ok = $ok
        $result.reason = if ($ok) { 'verified' } else { 'verify_failed' }
        Write-Redacted $result
        exit ([int](-not $ok))
    }

    # ----------------------------------------- REMOVE (idempotent shred+delete) ---------------------------
    if ($mode -eq 'remove') {
        if (Test-Path -LiteralPath $keyPath) {
            $item = Get-Item -LiteralPath $keyPath -Force
            if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq [System.IO.FileAttributes]::ReparsePoint) {
                $result.reason = 'remove_target_reparse'; Write-Redacted $result; exit 2
            }
            Remove-SecretFile $keyPath
        }
        Remove-TempSiblings $dir
        $result.file_present = (Test-Path -LiteralPath $keyPath)
        $ok = (-not $result.file_present)
        $result.ok = $ok
        $result.reason = if ($ok) { 'removed' } else { 'remove_failed' }
        Write-Redacted $result
        exit ([int](-not $ok))
    }

    # ----------------------------------------- PROVISION ------------------------------------------------
    Assert-SafeCaptureMode
    $mk = Read-MachineKeyring
    $ringJson = ([string]$mk.Ring).Trim()
    $activeId = ([string]$mk.Active).Trim()
    $meta = Get-RingMetadata $ringJson                               # throws a stable reason if the ring is bad
    if ([string]::IsNullOrWhiteSpace($activeId)) { $result.reason = 'active_id_absent'; Write-Redacted $result; exit 2 }
    if ($meta.Names -notcontains $activeId) { $result.reason = 'active_id_not_in_ring'; Write-Redacted $result; exit 2 }
    $result.key_count = [int]$meta.Count
    $result.active_present = $true

    # Create the fixed directory tree (admin-controlled) if missing. On EVERY run (including rotation, where the
    # dir already exists) RE-ASSERT the protected dir ACL and VERIFY it before trusting the dir for the atomic
    # temp write -- a dir created outside this script could carry broad inherited ACEs that would defeat the
    # temp-window / anti-planting argument. Then re-verify the FULL path chain carries NO reparse point.
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Set-ProtectedDirAcl $dir $principalSid
    if (-not (Test-ProtectedDirAcl $dir $principalSid)) { $result.reason = 'dir_acl_failed'; Write-Redacted $result; exit 2 }
    if (-not (Test-NoReparseChain $keyPath)) { $result.reason = 'reparse_point_detected'; Write-Redacted $result; exit 2 }

    # Build the file content by composing the ring JSON VERBATIM (no re-serialisation of secret values) with the
    # non-secret active id. The whole string is a secret-bearing artefact and is NEVER written to stdout/log.
    $idJson = ConvertTo-Json ([string]$activeId) -Compress
    $content = '{"keyring":' + $ringJson + ',"key_id":' + $idJson + '}'

    # atomic protected write: restrict the same-dir temp BEFORE the secret bytes land, then atomically PLACE it.
    $tmp = Join-Path $dir ('.hosted_executor_keyring.json.tmp.' + [guid]::NewGuid().ToString('N'))
    $bak = $keyPath + '.rotating.bak'
    $utf8 = New-Object System.Text.UTF8Encoding($false)              # UTF-8, no BOM
    try {
        [System.IO.File]::WriteAllText($tmp, '', $utf8)             # create empty (no secret yet)
        Set-ProtectedAcl $tmp $principalSid                        # restrict BEFORE writing the secret
        [System.IO.File]::WriteAllText($tmp, $content, $utf8)       # secret bytes into the already-restricted temp
        $content = $null; $ringJson = $null
        # value-blind read-back BEFORE placing: the temp must parse and carry EXACTLY the same key_count + active.
        $rb = (Get-Content -LiteralPath $tmp -Raw) | ConvertFrom-Json
        $rbNames = @($rb.keyring.PSObject.Properties | ForEach-Object { [string]$_.Name })
        if ([int]$rbNames.Count -ne [int]$meta.Count -or ($rbNames -notcontains $activeId) -or ([string]$rb.key_id -ne $activeId)) {
            throw 'readback_mismatch'
        }
        if ($null -ne $AfterTempForTest) { & $AfterTempForTest }    # test-only failure injection (production: null)
        # Place atomically. ROTATION (dest exists) uses File.Replace: atomic AND crash-safe -- on ANY failure the
        # destination is left UNCHANGED (the prior valid keyring), never absent; the prior content goes to $bak and
        # is shredded on success. FIRST provision (dest absent) uses Move onto the absent path. The admin-owned
        # directory ACL (re-asserted + verified above; service principal traverse-only, no write) prevents any
        # non-admin from planting a reparse/file during placement.
        if (Test-Path -LiteralPath $keyPath) {
            $destItem = Get-Item -LiteralPath $keyPath -Force
            if (($destItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -eq [System.IO.FileAttributes]::ReparsePoint) { throw 'dest_reparse' }
            if (Test-Path -LiteralPath $bak) { Remove-SecretFile $bak }
            [System.IO.File]::Replace($tmp, $keyPath, $bak)        # atomic replace; dest unchanged on failure
            $tmp = $null
            Remove-SecretFile $bak
        } else {
            [System.IO.File]::Move($tmp, $keyPath)                 # atomic rename onto the absent fixed path
            $tmp = $null
        }
    } catch {
        $content = $null; $ringJson = $null
        if ($tmp -and (Test-Path -LiteralPath $tmp)) { Remove-SecretFile $tmp }
        if (Test-Path -LiteralPath $bak) { Remove-SecretFile $bak }
        Remove-TempSiblings $dir
        $result.reason = 'write_failed'; Write-Redacted $result; exit 2
    }

    # Post-placement OUTSIDE the write try/catch (so a re-assert/verify failure cannot leave the reported status
    # diverging from disk): defensively re-assert the ACL (best-effort), then AUTHORITATIVELY verify it. If the
    # final file is not correctly protected, SHRED it -- never leave a weakly-protected secret -- so file_present
    # and acl_verified both reflect the real on-disk state.
    try { Set-ProtectedAcl $keyPath $principalSid } catch { }
    $aclOk = $false
    try { $aclOk = (Test-ProtectedAcl $keyPath $principalSid) } catch { $aclOk = $false }
    if (-not $aclOk -and (Test-Path -LiteralPath $keyPath)) { Remove-SecretFile $keyPath }
    $result.file_present = (Test-Path -LiteralPath $keyPath)
    $result.acl_verified = $aclOk
    $ok = ($result.file_present -and $result.acl_verified)
    $result.ok = $ok
    $result.reason = if ($ok) { 'provisioned' } else { 'acl_failed' }
    Write-Redacted $result
    exit ([int](-not $ok))
}
catch {
    $reason = [string]$_.Exception.Message
    if ([string]::IsNullOrWhiteSpace($reason)) { $reason = 'error' }
    $result.ok = $false; $result.reason = $reason
    Write-Redacted $result
    exit 2
}
finally {
    $ring = $null
    [System.GC]::Collect()
}

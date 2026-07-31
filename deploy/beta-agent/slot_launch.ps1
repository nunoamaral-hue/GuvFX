# slot_launch.ps1 -- GuvFX beta per-slot launch wrapper (ADR-0016 Option A).
#
# Runs AS the slot identity guvfx_b_slot<n> (the launch task's principal). It:
#   1. creates the slot's terminal64.exe SUSPENDED (/portable, hard-coded here -- never taken from an argument;
#      may ALSO pass a tightly-controlled /config:<derived slot file> so an approved EA auto-attaches -- see 1b),
#   2. adds ONE discretionary ACE to that process OBJECT granting the beta-agent service SID
#      PROCESS_QUERY_LIMITED_INFORMATION | READ_CONTROL (0x21000) -- read-modify-write, never a DACL replace,
#   3. reads the DACL back and asserts the ACE is present,
#   4. resumes the process.
# On ANY failure it TerminateProcess-es the still-suspended child (via the handle it created -- never by image
# name, which the production terminal shares) and exits non-zero, so nothing ever runs un-observably.
#
# The ACE is an INTRINSIC property of the process object: it is destroyed with the process, never persisted,
# never inherited by children. There is NO revocation step (ADR-0016).
#
# CONTRACT / SAFETY:
#   * ASCII-only, no BOM; must pass [Parser]::ParseFile under Windows PowerShell 5.1 (RULE 9). The embedded C#
#     is validated at commissioning by an interop self-test (GetKernelObjectSecurity on this process) BEFORE
#     terminal64 is launched -- ParseFile does not compile the C#.
#   * The grantee SID must be a service SID (S-1-5-80-...) that translates back to NT SERVICE\GuvFXBetaAgent;
#     anything else is refused. The terminal path must live beneath the beta slots root and be terminal64.exe.
#   * This file lives in an admin-only-writable directory (C:\GuvFX\beta\launcher); a slot cannot rewrite it.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$TerminalPath,
    [Parameter(Mandatory = $true)][string]$WorkingDirectory,
    [Parameter(Mandatory = $true)][string]$GranteeSid,
    # Swallows the inert '/portable' token the launch task carries for the digest/portable-switch detectors.
    # It is DELIBERATELY not forwarded to terminal64: /portable is hard-coded below, so a task argument can
    # never control what terminal64 runs with (no injection surface).
    [Parameter(ValueFromRemainingArguments = $true)]$IgnoredRest
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$SLOTS_ROOT = 'C:\GuvFX\beta\slots\'
$EXPECTED_GRANTEE_ACCOUNT = 'NT SERVICE\GuvFXBetaAgent'
# The exact granted mask (0x21000 = PROCESS_QUERY_LIMITED_INFORMATION | READ_CONTROL) is the single C# const
# GuvfxLaunchGrant.GRANT_MASK below; it is the ONE source of truth for both the ACE and its read-back check.

function Fail([string]$msg) {
    # Write to stderr directly, NOT Write-Error: under $ErrorActionPreference='Stop' Write-Error raises a
    # terminating error, so 'exit 2' would be unreachable and powershell.exe -File would report exit code 1.
    [Console]::Error.WriteLine("slot_launch: " + $msg)
    exit 2
}

function Apply-LiveUpdateContainment {
    # Variant A LiveUpdate containment. Runs AS the slot identity, BEFORE terminal64 is launched. MT5's
    # LiveUpdate stages a copy of terminal64.exe into the slot account's ROAMING profile
    # (%APPDATA%\MetaQuotes\WebInstall and %APPDATA%\MetaQuotes\Terminal\<hash>\liveupdate) and relaunches
    # from there - an executable OUTSIDE the slot, which breaks is_beneath VERIFY and the exact-path STOP
    # task (both proven on the host: without this, the relocated exe survives the STOP task). This denies the
    # slot identity WRITE on its OWN update-staging so MT5 cannot stage a relocation and always runs from the
    # canonical <slot>\terminal64.exe. It PRESERVES executable-path containment (never weakens VERIFY or STOP,
    # never broadens process matching); it adds NO new privilege (the slot owns its own profile, so the
    # wrapper can set this DACL with no admin/SeRestorePrivilege); it is idempotent and self-cleaning (each
    # launch purges any stale staged exe and re-establishes the Deny, so generation N+1 begins clean); and it
    # is FAIL-CLOSED - if containment cannot be proven in force, terminal64 is NOT launched, because a
    # relocated runtime would be unstoppable by the exact-path STOP task.
    $roaming = $env:APPDATA
    if ([string]::IsNullOrWhiteSpace($roaming)) {
        if ([string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
            Fail "LiveUpdate containment: cannot resolve the slot roaming profile (APPDATA and USERPROFILE both unset)"
        }
        $roaming = Join-Path $env:USERPROFILE 'AppData\Roaming'
    }
    $mqRoot = Join-Path $roaming 'MetaQuotes'
    # The wrapper runs AS the slot identity, so the CURRENT token's user IS the slot SID. Deny that SID by
    # value - no NTAccount translation, which hangs on this workgroup host (same reason the launch-grant path
    # reads SIDs, not names).
    $slotSid = ([System.Security.Principal.WindowsIdentity]::GetCurrent()).User
    # Update-staging paths. WebInstall (download, fixed name) is the LOAD-BEARING chokepoint: it is denied
    # unconditionally, and host proof (2026-07-31) showed denying it alone stops MT5 obtaining the update, so
    # nothing is ever staged to relocate. The per-hash Terminal\<hash>\liveupdate Denies are best-effort
    # defence-in-depth for hashes that ALREADY exist at launch (a fresh portable slot has none). Only the
    # slot's OWN roaming staging is touched - never the slot dir, never the operator estate, never another
    # identity's profile.
    $targets = New-Object System.Collections.Generic.List[string]
    $targets.Add((Join-Path $mqRoot 'WebInstall'))
    $terminalRoot = Join-Path $mqRoot 'Terminal'
    if (Test-Path -LiteralPath $terminalRoot) {
        foreach ($d in (Get-ChildItem -LiteralPath $terminalRoot -Directory -ErrorAction SilentlyContinue)) {
            $targets.Add((Join-Path $d.FullName 'liveupdate'))
        }
    }
    $writeRights = [System.Security.AccessControl.FileSystemRights]::Write
    $inherit = [System.Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [System.Security.AccessControl.InheritanceFlags]::ObjectInherit
    foreach ($t in $targets) {
        # A concrete target for the Deny at first launch (hash-independent for the fixed WebInstall path).
        if (-not (Test-Path -LiteralPath $t)) {
            try { New-Item -ItemType Directory -Force -Path $t | Out-Null }
            catch { Fail ("LiveUpdate containment: cannot create staging target " + $t + ": " + $_.Exception.Message) }
        }
        # Deterministic cleanup: empty the staging dir of ANY payload a prior occupancy left (a relocated
        # terminal64.exe and any downloaded update parts), so generation N+1 begins genuinely clean. These
        # dirs hold ONLY MT5 update staging, so emptying them removes nothing the runtime needs.
        try {
            Get-ChildItem -LiteralPath $t -Force -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction Stop
        }
        catch { Fail ("LiveUpdate containment: cannot purge stale staging under " + $t + ": " + $_.Exception.Message) }
        # Idempotent Deny(Write): drop any prior identical Deny we added, then add exactly one inheritable
        # Deny. Repeated launches never accumulate ACEs. The slot's inherited FullControl Allow is untouched -
        # this only ADDS a Deny, which takes precedence over Allow for the Write bits.
        try {
            $acl = Get-Acl -LiteralPath $t
            $denyRule = New-Object System.Security.AccessControl.FileSystemAccessRule($slotSid, $writeRights, $inherit, [System.Security.AccessControl.PropagationFlags]::None, [System.Security.AccessControl.AccessControlType]::Deny)
            [void]$acl.RemoveAccessRule($denyRule)
            [void]$acl.AddAccessRule($denyRule)
            Set-Acl -LiteralPath $t -AclObject $acl
        }
        catch { Fail ("LiveUpdate containment: cannot apply Deny on " + $t + ": " + $_.Exception.Message) }
        # POSITIVE CONTROL (RULE 11): read the DACL back and assert an explicit slot-SID Deny that carries
        # every Write bit is now in force. A grant that was requested but not verified is not a control.
        # Enumerate BY SID (GetAccessRules with SecurityIdentifier), NEVER the default .Access - .Access
        # name-translates every ACE to DOMAIN\name, so IdentityReference.Value would be 'HOST\guvfx_b_slot1'
        # and could never equal the SID string $slotSid.Value, leaving $verified permanently false and failing
        # every launch closed. This mirrors install_pool.ps1's SID-typed read-back convention.
        $verified = $false
        foreach ($rule in (Get-Acl -LiteralPath $t).GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq [System.Security.AccessControl.AccessControlType]::Deny -and
                (-not $rule.IsInherited) -and
                $rule.IdentityReference.Value -eq $slotSid.Value -and
                (([int]$rule.FileSystemRights -band [int]$writeRights) -eq [int]$writeRights)) {
                $verified = $true
            }
        }
        if (-not $verified) {
            Fail ("LiveUpdate containment: read-back could not confirm the Deny is in force on " + $t)
        }
    }
    Write-Host ("slot_launch: LiveUpdate containment in force (Deny-write for the slot identity on " + $targets.Count + " staging path(s))")
}

# -- 1. Validate arguments (defense in depth; the launch gate and approved-task digest also bind these). ----
if ([string]::IsNullOrWhiteSpace($TerminalPath)) { Fail "TerminalPath is empty" }
$full = [System.IO.Path]::GetFullPath($TerminalPath)
if (-not $full.ToLowerInvariant().StartsWith($SLOTS_ROOT.ToLowerInvariant())) {
    Fail ("TerminalPath is not beneath the beta slots root: " + $full)
}
if ([System.IO.Path]::GetFileName($full).ToLowerInvariant() -ne 'terminal64.exe') {
    Fail ("TerminalPath is not terminal64.exe: " + $full)
}
if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { Fail ("TerminalPath does not exist: " + $full) }

# The working directory becomes terminal64's CWD, so validate it beneath the slots root too (symmetric with
# TerminalPath) - a CWD outside the slot could change DLL search behaviour. GetFullPath first (no traversal).
if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) { Fail "WorkingDirectory is empty" }
$workFull = [System.IO.Path]::GetFullPath($WorkingDirectory)
if (-not $workFull.ToLowerInvariant().StartsWith($SLOTS_ROOT.ToLowerInvariant())) {
    Fail ("WorkingDirectory is not beneath the beta slots root: " + $workFull)
}

# -- 1b. Optional tightly-controlled startup config (ADR-0016 extension). The wrapper MAY pass terminal64 a
#        /config:<file> so an approved EA auto-attaches. In Session 0 the chart/MDI GUI fails, so the normal
#        profile-restore attach path does not work; a startup config is the only lifecycle-native way to attach
#        an EA. Controls that keep this injection-safe:
#          * DERIVED path (a FIXED filename beneath the already-validated WorkingDirectory) - NEVER a task/command
#            argument, so a tenant cannot point it elsewhere;
#          * trusted on PROVENANCE, not writability. The slot working dir is slot-WRITABLE (terminal64 /portable
#            stores its data there), so an in-slot tenant CAN create guvfx_startup.ini. A mere write-probe is
#            UNSOUND: the file's owner can drop its own FILE_WRITE_DATA (or set +r) and still control the content,
#            and an inherited Modify grant lets a slot delete-and-replace an admin file. So the config is honoured
#            ONLY when its OWNER is Administrators or SYSTEM - a non-admin identity cannot set a file's owner to
#            those (needs SeRestorePrivilege), so a slot-authored config is always slot-owned and is rejected;
#          * DACL: refuse if any non-admin/non-SYSTEM principal holds write/delete/change-perms/take-ownership;
#          * reject reparse points; the path must be whitespace-free (an unquoted /config: splits on a space);
#          * TOCTOU: pin the file with a deny-write + deny-delete share handle held ACROSS the launch, so it
#            cannot be swapped between validation and terminal64 opening it.
#        INTEGRITY, NOT CONFIDENTIALITY: whatever terminal64 reads, in-slot code can read too (both run as the
#        slot identity), so a credential in the config is NOT secret from the tenant. Only a DISPOSABLE demo login
#        may ever be placed in it; production/live credentials must not. The wrapper never reads the content.
$ConfigPathToPass = ''
$ConfigHandle = $null
$ADMIN_SID  = 'S-1-5-32-544'
$SYSTEM_SID = 'S-1-5-18'
$configCandidate = Join-Path $workFull 'guvfx_startup.ini'
if (Test-Path -LiteralPath $configCandidate -PathType Leaf) {
    $cfgFull = [System.IO.Path]::GetFullPath($configCandidate)
    if (-not $cfgFull.ToLowerInvariant().StartsWith($workFull.ToLowerInvariant())) {
        Fail ("startup config resolved outside the slot working directory: " + $cfgFull)
    }
    if ($cfgFull -match '\s') {
        Fail ("startup config path contains whitespace, refusing to pass /config: " + $cfgFull)
    }
    $cfgItem = Get-Item -LiteralPath $cfgFull -Force
    if (($cfgItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        Write-Host "slot_launch: startup config is a reparse point (untrusted) - ignoring; launching /portable only"
    } else {
        # Pin the file: FileShare.Read denies writers AND deleters while the handle is held. The owner/DACL are
        # then read FROM THIS HANDLE (not by re-opening the path), so validation and the pin describe the SAME
        # file object - a path-level swap between open and validation cannot desync them. The pin is held through
        # the wrapper's validation and the launch trigger; it does not (and is not relied on to) extend to
        # terminal64's own later open of /config:, which is instead kept safe by the OWNER gate (a tenant cannot
        # produce an admin-owned replacement) plus the deployment invariant that the slot dir grants the slot
        # Modify, NOT Full Control (so no FILE_DELETE_CHILD -> the slot cannot delete an admin-owned config).
        $ConfigHandle = [System.IO.File]::Open($cfgFull, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        # SID-typed reads (GetOwner/GetAccessRules with SecurityIdentifier) - NO NTAccount translation, which
        # hangs on this workgroup host.
        $sec = $ConfigHandle.GetAccessControl()
        $ownerSid = $sec.GetOwner([System.Security.Principal.SecurityIdentifier]).Value
        $ownerTrusted = ($ownerSid -eq $ADMIN_SID -or $ownerSid -eq $SYSTEM_SID)
        $writeBits = [int]([System.Security.AccessControl.FileSystemRights]"WriteData, AppendData, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership")
        $nonAdminWrite = $false
        foreach ($rule in $sec.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -ne [System.Security.AccessControl.AccessControlType]::Allow) { continue }
            $rsid = $rule.IdentityReference.Value
            if ($rsid -eq $ADMIN_SID -or $rsid -eq $SYSTEM_SID) { continue }
            if (([int]$rule.FileSystemRights -band $writeBits) -ne 0) { $nonAdminWrite = $true }
        }
        if ($ownerTrusted -and (-not $nonAdminWrite)) {
            $ConfigPathToPass = $cfgFull
            Write-Host ("slot_launch: honouring admin-owned startup config (owner " + $ownerSid + "): " + $cfgFull)
        } else {
            $ConfigHandle.Close(); $ConfigHandle = $null
            if (-not $ownerTrusted) {
                Write-Host ("slot_launch: startup config owner is " + $ownerSid + ", not Administrators/SYSTEM (untrusted) - ignoring; launching /portable only")
            } else {
                Write-Host "slot_launch: startup config grants a non-admin principal write/delete (untrusted) - ignoring; launching /portable only"
            }
        }
    }
}

if ($GranteeSid -notmatch '^S-1-5-80-\d+-\d+-\d+-\d+-\d+$') {
    Fail ("GranteeSid is not a service SID: " + $GranteeSid)
}
try {
    $acct = (New-Object System.Security.Principal.SecurityIdentifier($GranteeSid)).Translate([System.Security.Principal.NTAccount]).Value
} catch {
    Fail ("GranteeSid does not resolve to an account: " + $GranteeSid)
}
if ($acct -ne $EXPECTED_GRANTEE_ACCOUNT) {
    Fail ("GranteeSid resolves to '" + $acct + "', not " + $EXPECTED_GRANTEE_ACCOUNT)
}

# -- 2. Compile the native ACE-grant helper (ASCII-only, single-quoted here-string -- no interpolation). -----
$cs = @'
using System;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;

public static class GuvfxLaunchGrant
{
    const uint CREATE_SUSPENDED = 0x00000004;
    const uint DACL_SECURITY_INFORMATION = 0x00000004;
    // The ONE source of truth for the granted access mask: PROCESS_QUERY_LIMITED_INFORMATION (0x1000) |
    // READ_CONTROL (0x20000). The ACE is built with it AND the read-back asserts EQUALITY against it, so a
    // broader mask (e.g. adding PROCESS_VM_READ) can never be granted and pass verification.
    const int GRANT_MASK = 0x21000;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO
    {
        public int cb;
        public string lpReserved;
        public string lpDesktop;
        public string lpTitle;
        public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
        public short wShowWindow, cbReserved2;
        public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION
    {
        public IntPtr hProcess, hThread;
        public int dwProcessId, dwThreadId;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    static extern bool CreateProcessW(string app, StringBuilder cmd, IntPtr pa, IntPtr ta, bool inherit,
        uint flags, IntPtr env, string cwd, ref STARTUPINFO si, out PROCESS_INFORMATION pi);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint ResumeThread(IntPtr hThread);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool TerminateProcess(IntPtr hProcess, uint code);

    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr h);

    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool GetKernelObjectSecurity(IntPtr h, uint info, byte[] sd, uint len, out uint needed);

    [DllImport("advapi32.dll", SetLastError = true)]
    static extern bool SetKernelObjectSecurity(IntPtr h, uint info, byte[] sd);

    // Self-test the P/Invoke path against a handle we already own, before touching terminal64 (RULE 11).
    public static string SelfTest()
    {
        IntPtr me = System.Diagnostics.Process.GetCurrentProcess().Handle;
        uint needed = 0;
        GetKernelObjectSecurity(me, DACL_SECURITY_INFORMATION, null, 0, out needed);
        if (needed == 0) return "self-test: GetKernelObjectSecurity returned zero length";
        return null;
    }

    static byte[] ReadDacl(IntPtr h)
    {
        uint needed = 0;
        GetKernelObjectSecurity(h, DACL_SECURITY_INFORMATION, null, 0, out needed);
        if (needed == 0) return null;
        byte[] buf = new byte[needed];
        uint got = 0;
        if (!GetKernelObjectSecurity(h, DACL_SECURITY_INFORMATION, buf, needed, out got)) return null;
        return buf;
    }

    static bool HasGrant(IntPtr h, SecurityIdentifier sid)
    {
        byte[] sdb = ReadDacl(h);
        if (sdb == null) return false;
        RawSecurityDescriptor sd = new RawSecurityDescriptor(sdb, 0);
        if (sd.DiscretionaryAcl == null) return false;   // NULL DACL -> not granted (fail closed upstream)
        foreach (GenericAce ace in sd.DiscretionaryAcl)
        {
            CommonAce ca = ace as CommonAce;
            if (ca == null) continue;
            // EQUALITY, not "contains at least": the service ACE must grant EXACTLY GRANT_MASK, so a broader
            // grant (e.g. one that also carries PROCESS_VM_READ) fails verification and is torn down.
            if (ca.AceType == AceType.AccessAllowed && ca.SecurityIdentifier == sid
                && ca.AccessMask == GRANT_MASK) return true;
        }
        return false;
    }

    // Returns 0 on success; a non-zero code identifies the failing stage. On any post-create failure the
    // suspended child is terminated via the handle we created (never by image name) so nothing runs ungranted.
    public static int LaunchAndGrant(string exePath, string workDir, string granteeSid, string configPath)
    {
        SecurityIdentifier sid = new SecurityIdentifier(granteeSid);
        StringBuilder cmd = new StringBuilder();
        cmd.Append('"').Append(exePath).Append("\" /portable");   // /portable is HARD-CODED here
        // ADR-0016 extension: append a tightly-controlled /config:<file>. configPath is empty unless the
        // PowerShell side already proved the file exists, is beneath the slot, is whitespace-free, and is NOT
        // writable by the slot identity. It is a DERIVED path (never a task/command argument), so terminal64's
        // startup config cannot be steered by a tenant.
        if (!string.IsNullOrEmpty(configPath))
            cmd.Append(" /config:").Append(configPath);
        STARTUPINFO si = new STARTUPINFO();
        si.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        PROCESS_INFORMATION pi;
        if (!CreateProcessW(exePath, cmd, IntPtr.Zero, IntPtr.Zero, false, CREATE_SUSPENDED,
                            IntPtr.Zero, workDir, ref si, out pi))
            return 10;                                    // create failed -> nothing to clean up

        try
        {
            byte[] sdb = ReadDacl(pi.hProcess);
            if (sdb == null) { TerminateProcess(pi.hProcess, 1); return 11; }
            RawSecurityDescriptor sd = new RawSecurityDescriptor(sdb, 0);
            if (sd.DiscretionaryAcl == null) { TerminateProcess(pi.hProcess, 1); return 12; } // NULL DACL: fail closed
            RawAcl dacl = sd.DiscretionaryAcl;

            // READ-MODIFY-WRITE: append ONE allow ACE; the existing default ACEs (including the owner's own
            // PROCESS_TERMINATE, which the slot's STOP task needs) are preserved. Never build a fresh DACL.
            CommonAce grant = new CommonAce(AceFlags.None, AceQualifier.AccessAllowed, GRANT_MASK, sid, false, null);
            dacl.InsertAce(dacl.Count, grant);

            byte[] outb = new byte[sd.BinaryLength];
            sd.GetBinaryForm(outb, 0);
            if (!SetKernelObjectSecurity(pi.hProcess, DACL_SECURITY_INFORMATION, outb))
            { TerminateProcess(pi.hProcess, 1); return 13; }

            if (!HasGrant(pi.hProcess, sid))              // read-back verification (mask EQUALS GRANT_MASK)
            { TerminateProcess(pi.hProcess, 1); return 14; }

            if (ResumeThread(pi.hThread) == 0xFFFFFFFF)
            { TerminateProcess(pi.hProcess, 1); return 15; }
            return 0;
        }
        catch
        {
            // Any THROWN failure (e.g. a malformed SD, an oversized ACL from InsertAce) must still tear the
            // suspended child down BEFORE the finally closes our only handle to it - the "on ANY failure
            // terminate the child" contract. Best-effort; the process never ran (CREATE_SUSPENDED).
            try { TerminateProcess(pi.hProcess, 1); } catch { }
            return 16;
        }
        finally
        {
            if (pi.hThread != IntPtr.Zero) CloseHandle(pi.hThread);
            if (pi.hProcess != IntPtr.Zero) CloseHandle(pi.hProcess);  // our handle only; the process lives on
        }
    }
}
'@

try {
    Add-Type -TypeDefinition $cs -Language CSharp -ErrorAction Stop
} catch {
    Fail ("could not compile the ACE-grant helper (Constrained Language Mode?): " + $_.Exception.Message)
}

# -- 3. Interop self-test (positive control) BEFORE launching terminal64. -----------------------------------
$selfErr = [GuvfxLaunchGrant]::SelfTest()
if ($selfErr) { Fail $selfErr }

# -- 3b. LiveUpdate containment (Variant A) BEFORE launch: deny the slot identity write on its OWN roaming
#        MT5 update-staging so MT5 cannot relocate terminal64 outside the slot. Fail-closed: Apply-...
#        calls Fail (exit 2, no launch) if the Deny cannot be established and read back in force.
Apply-LiveUpdateContainment

# -- 4. Launch suspended, grant, verify, resume -- or terminate + fail. -------------------------------------
$rc = [GuvfxLaunchGrant]::LaunchAndGrant($full, $WorkingDirectory, $GranteeSid, $ConfigPathToPass)
# Release the deny-write+deny-delete pin held across the wrapper's validation + launch trigger. (terminal64's
# own later read of /config: is kept safe by the OWNER gate, not by this handle - see the pin comment in 1b.)
if ($ConfigHandle) { $ConfigHandle.Close(); $ConfigHandle = $null }
if ($rc -ne 0) { Fail ("launch/grant failed at stage " + $rc) }

$cfgNote = if ($ConfigPathToPass) { " with startup config" } else { " (/portable only)" }
Write-Host ("slot_launch: launched and granted " + $EXPECTED_GRANTEE_ACCOUNT + " query access to " + $full + $cfgNote)
exit 0

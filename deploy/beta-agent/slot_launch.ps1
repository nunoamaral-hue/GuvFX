# slot_launch.ps1 -- GuvFX beta per-slot launch wrapper (ADR-0016 Option A).
#
# Runs AS the slot identity guvfx_b_slot<n> (the launch task's principal). It:
#   1. creates the slot's terminal64.exe SUSPENDED (/portable, hard-coded here -- never taken from an argument),
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
    public static int LaunchAndGrant(string exePath, string workDir, string granteeSid)
    {
        SecurityIdentifier sid = new SecurityIdentifier(granteeSid);
        StringBuilder cmd = new StringBuilder();
        cmd.Append('"').Append(exePath).Append("\" /portable");   // /portable is HARD-CODED here
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

# -- 4. Launch suspended, grant, verify, resume -- or terminate + fail. -------------------------------------
$rc = [GuvfxLaunchGrant]::LaunchAndGrant($full, $WorkingDirectory, $GranteeSid)
if ($rc -ne 0) { Fail ("launch/grant failed at stage " + $rc) }

Write-Host ("slot_launch: launched and granted " + $EXPECTED_GRANTEE_ACCOUNT + " query access to " + $full)
exit 0

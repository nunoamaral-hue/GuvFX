// GuvfxLaunch.cs -- P0 native tenant-scoped single-instance MT5 launch guard.
//
// The customer RemoteApp is the ONLY normal launch authority for a tenant's portable MT5 (bridge + observer are
// attach-only, enforced by MT5_GUARDED_ATTACH). MT5 /portable does NOT self-dedupe, so a browser refresh /
// reconnect / second tab re-runs the RemoteApp start-program and spawns a SECOND terminal64.exe -> the observer
// fails closed (duplicate_terminal) and onboarding stalls. RemoteApp is repointed to THIS native exe (never
// powershell.exe -- keeping the deny-by-default AppLocker boundary intact), which makes the launch idempotent.
//
// SECURITY-CONTEXT IDENTITY -- there are NO customer-controlled arguments. The tenant identity is derived from
// the running Windows token (the RemoteApp runs AS guvfx_u_<id>). It provides NO shell, accepts NO executable
// path / username / account id / command line, starts NO process other than the fixed tenant terminal64.exe
// /portable at the derived path, kills NOTHING, never logs in, never touches the DB. Customer Zero + the
// account-18 control are refused. The governed AJ#6.4 relaunch stays the only explicit close+relaunch authority.
//
// Compile (no external deps): csc /nologo /optimize /platform:x64 /out:guvfx_launch.exe GuvfxLaunch.cs
// (built on-host via Add-Type -OutputType ConsoleApplication for the throwaway certification).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Security.Principal;
using System.Threading;

public static class GuvfxLaunch
{
    const string ACCOUNTS_BASE = @"C:\GuvFX\accounts";
    static readonly int[] RESERVED = { 1, 18 };   // SACRED: Customer Zero + the account-18 control.
    const int MUTEX_WAIT_MS = 30000;

    static int Fail(string why) { Console.WriteLine("LAUNCH-VERDICT fail " + why); return 1; }

    public static int Main()
    {
        try
        {
            // ---- Identity from the security context: NO arguments are read. ----
            string who = WindowsIdentity.GetCurrent().Name;            // e.g. HOST\guvfx_u_30
            int slash = who.LastIndexOf('\\');
            string user = slash >= 0 ? who.Substring(slash + 1) : who;
            if (!user.StartsWith("guvfx_u_", StringComparison.OrdinalIgnoreCase)) return Fail("refusing_identity");
            int id;
            if (!int.TryParse(user.Substring("guvfx_u_".Length), out id) || id <= 0) return Fail("refusing_account_id");
            foreach (int r in RESERVED) if (r == id) return Fail("refusing_reserved_identity");

            string root = Path.GetFullPath(Path.Combine(ACCOUNTS_BASE, id.ToString(), "terminal"));
            string exe = Path.Combine(root, "terminal64.exe");
            // Defence in depth: the derived path must be exactly accounts\<id>\terminal (never a traversal).
            string expected = Path.GetFullPath(Path.Combine(Path.Combine(ACCOUNTS_BASE, id.ToString()), "terminal"));
            if (!string.Equals(root, expected, StringComparison.OrdinalIgnoreCase)) return Fail("refusing_terminal_root");
            if (!File.Exists(exe)) return Fail("terminal64_missing");

            // ---- Per-tenant serialisation. Local\ namespace: session-scoped (fSingleSessionPerUser=1 => one
            // session per tenant, so refreshes reconnect to it) and creatable by a NON-ADMIN (Global\ needs
            // SeCreateGlobalPrivilege the tenant lacks). NAME is per-account, so it can never gate another tenant.
            bool createdNew;
            using (var mutex = new Mutex(false, "Local\\GuvFX_MT5_launch_" + id, out createdNew))
            {
                bool held = false;
                try { held = mutex.WaitOne(MUTEX_WAIT_MS); }
                catch (AbandonedMutexException) { held = true; }
                if (!held) return Fail("launch_serialisation_timeout");

                Process target = null;
                try
                {
                    List<int> pids = TenantPids(exe);
                    if (pids.Count >= 2) return Fail("duplicate_terminal");   // never arbitrate/kill
                    if (pids.Count == 1)
                    {
                        try { target = Process.GetProcessById(pids[0]); } catch { }
                        Console.WriteLine("LAUNCH-VERDICT reuse " + id);
                    }
                    else
                    {
                        // /portable is HARD-CODED here; never taken from an argument.
                        var psi = new ProcessStartInfo(exe, "/portable") { UseShellExecute = false, WorkingDirectory = root };
                        target = Process.Start(psi);
                        Console.WriteLine("LAUNCH-VERDICT launch " + id);
                    }
                }
                finally { try { mutex.ReleaseMutex(); } catch { } }

                // ---- Hold the RemoteApp session for exactly the terminal's lifetime (unless validating). ----
                if (Environment.GetEnvironmentVariable("GUVFX_LAUNCH_NO_HOLD") != "1" && target != null)
                {
                    try { target.WaitForExit(); } catch { }
                }
            }
            return 0;
        }
        catch (Exception e) { return Fail("launch_exception:" + e.GetType().Name); }
    }

    // terminal64.exe processes whose main-module path is EXACTLY the tenant's canonical exe. A cross-user
    // terminal (CZ/support@ in another session) throws on MainModule and is silently skipped -> never counted.
    static List<int> TenantPids(string exe)
    {
        var outp = new List<int>();
        foreach (var p in Process.GetProcessesByName("terminal64"))
        {
            try
            {
                if (string.Equals(p.MainModule.FileName, exe, StringComparison.OrdinalIgnoreCase)) outp.Add(p.Id);
            }
            catch { /* different user / exited -> not our tenant terminal */ }
        }
        return outp;
    }
}

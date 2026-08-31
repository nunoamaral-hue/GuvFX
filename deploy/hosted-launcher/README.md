# GuvFX native single-instance MT5 launch guard

`GuvfxLaunch.cs` compiles to `guvfx_launch.exe` — the per-tenant RemoteApp start-program that
guarantees at most one portable MT5 per tenant (refresh / reconnect / second tab never create a
second `terminal64.exe`). It is a **native exe on purpose**: repointing the RemoteApp at it avoids
allowing `powershell.exe` for the deny-by-default tenant (preserving AppLocker isolation).

- **Identity**: derived from the running Windows token (RemoteApp runs AS `guvfx_u_<id>`). **No
  customer-controlled arguments** — no executable path, username, account id, or command line. Refuses
  any non-`guvfx_u_<id>` identity and the reserved ids (Customer Zero, account-18).
- **Behaviour**: 0 existing → launch exactly one `/portable`; 1 → reuse/wait; ≥2 → fail closed
  `duplicate_terminal` (never arbitrates/kills). Per-tenant `Local\` mutex (session-scoped,
  non-admin-creatable; `fSingleSessionPerUser=1` ⇒ refreshes reconnect to the one session).
- **AppLocker**: place the exe in a **non-tenant-writable** location and allow it with the narrowest
  rule — a **publisher rule if GuvFX signs it**, else an **exact SHA256 hash rule**. Never allow
  `powershell.exe` / `cmd.exe` / `wscript` / `cscript`.
- Build WINDOWLESS (GUI subsystem → no customer-visible console):
  `csc /nologo /optimize /platform:x64 /target:winexe /out:guvfx_launch.exe GuvfxLaunch.cs`
  (on-host via `Add-Type -OutputType WindowsApplication`). Launch verdicts go to the Windows Event Log
  (source `GuvFX-Launcher`, pre-registered by host provisioning); the process exit code (0/1) is the machine
  contract. Any recompile changes the SHA (non-deterministic PE timestamp/MVID) → re-pin the manifest + the
  AppLocker FileHashRule in lockstep, and re-assert the ACL (SYSTEM/Admins Full, Users Read+Execute).

Not yet wired into provisioning — arming (RemoteApp repoint + AppLocker allow + provisioning
integration) is a gated step after the run-as-tenant re-certification.

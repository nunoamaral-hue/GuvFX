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
  `csc /nologo /optimize /platform:x64 /target:winexe /out:guvfx_launch.exe GuvfxLaunch.cs`.
  Use **`Build-GuvfxLauncher.ps1`** (this directory) — it compiles `/target:winexe`, proves the PE-subsystem
  parser on a known positive (`explorer.exe` = GUI/2) and negative (`cmd.exe` = CUI/3) per RULE 11, **refuses to
  emit a non-GUI (console) binary** (`built_binary_not_gui_subsystem`), and prints the file SHA256 (for the
  manifest) + the AppLocker hash (for the FileHashRule) as one JSON object. It writes only under `-OutDir` and
  never touches the live launcher. Launch verdicts go to the Windows Event Log (source `GuvFX-Launcher`,
  pre-registered by host provisioning); the process exit code (0/1) is the machine contract, so discarding
  stdout under the GUI subsystem loses no signal. Any recompile changes the SHA (non-deterministic PE
  timestamp/MVID) → re-pin the manifest + the AppLocker FileHashRule in lockstep, and re-assert the ACL
  (SYSTEM/Admins Full, Users Read+Execute).

- **Windowless is a certification gate, not just a build note.** `Verify-GuvfxNativeLauncher.ps1` (the read-only
  provisioning gate) asserts `subsystem_is_gui` (PE subsystem = GUI/2, proven on the same RULE 11 controls) in
  addition to exists / SHA256 / ACL / AppLocker / runtime; `slot_preparation` requires it, so a
  **console-subsystem launcher fails provisioning closed** (`PREP_LAUNCHER_FAILED`) and can never be re-certified.

History note: the first armed build (2026-08-24) was a **console-subsystem** exe — the member saw a black
`LAUNCH-VERDICT ...` console in front of MT5. The source was already the windowless design (commit `180538f`);
it was simply never rebuilt. PR C rebuilds it GUI, re-pins the manifest + AppLocker, and adds the
`subsystem_is_gui` gate above so the defect cannot recur.

Arming (RemoteApp repoint + AppLocker allow) is gated by `HOSTED_NATIVE_LAUNCHER_GATE_ENABLED` (ON in prod).

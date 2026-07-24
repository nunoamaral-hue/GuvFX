# B3P — beta-agent service harness: WinSW vs NSSM vs native pywin32

Decision (Nuno, 2026-07-24): **use a wrapper, prefer WinSW; fall back to native pywin32 only if a wrapper
demonstrably cannot meet a requirement.** This note records why a wrapper meets every GuvFX operational
requirement *without making the production Python installation part of the beta runtime*, and confirms no
requirement forces pywin32.

## Why native pywin32 was rejected (what the APPLY proved)

Installing the agent as a **pywin32** service is what triggered the stop
(`service_install_STOP_2026-07-24.md`). Two failures, both intrinsic to pywin32-as-a-service, not to our
scripting:

1. **It writes helper DLLs to global locations.** `pywin32_postinstall` put `pywintypes311.dll` +
   `pythoncom311.dll` in `C:\Windows\System32`, and the service install put `pywintypes311.dll` in
   `C:\Program Files\Python311` — the interpreter the **live production bridge** runs on. `pythonservice.exe`
   resolves its helpers against `sys.base_prefix` and System32, so the dedicated venv isolates
   `site-packages` but **not** these DLLs. The production Python directory becomes part of the beta runtime.
2. **The virtual-account assignment is fragile.** pywin32 registers the service as LocalSystem; the
   follow-up `sc config obj=` to `NT SERVICE\GuvFXBetaAgent` did not take, leaving it over-privileged.

## The wrapper model

A service wrapper (WinSW or NSSM) is a **standalone native executable** registered as the Windows service.
The SCM talks to the wrapper; the wrapper launches the **venv** Python as a child running `agent.py`
(which already has a foreground, blocking `main()` with a graceful `stop()` on interrupt). The wrapper
writes nothing to System32 or the base interpreter. **The base Python — the bridge's — is never touched;
the venv is the beta runtime's only Python, and pywin32 is not needed by the service HOST at all** — the
wrapper replaces `service.py` (the pywin32 SCM shim), which was the only code that needed pywin32 *to be a
service*.

**Precise scope of the pywin32 claim (do not overstate it).** The agent is *not* stdlib-only. Its
slot-mutation code (`win_slot_ops`) imports `win32security`, `win32ts`, `win32api`, `win32com.client` and
`pywintypes` — but **lazily, inside the methods that materialise a slot**, never at module top level. So:

- The **service host** (WinSW) needs no pywin32. ✔
- `import agent` (the installer's runtime smoke test) succeeds **without** pywin32 loaded, because those
  imports are lazy — it validates the interpreter and bundle coherence, not that pywin32 works.
- The **agent still needs pywin32 at runtime** to materialise a slot. That pywin32 stays **inside the
  venv**; its native DLLs load from `<venv>\Lib\site-packages\pywin32_system32` via the pip-installed
  `pywin32` bootstrap `.pth`. This needs **no** global DLL — *provided venv provisioning does not run
  `pywin32_postinstall -install`*, which is the OTHER global write (it put `pywintypes311.dll` in System32
  at 05:38 in the incident). `provision_beta_venv.ps1` is corrected to skip postinstall and to prove
  `import win32security` works from the venv with no new System32 DLL.

So "writes nothing global" is delivered by **two** changes together: WinSW as the host (removes the
service-install global write) **and** a postinstall-free venv (removes the provisioning global write).

## Requirement-by-requirement

| GuvFX requirement | Wrapper (WinSW) | Native pywin32 |
|---|---|---|
| **Startup** | Real Windows service; SCM starts the wrapper, which launches the venv-python child. Start mode configurable (we use `manual`, no autostart). | Yes, but drags the global DLLs in. |
| **Shutdown** | Wrapper signals the child (Ctrl+C to the console app); `agent.py main()` catches `KeyboardInterrupt` → `server.stop()` drains in-flight mutating ops within the bounded drain window. `stoptimeout` bounds it. | Yes (SCM stop → `SvcStop`). |
| **Restart** | `sc` / `Restart-Service` / SCM recovery relaunch the child. | Yes. |
| **Logging** | Wrapper captures child stdout/stderr to its own rotating logs, separate from the agent's `agent-state\logs`. | pywin32 has no built-in stdio capture; you add it yourself. |
| **Recovery** | `onfailure` restart-with-delay in the wrapper **and** SCM failure actions; we keep recovery **disabled** for the first install (nothing may auto-restart before approval). | SCM failure actions only. |
| **Identity** | Runs as `NT SERVICE\GuvFXBetaAgent` via the wrapper's service-account config (no password) — the exact assignment that failed under pywin32. Least privilege preserved. | The `obj=` assignment is the step that failed. |
| **Service control** | It **is** a native service: `sc`, `Get-Service`, `Stop-Service`, event-log integration all work unchanged. | Same. |
| **Isolation of prod Python** | **Writes nothing global.** Base interpreter + System32 untouched. | **Fails this** — the whole reason for the stop. |

## Why WinSW over NSSM

Both are mature and both meet the table above. WinSW is preferred because:

- **Declarative, reviewable config.** WinSW's service definition is a single XML file that lives in the
  repo as a reviewed artefact (executable, arguments, account, log mode, recovery). NSSM's configuration is
  imperative registry writes via `nssm set …`, which are harder to diff and audit.
- **Deterministic, self-contained.** A pinned WinSW release is a single signed executable; the v3 line ships
  a self-contained .NET build with no framework dependency. NSSM is also a single exe, but its state lives
  in the registry rather than a versioned file.
- **Graceful stop fidelity.** WinSW's documented stop sequence (Ctrl+C to console apps, bounded by
  `stoptimeout`) maps cleanly onto `agent.py`'s `KeyboardInterrupt`→drain path.

NSSM remains an acceptable fallback if WinSW cannot be placed on the host.

## Is there any requirement a wrapper cannot satisfy? — No

The agent is a plain HTTP server with a bounded graceful shutdown. It needs no custom SCM control codes, no
session-change notifications, no Win32 API surface from inside a service handler — the only things a wrapper
cannot provide that a native pywin32 service can. **No demonstrated technical requirement forces pywin32**,
so the fallback condition is not met.

## What this changes in the artefacts (implemented next, through the pipeline)

- `install_service.ps1`: lay down a **pinned, hash-verified** `WinSW.exe` (renamed `GuvFXBetaAgent.exe`) and
  `GuvFXBetaAgent.xml` under the beta tree; register via the wrapper; then assign the identity **post-install**
  — `sc config obj= "NT SERVICE\GuvFXBetaAgent"` (validated) + an LSA `SeServiceLogonRight` grant — because
  WinSW v2.12.0 does not apply `<serviceaccount>` (host-proven 2026-07-24; see ADR 0013). Install **stopped**,
  recovery disabled; verify exact identity / `ProcessId 0` / `SeServiceLogonRight` / startmode / state. No
  `pywin32`, no global DLL writes. (`sc config obj=` here is the *supported* identity step for a WinSW-created
  service — it takes cleanly; the incident's failure was pywin32-specific.)
- The WinSW executable is a new third-party dependency. Introducing an executable to the production host is
  an operator-gated step: a specific release is pinned (WinSW v2.12.0 `WinSW.NET4.exe`, SHA-256
  `923111c7142b3dc783a3c722b19b8a21bcb78222d7a136ac33f0ca8a29f4cb66`) and verified on the host before first
  use. The installer refuses a `WinSW.exe` whose hash does not match the pinned value.
- `provision_beta_venv.ps1`: **skip `pywin32_postinstall -install`** and instead prove `import
  win32security` (+ `win32ts`, `win32api`) works from the venv via the pip bootstrap `.pth`, asserting no
  new `pywintypes311.dll`/`pythoncom311.dll` appears in System32 or the base interpreter from that run. This
  is the second half of "writes nothing global"; without it the WinSW switch removes only one of the two
  global writes.
- `service.py` (pywin32 SCM shim) is retained in the bundle but no longer on the service path; pywin32 is no
  longer required for the service HOST (it remains required, from the venv, for the agent's slot ops).
- **On-host proof obligation.** WinSW's assignment of the `NT SERVICE\GuvFXBetaAgent` *virtual* account is
  the one thing that cannot be verified off-host. The installer's `-Apply` verify **fails closed** if
  `StartName` is not that account (it will not leave a mis-identified service installed). If WinSW cannot
  assign a virtual service account on this host, that is the "demonstrated technical requirement a wrapper
  cannot satisfy" the decision named — reported, not worked around.

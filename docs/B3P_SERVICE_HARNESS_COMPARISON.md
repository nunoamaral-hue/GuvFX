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
the venv is the beta runtime's only Python, and pywin32 is not needed for the service host at all** (the
agent code is stdlib-only; only `service.py`, the pywin32 SCM shim, needed pywin32, and the wrapper
replaces it).

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
  `GuvFXBetaAgent.xml` under the beta tree; register via the wrapper; run as `NT SERVICE\GuvFXBetaAgent`;
  install **stopped**, recovery disabled; verify identity/startmode/state. No `pywin32`, no `sc config obj=`,
  no global DLL writes.
- The WinSW executable is a new third-party dependency. Introducing an executable to the production host is
  an operator-gated step: a specific release is pinned and its published SHA-256 verified on the host before
  first use. The installer refuses a WinSW.exe whose hash does not match the pinned value.
- `service.py` (pywin32 SCM shim) is retained in the bundle but no longer on the service path; pywin32 is no
  longer required for the service host.

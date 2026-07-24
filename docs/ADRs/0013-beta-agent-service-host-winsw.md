# 0013 — Beta agent service host: WinSW wrapper, not a native pywin32 service

- Date: 2026-07-24
- Status: Accepted

## Context

The B3P beta provisioning agent (`deploy/beta-agent/agent.py`) must run as a managed Windows service on
the production host `WIN-RD8VDS93DK7` (Tailscale `100.79.101.19`), which **also** runs the operator's live
MT5 (production terminal) and the live signal bridge (port 8788). The service must be install-only until a
separate first-start authorisation, run under a least-privilege identity, and — critically — leave the
production Python installation and the machine untouched.

The original design installed the agent as a **native pywin32 Windows service** via a `pythonservice.exe`
SCM shim (`deploy/beta-agent/service.py`). Its APPLY on 2026-07-24 hit a STOP condition
(`evidence/b3p2-install/service_install_STOP_2026-07-24.md`, ADR-superseded by this record).

## Verified facts

- **Global DLL writes (the incident).** `pywin32_postinstall` wrote `pywintypes311.dll` + `pythoncom311.dll`
  into `C:\Windows\System32` (05:38), and the pywin32 service install wrote `pywintypes311.dll` into
  `C:\Program Files\Python311` — the interpreter the **live production bridge** runs on.
  `pythonservice.exe` resolves its helper DLLs against `sys.base_prefix` and System32, so the dedicated
  venv isolates `site-packages` but **not** these DLLs. The production Python became part of the beta
  runtime. Evidence: `evidence/b3p2-install/service_install_STOP_2026-07-24.md`.
- **Virtual-account assignment failed.** pywin32 registered the service as `LocalSystem`; the follow-up
  `sc config obj= "NT SERVICE\GuvFXBetaAgent"` did not take, leaving it over-privileged.
- **The agent is NOT stdlib-only.** `deploy/beta-agent/win_slot_ops.py` imports `win32security`,
  `win32ts`, `win32api`, `win32con`, `win32com.client`, `pywintypes` — but **lazily, inside the methods
  that materialise a slot** (grep-verified: all imports are indented inside functions, none at module
  top level). Therefore `import agent` succeeds without pywin32 loaded; the agent still needs pywin32 at
  runtime to materialise a slot.
- **WinSW pin.** `WinSW.NET4.exe` v2.12.0, size 852480 bytes, SHA-256
  `923111c7142b3dc783a3c722b19b8a21bcb78222d7a136ac33f0ca8a29f4cb66` (pinned identically in
  `install_service.ps1`, `tests_install_artefacts.py`, and this repo's docs).
- **Implementation merged.** PR #195 (merged `bb02f74`) replaced the pywin32 install path with the WinSW
  model and passed an adversarial review (48 agents, 2/3-vote, 10 findings fixed) and `make check`
  (backend 1579 tests, frontend 0 lint errors, build OK).
- **Comparison of record:** `docs/B3P_SERVICE_HARNESS_COMPARISON.md`.

## Assumptions

- WinSW v2.12.0 assigns the `NT SERVICE\GuvFXBetaAgent` **virtual** service account from
  `<serviceaccount><username>` **plus `<allowservicelogon>true</allowservicelogon>`** at `CreateService`.
  **HOST-PROVEN 2026-07-24:** without `<allowservicelogon>` WinSW ignores `<username>` and installs
  LocalSystem (the `-Apply` verify caught this and refused to start); this **reversed the earlier review
  finding F2** (which had removed the element on a hypothesised pre-registration LSA-resolve error — that
  error does not occur, because `CreateService` auto-provisions the virtual account before the logon-right
  grant). The installer's `-Apply` verify **fails closed** if `StartName` is not that account. If WinSW
  could not assign the virtual account even with `<allowservicelogon>`, that would be the demonstrated
  technical requirement justifying the native-pywin32 fallback (see Reversal path).
- WinSW delivers `CTRL_C_EVENT` to the console child on stop, which Python raises as `KeyboardInterrupt`
  (agent.py `main()` catches it and drains). Bounded by `<stoptimeout>`.

## Decision drivers

Isolation of the production Python (paramount — it is the live bridge's interpreter); least privilege;
install-only / no auto-start / no auto-restart before approval; reviewability of the service definition;
deterministic, hash-verifiable dependency; graceful bounded drain on stop; reversibility.

## Options considered

- **A — Native pywin32 service (`pythonservice.exe`).** Rejected. Writes helper DLLs to System32 **and**
  the base interpreter (the venv does not isolate them); the `sc config obj=` virtual-account assignment is
  fragile and failed in practice, leaving LocalSystem. Directly caused the 2026-07-24 STOP.
- **B — WinSW wrapper (chosen).** A standalone, hash-pinned .NET executable registered as the service; the
  SCM talks to the wrapper, which launches the **venv** Python as a child running `agent.py`. Writes nothing
  to System32 or the base interpreter; takes its account, start mode, recovery and logging from a single
  reviewed XML.
- **C — NSSM wrapper.** Acceptable fallback if WinSW cannot be placed on the host. Rejected as primary
  because its configuration lives in imperative `nssm set` registry writes rather than a versioned,
  diffable XML.

## Decision

Host the beta agent with a **WinSW wrapper** (Option B), preferring it over NSSM, and fall back to native
pywin32 **only** if a wrapper demonstrably cannot meet a requirement (Nuno, 2026-07-24). No requirement was
found that forces pywin32: the agent is a plain HTTP server with a bounded graceful shutdown and needs no
custom SCM control codes, session-change notifications, or in-handler Win32 surface.

**Boundary:** `SCM → WinSW (GuvFXBetaAgent.exe) → venv Python (agent-venv\Scripts\python.exe) → agent.py`.
A Windows primitive knows only slot identity/dir/task (per ADR architecture invariant); the wrapper knows
only how to run one child; the SCM knows only the wrapper.

- **Remaining pywin32 use.** pywin32 stays installed **in the venv** and is imported **lazily** by
  `win_slot_ops` during slot materialisation; its native DLLs load from `<venv>\Lib\site-packages\
  pywin32_system32` via the pip bootstrap `.pth`. `provision_beta_venv.ps1` no longer runs
  `pywin32_postinstall` (the second global write), so pywin32 is present without any System32 / base
  interpreter DLL.
- **Identity & ACL model.** Service runs as the virtual account `NT SERVICE\GuvFXBetaAgent`. Its SID is
  derived from the name via `sc.exe showsid` **before** the service exists and bound with `Set-Acl` (never
  `icacls`, which cannot resolve the not-yet-existent account → 1332). Grants: **Modify** on
  state/tombstones/slots; **ReadAndExecute** on the agent code, golden image, WinSW dir and the venv. Every
  grant is post-checked; a missing ACE fails the install.
- **Startup / shutdown.** `<startmode>Manual</startmode>` (no autostart); stop sends Ctrl+C to the child,
  bounded by `<stoptimeout>300 sec</stoptimeout>`, which the installer asserts is greater than
  `BETA_AGENT_DRAIN_TIMEOUT_S` so a stop cannot force-kill a mutation mid-drain (B-6).
- **Logging.** WinSW captures the child's stdout/stderr to rolling logs under
  `C:\GuvFX\beta\agent-state\logs`, separate from the agent's own lifecycle logs.
- **Recovery.** `<onfailure action="none" />` — nothing auto-restarts before approval; the installer both
  validates the XML has exactly one `onfailure=none` and **parses** `sc qfailure` to confirm no SCM restart
  action.
- **Dependency pinning & integrity.** WinSW is pinned by version + SHA-256; the installer refuses any binary
  whose hash does not match and re-hashes the staged copy. Introducing the executable to the host is
  operator-gated.
- **Upgrade procedure.** To move to a new WinSW release: update the pin (version + SHA-256) in
  `install_service.ps1` and the conformance tests, review, then on the host `GuvFXBetaAgent.exe stop`,
  replace the staged wrapper, `GuvFXBetaAgent.exe uninstall` then `install` (config unchanged). No global
  state to migrate. To change the service definition, edit `winsw/GuvFXBetaAgent.xml`, re-review, re-stage,
  reinstall.

## Consequences

- The production Python and System32 are no longer touched by beta service installation; the beta runtime's
  only Python is the venv. `service.py` (the pywin32 SCM shim) is retained in the bundle but is off the
  service path.
- A new third-party executable (WinSW) is introduced to the host — a pinned, hash-verified, operator-placed
  dependency.
- `uninstall.ps1` is now WinSW-aware (WinSW `uninstall` + `sc delete` fallback; revokes the WinSW-dir and
  venv ACLs; removes the staged wrapper dir).

## Risks and controls

- **RED — production Python contamination.** Control: `provision_beta_venv.ps1` skips postinstall; the
  installer **measures** before/after that no pywin32 DLL was created/modified in System32 or the base
  interpreter (RULE 11), and fails closed on any change.
- **RED — over-privileged identity.** Control: the `-Apply` verify throws unless `StartName` is exactly the
  virtual account; the derived SID is validated as `S-1-5-80-…`.
- **AMBER — WinSW virtual-account assignment unproven off-host.** Control: fail-closed verify + on-host PLAN
  before APPLY; documented fallback to pywin32/NSSM.
- **RED — accidental service start / auto-restart.** Control: manual start mode, `onfailure=none`, install
  asserts Stopped, conformance tests forbid every start form (`Start-Service`/`Restart-Service`/
  `Set-Service -Status Running`/`.Start()`/`sc start`).
- Production boundary preserved: nothing in the install path touches MT5, the bridge, port 8788,
  production tasks, `guvfx_u_*` identities, autologon, or unrelated firewall rules.

## Evidence / validation

- PR #195 (`bb02f74`); adversarial review 48 agents / 2-of-3 vote / 10 findings fixed; `make check` green
  (backend 1579 tests OK, frontend 0 lint errors, build OK).
- Off-host: scripts ASCII-only (RULE 9), brace/paren-balanced, no `\"` escape hazard; WinSW XML well-formed;
  pinned SHA-256 consistent across installer/tests/docs.
- **Not covered off-host (on-host gates):** Windows PowerShell 5.1 AST parse (`[Parser]::ParseFile`,
  RULE 9); WinSW's actual virtual-account assignment; the before/after global-DLL measurement on the real
  host. These are executed during the commissioning APPLY.

## Reversal path

`uninstall.ps1 -Apply` removes the WinSW service (WinSW `uninstall` + `sc delete`), revokes the WinSW/venv
ACLs, and removes the staged wrapper dir; slot dirs, tombstones and `agent-state\` are retained. A scoped
`sc.exe delete GuvFXBetaAgent` alone also removes the SCM registration (used in the 2026-07-24 recovery).
If WinSW cannot assign the virtual account on-host, fall back to NSSM (Option C) or, only as a last resort
with the global-DLL problem re-solved, native pywin32 (Option A).

## Revisit trigger

WinSW fails to assign the `NT SERVICE` virtual account on-host; a WinSW CVE or a required upgrade; the MT5
viability trial forces a change to the launch/identity model; or a future move off the co-hosted production
box.

## Approval

Nuno, 2026-07-24 (RED: production access / service host migration) — "prefer WinSW over NSSM; fall back to
native pywin32 only if a wrapper demonstrably cannot satisfy a requirement," and the FINAL COMMISSIONING
packet authorising this ADR. PM owns lifecycle status.

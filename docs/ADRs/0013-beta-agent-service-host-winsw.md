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

- **HOST-PROVEN 2026-07-24 (no longer an assumption):** WinSW v2.12.0 (.NET4) does **not** assign the
  `NT SERVICE\GuvFXBetaAgent` **virtual** account from `<serviceaccount>` at all — with **or** without
  `<allowservicelogon>` it installs **LocalSystem** (virtual-account support is a WinSW **v3** feature). The
  installer's `-Apply` verify caught this both times and refused to start. Therefore the identity is assigned
  **post-install, not by XML alone**: `sc.exe config <name> obj= "NT SERVICE\GuvFXBetaAgent"` (result
  captured + validated) flips `StartName` to the virtual account (host-proven: `ChangeServiceConfig
  SUCCESS`), and an **LSA `SeServiceLogonRight` grant** to the derived service SID makes it start-capable
  (it is not auto-granted — secedit-verified). The `-Apply` verify then requires `StartName` to be **exactly**
  the virtual account (no LocalSystem/LocalService/NetworkService fallback), `ProcessId 0`, and
  `SeServiceLogonRight` present. This supersedes review finding F2 (which was wrong in both directions) and
  the earlier "sc config obj= failed" incident note (that failure was pywin32-specific; `sc config obj=`
  takes cleanly for a WinSW-created service).
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
  grant is post-checked; a missing ACE fails the install. **Canonical identity-assignment sequence (Nuno,
  2026-07-24):** WinSW `install` → `sc config obj= "NT SERVICE\GuvFXBetaAgent"` (validated) → LSA grant of
  `SeServiceLogonRight` to the service SID → fail-closed verify (exact `StartName`, `ProcessId 0`,
  `SeServiceLogonRight` present, Manual, Stopped, recovery none). WinSW **remains** the approved service
  host; the XML `<serviceaccount>` is retained (WinSW v3 would honour it) but is **not** relied upon for
  identity on v2.12.0.
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

---

## Addendum 2026-08-06 — Supervised lifecycle, launch enforcement & health monitoring (minimum production hardening)

- Date: 2026-08-06
- Status: Accepted (additive) — **supersedes the DARK install-only lifecycle** (`startmode Manual`,
  `onfailure=none`) for the moment the agent transitions from install-only to a **supervised production
  service**. It does **not** change the WinSW-vs-pywin32 host decision above, the least-privilege identity,
  the bind pin (`:8791`), the drain contract, or the integrity gate — all of those remain in force. The
  original record is preserved verbatim; this addendum only adds the supervision layer.

### Why (the paid-for reason)
The 2026-08-05 incident: the sanctioned `GuvFXBetaAgent` service was **Stopped** while a non-service
`python agent.py` served `:8791`, then exited un-restarted and un-logged, leaving the socket dark for hours —
discovered only by a customer's failed validation (`login_timeout`, later root-caused to a backend→agent
**connect** timeout). Manual/interactive launch is session-bound (security RULE 1), has no supervision,
restart or durable lifecycle log, and — critically — answers a signed NEGOTIATE **byte-identically** to the
supervised child, so a liveness probe alone cannot tell them apart.

### Decision
1. **Supervised auto-restart (RR-1).** The production profile
   (`deploy/beta-agent/winsw/GuvFXBetaAgent.supervised.xml`) runs `startmode Automatic` + `delayedAutoStart`
   with a **bounded-backoff restart FLOOR that never permanently gives up**: three `onfailure action=restart`
   tiers (10s → 60s → 300s; the last governs all subsequent failures) plus `resetfailure=1 hour`. Crash-loop
   **detection + paging** is the backend's job (`agent_crash_loop`, computed from probe up→down→up
   transitions) so the supervisor keeps trying (availability) while a human is brought in (RR-11) — the two
   are deliberately separated (the old "subsequent → none" stranded the agent on any persistent condition).
2. **Signed readiness probe + alert delivery (RR-2/RR-11).** A backend signed-NEGOTIATE probe
   (`terminal_provisioning/agent_health_probe.py`) on a cadence (Healthy 60s / Degraded 30s / Unavailable
   exp-backoff cap 5 min, consecutive-success recovery) feeds monitoring
   (`agent_monitoring.py`) whose alerts terminate at a **named human** via a delivery sink
   (`agent_alert_sink.py`). A computed alert that pages nobody reproduces the incident, so delivery is a
   first-class deliverable, not an afterthought.
3. **Durable lifecycle logging (RR-3).** `agent_lifecycle.py` writes a secret-safe, allow-listed
   `agent_lifecycle.jsonl` (start/listen/ready/stopping/stopped/rejected, with pid/ppid/version/supervised/
   bind) **independent of the WinSW wrapper log**, so a start/exit is never invisible again.
4. **Launch enforcement, REAL not documentary (RR-4).** (a) a single-instance guard (lock file + PID
   liveness; the OS bind is the hard backstop); (b) launch classification — the supervised service injects
   non-secret markers (`BETA_AGENT_SERVICE_IDENTITY` + `BETA_AGENT_SUPERVISED_TOKEN`); a bare
   `python agent.py` lacks them, so it advertises `agent_supervised=false` in NEGOTIATE and the probe pages
   `agent_unsupervised_listener` **and never reads HEALTHY**; (c) an optional **hard refuse-to-bind**
   (`BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH=1`), default OFF, to be enabled ONLY after the supervised service
   is proven in place (enabling it before then would brick the manual recovery path — RULE-1 care). A
   documented maintenance override (`BETA_AGENT_LAUNCH_OVERRIDE=1`) permits a sanctioned manual start while
   still reporting `supervised=false`.

### Honest scope (RULE 7)
The launch proof defends the **accidental** unsanctioned launch (the actual incident), not a privileged
adversary who could edit the bundle or read the marker off the host. Stronger proof — parent-process =
WinSW, or a per-start nonce written to an ACL'd file — is a **named fast-follow**, not claimed here. The
single-instance **hard** guarantee is the EXCLUSIVE OS bind (see the fold-in below); the lock file is
**advisory** — durable identity + clean detection — and never vetoes a start.

### Adversarial-review fold-in (2026-08-06)
A 6-lens review corrected three real defects before this addendum was accepted:
- **Exclusive bind.** `BoundedThreadingHTTPServer` inherited `allow_reuse_address=True` → `SO_REUSEADDR`,
  which on **Windows** lets a second process bind the same `:8791` and hijack it. Fixed to
  `allow_reuse_address=False` + `SO_EXCLUSIVEADDRUSE`, so a second bind FAILS at the OS — the bind is now the
  genuine hard single-instance guard.
- **Advisory lock.** The lock guard previously *raised* on a live-holder conflict **before** the bind, so a
  crash + PID reuse could turn one crash into a persistent dark-`:8791` outage on a free port. The conflict
  is now logged (`AGENT_DEGRADED`) and startup proceeds; the exclusive bind arbitrates.
- **Crash visibility + restart.** An abnormal serve-thread death used to let `main()` exit **0** (no WinSW
  restart, no lifecycle event). It now emits `AGENT_CRASHED` and exits **non-zero** so `onfailure=restart`
  fires. Crash-loop *paging* (`agent_crash_loop`) is produced by `evaluate_alerts` from the readiness
  tracker's up→down→up count — making the paging claim above real, not aspirational.

### Rollback
Revert to the DARK install-only `GuvFXBetaAgent.xml` (Manual / `onfailure=none`) and stop the service — no
data or schema change is involved. Backend probe/monitoring are inert unless scheduled; the frontend panel is
unrouted. Full reversal procedure: `docs/operations/validation-agent/deployment-min-hardening.md`.

### Approval
Repository engineering + docs + tests: **APPROVED** (Sponsor, this packet). Applying the supervised WinSW
profile, starting/restarting the service, and any Windows-host/backend deployment remain **separately
Sponsor-gated** and are **not** performed by this packet. PM owns lifecycle status.

### Addendum 2026-08-06b — single sanctioned installer for BOTH profiles

The 2026-08-06 supervised deploy attempt proved a bare `winsw install` regresses the identity to **LocalSystem**
on WinSW v2.12 (virtual-account support is a WinSW v3 feature), and that the then-installer was hard-gated to
the DARK profile. **Resolution (repository):** `install_service.ps1` now takes an explicit, mandatory
`-Profile Dark|Supervised` and is the **single sanctioned install/reconfigure mechanism** — WinSW / `sc config`
/ `secedit` are never called by hand. For BOTH profiles it performs the post-install `sc config obj=` virtual-
account assignment + LSA `SeServiceLogonRight` grant, then **fails closed + auto-rolls-back** unless
`SERVICE_START_NAME == NT SERVICE\GuvFXBetaAgent`. Profile-specific verification: DARK ⇒ Manual + recovery
none; SUPERVISED ⇒ Automatic(delayed) + bounded restart tiers + launch-proof env markers. Both install
**STOPPED** (install-only; first start stays gated). Contract:
`docs/operations/validation-agent/installer-contract.json`; tests: `tests_supervised_installer.py`.

### Addendum 2026-08-06c — monitoring runner, scheduler, and external alert delivery

The "crash-loop paging is real, not aspirational" claim above depended on a runner + scheduler + delivery
channel that did **not** exist: the merged `agent_health_probe` / `agent_monitoring` / `agent_alert_sink`
were **inert** (no runner, no cron, sinks Null/Logging only). Two deployment attempts STOPPED on exactly
this. **Resolution (repository):** the operations layer that actually runs them.

- **Runner** — `agent_monitor_runner.run_once` executes one pass: signed-NEGOTIATE probe → durable hysteresis
  → alert policy (per-alert cooldown, one-shot recovery, flap decay) → delivery. Its ONLY side effects are
  the probe, a write to the singleton `AgentMonitorState` row, and an alert message. It performs no broker
  validation, no credential read, no attempt creation, no MT5 start, and touches no customer account or
  `:8788`.
- **Durable state** — `AgentMonitorState` (migration `0011`, singleton pk=1) so hysteresis + cooldown survive
  a backend restart. Operational metadata only.
- **Command + scheduler** — `manage.py run_agent_readiness_probe` (single-flight `select_for_update(nowait)`
  lock; deterministic exit codes 0/10/20/30/40/50; `--dry-run`, `--synthetic-state`), scheduled by
  `deploy/validation-agent-monitor/` cron. `manage.py test_agent_alert_delivery` is the pre-arm delivery gate
  the Aug-5 outage lacked; `manage.py agent_monitor_status` is the read-only, secret-free ops evidence.
- **External delivery** — `TelegramAlertSink` (a DEDICATED ops chat, its OWN bot token; the factory refuses
  to build if the ops chat_id equals the customer channel or the token is missing) + `EmailAlertSink`
  fallback. DARK by default: `VALIDATION_AGENT_MONITORING_ENABLED=false`, `AGENT_ALERT_SINK=null`, no
  destination configured.

Rollback is flag-OFF (`VALIDATION_AGENT_MONITORING_ENABLED=false` / `AGENT_ALERT_SINK=null`) plus
`install_agent_monitor_cron.sh --remove`; migration `0011` reverses cleanly; no destructive step. Contract:
`docs/operations/validation-agent/monitoring-runner-contract.json`; deployment package:
`docs/operations/validation-agent/monitoring-runner-deployment.md`; tests: `tests_agent_monitor_runner.py`,
`tests_agent_alert_sink_delivery.py`. **APPROVED for repository engineering (Sponsor, this packet).**
Deployment, flag-arming, and selecting a live destination remain **separately Sponsor-gated**.

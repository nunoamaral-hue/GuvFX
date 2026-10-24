# Stream 7B — Live Disposable Host Certification: STATUS (BLOCKED at host-mutation gate)

**Date:** 2026-08-11 · **Host:** `WIN-RD8VDS93DK7` (100.79.101.19, Windows Server 2025) · **Posture:** DARK ·
**Customer Zero:** untouched (read-only recon only).

## Outcome: BLOCKED — the signed-executor **host daemon does not exist**

Authorization (Sponsor-granted) and host access (Administrator SSH over Tailscale) are both confirmed. Phase 0
(repo baseline green) and Phases 1/3 (read-only host + Customer-Zero BEFORE fingerprint) were completed. **No host
was mutated.**

Certification then stopped at Phase 4 on a verified, material finding: **the runnable host-side signed executor
was never built.** Stream 5 delivered, all DARK and unit-tested with fakes:
- the wire contract — `hosted_workspace/host_protocol.py`;
- the dispatch *logic* — `hosted_workspace/host_agent_dispatch.dispatch()`, which takes `run_primitive`,
  `nonce_burn` and `envelope_open` as **injected** callables (only tests supply them);
- the Django *client* — `hosted_workspace/host_executor.SignedHostExecutor` + `_http_transport`, which POSTs to
  `base_url + "/hosted/provision"`.

But the **host daemon** that binds these is absent (verified by grep on `main` @ 6a1193b):
- nothing serves `POST /hosted/provision` on the host;
- there is **no production `run_primitive`** that ParseFile-validates (RULE 9) and executes the reviewed `.ps1`
  primitives;
- there is **no durable nonce store**, no host-side envelope-open (private-key) wiring;
- there is **no service installer / WinSW config / deploy bundle** for the executor (`deploy/hosted-executor*`
  does not exist);
- nothing calls `host_agent_dispatch.dispatch()` outside tests.

Consequently there is no signed-executor process to run `prepare_hosted_slot()` *through*. Manually creating the
Windows identity/ACL/RemoteApp/tasks and then declaring provisioning "certified" is explicitly out of scope
(Phase 6: *"Do NOT manually complete the target and then claim provisioning passed"*), and would not certify the
engine. Building a brand-new authenticated **code-execution daemon** and deploying it to the box that runs the
**live trading Customer-Zero terminal**, in a single unbroken pass, is the single highest-risk artefact in the
architecture and must be designed + adversarially reviewed (0 surviving HIGH) at the daemon level **before** it
ever runs there — i.e. it is its own bounded stream, not a sub-step of this cert.

## Customer-Zero BEFORE fingerprint (read-only; durable baseline)

Captured to `cz-host-BEFORE.json`. Salient facts (safe hashes only, no secrets):
- **AppLocker: ENFORCED** — Exe(16)/Msi(2)/Script(4) all `Enabled`; effective-policy SHA-256 prefix
  `67FCBF00…`. (Confirms the Stream-6 necessity: any policy-replacing op would wipe live CZ enforcement.)
- **RemoteApp:** exactly one alias `terminal64` → `C:\GuvFX\accounts\1\terminal\terminal64.exe /portable`.
- **CZ identity:** `guvfx_u_1`, SID `S-1-5-21-2216203845-1747098376-1637942580-1003`, enabled.
- **Live workloads:** `terminal64.exe` PID 3972 at `C:\Program Files\IS6 Technologies MT5 Terminal\…` (session 1,
  CPU 21,345 s = the live trading terminal); PID 7812 at `C:\GuvFX\accounts\1\terminal\…` (session 3, hosted CZ).
- **Services:** `GuvFXBetaAgent` Running (Auto); listeners on :8791 (beta agent) and :8788 (trade bridge). **No
  hosted-executor service.** `fSingleSessionPerUser=1`; AppIDSvc Running/Auto; RDS role Installed.

## Markers
**None emitted.** `SIGNED_HOST_EXECUTOR_HOST_CERTIFIED`, `G5_WORKSPACE_ACL_HOST_CERTIFIED`,
`HOSTED_SLOT_PROVISIONING_CERTIFIED`, `HOSTED_SLOT_PROVISIONING_IDEMPOTENCY_CERTIFIED` — all **WITHHELD** (no host
provisioning occurred). `AUTONOMOUS_ONBOARDING_CERTIFIED` — not applicable.

## Recommended next packet — Stream 7C: build + review the host daemon (repo, DARK), THEN live cert
1. Build the host-resident signed-executor daemon (authenticated HTTP service serving `/hosted/provision` →
   `verify → dispatch()`; a production `run_primitive` that ParseFile-gates and executes ONLY the mapped
   reviewed `.ps1` with server-derived args; durable single-use nonce store; envelope-open with the host private
   key; a supported-service installer). Tests + adversarial review at the daemon level (0 surviving HIGH), DARK.
2. Then re-run this Stream 7B live cert: provision keys by reference, deploy the reviewed daemon, arm the flag for
   the disposable slot only, run the real `prepare_hosted_slot`, RULE-11 controls, idempotency, CZ STOP-check.

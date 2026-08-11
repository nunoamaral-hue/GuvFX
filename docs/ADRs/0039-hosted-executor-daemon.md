# ADR-0039 — Hosted signed-executor daemon (the runnable host end)

- **Status:** Proposed (Amber — repository + packaging only; effective on a host solely at the Sponsor-gated
  deployment + certification packet). **DARK, not deployed.**
- **Date:** 2026-08-11
- **Programme:** Beta Readiness Stream 7C (build the host daemon)
- **Builds on:** ADR-0036 (Host Provisioning Engine), ADR-0037 (Signed Host Executor — the wire contract +
  dispatch logic + Django client), ADR-0038 (Multi-tenant isolation). Closes the gap Stream 7B surfaced: the
  *runnable* host end of the signed transport had never been built.

## Context

Stream 5 (ADR-0037) shipped the signed provisioning transport as three DARK, unit-tested pieces: the wire
contract (`host_protocol`), the dispatch **logic** (`host_agent_dispatch.dispatch`, with `run_primitive` /
`nonce_burn` / `envelope_open` as **injected** callables used only by tests), and the Django **client**
(`host_executor.SignedHostExecutor` → posts to `/hosted/provision`). The Stream 7B live-host certification then
established, with evidence, that **nothing served `/hosted/provision`, no production `run_primitive` executed the
reviewed `.ps1`, no durable nonce store / envelope-open wiring existed, and no service installer existed** — so
`prepare_hosted_slot` could not run *through* the signed executor because there was no executor process to run it
through. All four `*_HOST_CERTIFIED` markers were withheld.

This ADR records the decision to build that runnable host daemon as a complete, reviewable repository
deliverable, deferring deployment and live certification to a separate Sponsor-gated packet.

## Decision

Add a Django-free host bundle `deploy/hosted-executor/` that binds the Stream 5 pieces into a supported Windows
service, mirroring the proven, deployed beta agent (`deploy/beta-agent/`). It is the ONLY component that turns a
signed request into a host action, and it is deliberately incapable of arbitrary execution.

### Components

- **`daemon.py`** — an authenticated HTTP listener (stdlib `ThreadingHTTPServer`) serving exactly
  `POST /hosted/provision` (+ a non-secret `GET /hosted/health`). It hands each request to `dispatch`, returns
  the signed response, and provides the lifecycle: exclusive single-instance bind (Windows
  `SO_EXCLUSIVEADDRUSE`), bounded concurrent connections, bounded request body, disabled keep-alive, a **drain**
  on stop (in-flight provisioning ops finish before the socket closes), and a crash → non-zero exit so the
  supervisor restarts.
- **`daemon_config.py`** — RULE-3 configuration. The executor's **own** HMAC keyring
  (`HOSTED_EXECUTOR_KEYRING` / `_KEY_ID`) and its **distinct** envelope private keyring
  (`HOSTED_EXECUTOR_ENC_PRIVKEYS`) are loaded from the machine environment; a missing/placeholder/substituted
  value is a startup failure, never a fall-back to another service's secret. The bind is pinned to the exact
  expected private/Tailscale address; the ports 8791 (beta agent), 8788 (bridge), 8787, 3389 are refused.
- **`nonce_store.py`** — a durable single-use SQLite nonce store (`burn` atomic first-use, prune by expiry),
  mirroring the beta agent, so a replay is refused across a restart. `nonce_burn` is invoked by
  `verify_hosted_request` only after the HMAC verifies.
- **`primitive_runner.py`** — the injected `run_primitive`. A primitive name resolves to exactly one reviewed
  `.ps1` (allow-list); every mapped `.ps1` is ParseFile-gated at startup (RULE 9, with a positive marker so a
  bare exit-0 is not mistaken for a clean parse — RULE 11); execution is a **fixed argument vector**
  (`powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <fixed .ps1> <named args>`) passed to
  `subprocess` as a list (never a shell string, never `-Command`/`-EncodedCommand`). The snake_case dispatch
  args are mapped to the scripts' real `-ParamName` (including the AppLocker `username → -HostedUser`), mandatory
  params the dispatch omits are injected (`-AccountId`, `-Mode`), unmapped keys are dropped, and the Windows
  password is written to the child's **stdin** (first line) — never argv, env, or a log. The verdict is the
  script's `ok` **and** a zero exit; a timeout / non-zero / unparsable / oversize output fails closed.
- **`envelope_open.py`** — opens the sealed Windows password (ADR-0027) with the host private key, rebuilding
  the AAD byte-identically to the seal side (`operation="PROVISION_IDENTITY"`,
  `runtime_uuid=f"account:{account_id}"`, `correlation_id`, `nonce`) so a relayed/substituted envelope fails
  AEAD auth closed.
- **`lib/broker_cred_envelope.py`** — the Django-free envelope crypto, vendored byte-identical to the beta-agent
  copy (drift-guarded by a test).
- **`winsw/*.xml` + `install_service.ps1`** — the single sanctioned installer (WinSW hash-pin, profile-aware XML
  contract, ASCII-only staged XML, ParseFile-gate of the staged primitives, `sc config obj=` NT-SERVICE identity
  + `SeServiceLogonRight`, install-only verify, rollback on failure), mirroring the beta agent.

### Where the code lives, and how CI covers it

The daemon's `host_protocol` + `host_agent_dispatch` remain the **single source of truth in
`backend/hosted_workspace/`** (Django-tested); the installer stages them into `lib/hosted_workspace/` on the
host, and under CI the tests import the real backend modules. Nothing is duplicated in the repo except the
vendored, drift-guarded envelope crypto. The daemon's own modules are exercised by
`backend/hosted_workspace/tests_host_*.py`, which insert the bundle on `sys.path` (the proven beta-agent test
pattern) so `make check` runs them — 72 tests covering the arg-vector mapping, password→stdin, ParseFile gate,
nonce durability, envelope round-trip + fail-closed, config RULE-3, the HTTP listener, and the drain.

## Consequences

- The Stream 7B live disposable-host certification is now **actionable** — but is a separate, Sponsor-gated
  packet (`docs/operations/hosted-workspace/HOSTED_EXECUTOR_DEPLOY_RUNBOOK.md`): stage the bundle + primitives,
  provision the `HOSTED_EXECUTOR_*` machine secrets, install the reviewed service, arm the flag for one
  disposable non-CZ slot, run the real `prepare_hosted_slot`, RULE-11 controls, idempotency, and the Customer
  Zero before/after STOP-check. This packet deploys nothing.
- No `*_HOST_CERTIFIED` marker is emitted here (no host provisioning has occurred). Execution stays DARK
  (`HOSTED_HOST_EXECUTOR_ENABLED` unset).
- **Customer Zero is untouched.** The daemon refuses account #1 (hard floor) in two layers, never binds a
  non-expected interface, and is not installed on any host by this packet.

## Security review

The definitive architecture + trust-boundary + threat-model security review (request lifecycle, trust
boundaries, security properties with mechanism+evidence, failure analysis, threat model, code traceability, and
a reviewer checklist) is `docs/operations/hosted-workspace/HOSTED_EXECUTOR_SECURITY_REVIEW.md`. It
cross-references ADR-0036 (host provisioning engine), ADR-0037 (signed executor contract), and ADR-0038
(multi-tenant isolation).

## Adversarial review (0 surviving HIGH/MEDIUM)

A five-lens adversarial-verify review (injection, auth/crypto, confinement, network/DoS, fail-closed) surfaced
four confirmed defects, all fixed + regression-tested:

- **MEDIUM (slowloris):** the per-recv socket timeout did not bound total request-read time, so a trickle
  client could hold a connection permit indefinitely and exhaust the (deliberately small) connection cap
  pre-auth. Fixed with a wall-clock watchdog that force-closes the socket after `request_timeout_s` and is
  disarmed the instant the body is read — bounding only the untrusted read phase, never the long provisioning
  dispatch.
- **LOW ×3:** `HOSTED_EXECUTOR_MAX_SKEW_SECONDS` was dead config (now threaded through `dispatch` →
  `verify_hosted_request`); an empty/whitespace `HOSTED_EXECUTOR_RESERVED_ACCOUNT_IDS` could disable the
  Customer-Zero floor (the daemon now unions `{1}` unconditionally); and the envelope private keyring was not
  placeholder/type-gated at boot like the HMAC keyring (now symmetric). The review also earned a `_send`
  hardening (swallow a broken pipe across the whole response emission, not just the body write).

## Residuals / deferred (stated, not hidden — RULE 7)

- **`VERIFY_SLOT` is unimplemented on this host build.** No reviewed read-only slot-verify `.ps1` exists yet;
  `run_primitive("verify_slot", …)` fails closed with `verify_slot_unimplemented`. It is **not** on the
  `prepare_hosted_slot` critical path (which uses `ENSURE_REMOTEAPP`, not `VERIFY_SLOT`). A dedicated
  `Verify-GuvfxSlot.ps1` is future work.
- **Client read timeout vs. a long `MATERIALISE_RUNTIME`.** The backend client posts with a 30s timeout;
  copying the ~378 MB golden runtime can exceed it, surfacing as `host_unavailable` while the host keeps working
  (the known CZ MATERIALISE-timeout gotcha). The daemon's per-primitive timeout is generous (600s) and it drains
  correctly, but the **client** side needs poll-not-repost before the live cert runs a real materialise — a
  deployment-packet concern, tracked in the deploy runbook.
- The installer's file staging (repo → host dirs) is a documented operator step (the deploy packet /
  `stage-manifest.json`), mirroring the beta agent's division of labour (deploy package copies files;
  `install_service.ps1` registers the service).

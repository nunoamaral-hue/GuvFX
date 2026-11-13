# ADR-0104 — Independent host-emitter one-shot attestation trigger (signed op, no PowerShell desktop)

- **Status:** Accepted (repository-only / DARK, default-OFF). Merge, deployment, task installation, verifier-window
  opening, the real secret provisioning and the real one-shot attestation are **separate Sponsor/human gates**. No
  deploy, no migration, no host contact, no task install/trigger, no flag enablement, no secret handled, no
  verifier window opened, no authorization and no broker order is part of this packet.
- **Packet:** GFX-COPIER-INDEPENDENT-EMITTER-IMPLEMENT-093 (design confirmed in packet 092).
- **Date:** 2026-11-13.
- **ADR number:** 0104 — the next genuinely free number on `main` (0101 bounded verifier window and 0103 dashboard
  auto-monitoring enrolment are merged; 0102 is an unused gap).
- **Builds on / realises:** ADR-0101 (the bounded verifier window that holds `COPIER_INGEST_HMAC_KEY` for one
  verification session) and ADR-0077 (the host emitter + its attestation envelope). Packet 090 verified the
  ADR-0101 **backend** verifier mechanism (window → claim → crypto-verify → one-use/shred → fail-closed) using a
  backend-side signer, because the production account-#46 web workspace is **MT5-only**: it has no interactive
  PowerShell desktop on which to run the existing human-operated wrapper
  (`operator/Invoke-GuvfxCopierOneShotAttestation.ps1`). The genuine **Windows emitter** attestation path was
  therefore never exercised. This ADR adds the mechanism to run that independent emitter attestation **without**
  an interactive PowerShell desktop.

## Context — why a new path is needed

The human-operated one-shot wrapper enters the secrets at a non-echoing PowerShell prompt and runs the emitter
`attest` mode locally over RDP. Account #46's web workspace exposes only MT5, with no PowerShell console, so that
wrapper cannot be used there. The only supported backend→host control path that is **not** an arbitrary shell is
the signed host-executor seam (`host_protocol` / `host_agent_dispatch` / `host_executor` / `primitive_runner` +
fixed reviewed `.ps1`), and no existing signed op triggers the emitter attestation task. A new, narrowly-scoped
signed op is required.

## Decision — a signed, account-generic op that triggers one pre-installed, DISABLED, re-disabled attest task

Add `TRIGGER_EMITTER_ONESHOT_ATTEST` across the four signed-executor layers, mirroring the read-only
`OBSERVE_WORKSPACE` op (no confinement args; the host derives identity/paths from `account_id`). It triggers one
pre-installed, **DISABLED** attestation task for exactly one invocation and always re-disables it.

1. **Signed op, no caller surface.** The op carries only `operation` + `account_id` + an **empty** params dict and
   **no** payload. There is nowhere on the wire for a command, script, path, username, task name, URL, account or
   secret. `host_agent_dispatch` derives the slot from `account_id`, refuses Customer Zero (and any reserved id),
   and maps the op to exactly one reviewed primitive (`Trigger-GuvfxCopierEmitterOneShot.ps1`) with server-derived
   `-Username`/`-RuntimeRoot`/`-TerminalRoot` + injected `-AccountId`.
2. **Exact-account-46 pin at the policy edges.** The signed op + host dispatcher stay account-generic (like every
   sibling op) and refuse Customer Zero; the **policy** pin to the sole account the emitter bootstrap exists for
   (46) lives at three defence-in-depth edges: the backend command, the Django executor method, and the `.ps1`
   itself. The wire stays generic; the capability is pinned.
3. **One invocation, always re-disabled (fail-closed).** The host primitive proves the pre-installed one-shot task
   exists, is **currently disabled**, and that its action points only at the approved non-interactive attest
   entrypoint and the **attest-verify** route (never the event-ingest route); deletes any stale result; **enables**
   the task; triggers it; waits bounded; and **always re-disables it in a `finally`** — with a locale-independent
   read-back (RULE 11). No `exit` occurs in the enable region (a PowerShell `exit` would skip the `finally`), so
   the re-disable is structurally guaranteed, and if it cannot be verified the whole op is reported **not ok** (the
   task is never silently left enabled). There is no auto-retry.
4. **Host-local secret, SECRET_UNAVAILABLE fail-closed.** The task's action (`run_oneshot_attest.py`) reads the
   ingest secret **only** from a host-local **ephemeral key file**, injects it into a process-local env for a single
   attest, POSTs the signed envelope **once** (no retry) to the backend ATTEST verifier, writes a **sanitised**
   result (ok + http_status/verified/reason — never the envelope, signature, broker login or secret), and
   **shreds** the key file after one use. If that host-local secret file is absent/short the result is
   **SECRET_UNAVAILABLE** and nothing is attested or posted. This packet **does not** invent or provision a
   production secret store; the default (no key file) is SECRET_UNAVAILABLE.
5. **Secret-blind to Claude and to the backend.** The real `COPIER_INGEST_HMAC_KEY` never crosses the signed wire,
   never reaches the backend, and never reaches this session. The host result the backend sees carries no secret;
   the backend command re-sanitises it defensively.

### Why this cannot enable trading

The op only enables → triggers → re-disables one fixed attestation task. The attestation is identity-only (ADR-0077:
no positions); verifying it creates no `MasterEvent`, `ExecutionJob`, `CopierLiveArm`, `CopierLiveAuthorization`,
`CopierReplication`, lifecycle row or broker contact (asserted by tests, and the backend verify surface is
separately gated and fails closed at 503). The op contacts **no** follower account (#48), arms **no**
observation/replication/execution/event-ingestion, and the scheduled emitter and the one-shot task both stay
DISABLED outside the single invocation. The DARK flag `copier_emitter_oneshot_enabled` defaults OFF and is ANDed
with the master `copier_enabled` gate; the signed host executor is independently gated (host-executor flag +
keyring/base_url), so with the flag on but the executor unconfigured the command still contacts no host.

## Trust boundary and exposure analysis

| Surface | Exposure | Mitigation |
|---|---|---|
| Signed wire (op/params/payload) | — | Only `account_id`; empty params; no payload. No command/script/path/url/account/secret can ride it; `params_not_allowed` on any smuggled key. |
| Customer Zero / wrong account | denied | Reserved-id refusal (dispatcher + executor `_send`); account-46 pin at command + executor + `.ps1`. |
| Task left enabled | denied | Enable only for one invocation; re-disable in a `finally` with locale-independent read-back; no `exit` in the enable region; unverifiable re-disable ⇒ not ok. |
| Task/action drift | denied | Primitive verifies the task is pre-installed, currently disabled, and its action references `run_oneshot_attest.py` + the result file + the `attest-verify` route and **not** `event-ingest`, before enabling. |
| Ingest secret | host-local only | Read only from an ephemeral host-local key file; absent ⇒ SECRET_UNAVAILABLE; injected to a process-local env only; shredded after one use; never on the wire, in the backend, in logs, in stdout, or to Claude. |
| Backend / Claude visibility of the secret | — | The signed op carries no secret; the host result + the command output are sanitised (no envelope/signature/secret). |
| Retry / replay | bounded | Exactly one host round-trip and one POST; no auto-retry. Envelope replay stays rejected by the ADR-0101 window's single-use nonce/`max_uses`. |
| Event-ingestion / execution reachability | denied | Attestation-only; posts to `attest-verify` (never event-ingest); arms no observation/replication/execution/ingestion; no follower contact; no order. |

## Rejected alternatives

- **Use the human-operated wrapper on #46** — impossible: the #46 web workspace is MT5-only, with no PowerShell
  desktop (the whole reason for this ADR).
- **Put the ingest secret in the task/env/registry** — rejected: it would persist a standing secret at rest
  (exactly what ADR-0101 forbids for the backend). The secret is a host-local ephemeral file, shredded after one
  use; absent ⇒ SECRET_UNAVAILABLE.
- **Make the op account-specific on the wire (hard-pin 46 in `host_protocol`)** — rejected: every sibling op is
  account-generic and Customer-Zero-refused; the 46 pin belongs at the policy edges (command/executor/`.ps1`), not
  on the wire. Keeps the signed contract uniform and reviewable.
- **Add an `attest-post` mode to the shared emitter `app.py`** — rejected for a smaller blast radius: the POST +
  the host-local-secret/shred lifecycle live in a new deploy entrypoint (`run_oneshot_attest.py`); `app.py`'s
  `attest` mode is unchanged (build + self-verify only).

## Consequences

- No migration, no model change. The only backend-code additions are one DARK flag, one executor method, one op
  name in the allow-list + dispatch map + runner contract + stage manifest, and one operator command.
- New repository (DARK, never installed by this packet) host assets: `Trigger-GuvfxCopierEmitterOneShot.ps1` (the
  host primitive), `deploy/copier-emitter/oneshot_attest_task_template.xml` (the DISABLED one-shot task), and
  `deploy/copier-emitter/run_oneshot_attest.py` (the non-interactive attest+POST entrypoint, added to the bundle
  manifest + the lib subset it already vendors).
- With the flag OFF and the executor unconfigured (the production default), the command refuses before any host
  contact and everything is byte-identical to pre-ADR-0104.

## Governed one-shot task installer (packet GFX-COPIER-ONESHOT-TASK-INSTALLER-097, repository-only)

The primitive above triggers a **pre-installed** DISABLED task. `deploy/copier-emitter/Install-GuvfxCopierEmitterOneShotTask.ps1`
is the governed installer a future, separately Sponsor-gated host step runs to register that task
(`GuvFX_CopierEmitterOneShot_46`). It realises this ADR's "pre-installed DISABLED one-shot task" and is itself
repository-only / DARK (it installs nothing unless `-Apply`, and even then only registers a DISABLED, triggerless
task). Contract:

- **Pinned to account 46.** Refuses account 48 explicitly, Customer Zero and every reserved identity; accepts no
  arbitrary task name, executable, script path, endpoint, account, operation or free-form command (the only
  caller-influenced executable is the Python interpreter, refused unless it is `python`/`py`/`pythonw[.exe]`). Every
  identity and path is derived from account 46 plus the reviewed staged package.
- **Integrity before the scheduler.** Validates the required runtime files (`run_oneshot_attest.py`,
  `oneshot_attest_task_template.xml`) against `MANIFEST.json` SHA-256, ParseFile-gates the bundle `.ps1` (RULE 9),
  and validates the account-46 `config.json`, before any registration.
- **Reviewed render + authoritative read-back.** Substitutes only the fixed template placeholders (XML-escaped),
  structurally validates the rendered XML (disabled, zero triggers, the fixed action, `guvfx_u_46` principal, no
  secret), registers DISABLED, then reads the registered task back and fails closed unless every one of those
  invariants holds. Verification is from XML booleans (`<Enabled>false</Enabled>`), never locale-dependent status
  text (RULE 11).
- **Idempotent + rollback.** A byte-identical compliant task is a no-change; drift requires an explicit `-Replace`,
  which first exports a rollback of the previous definition; a failed replacement restores that export, left
  disabled. A `try/finally` with no `exit` in the register/read-back region guarantees the cleanup/rollback can
  never be skipped.
- **Never** enables or runs the task, provisions or reads the real HMAC key, opens a verifier window, contacts the
  backend or broker, or alters the run-mode emitter task. Output is a redacted structured result with no secret.

## Not in this packet (separate Sponsor/human gates)

Merge · deploy · install/enable/trigger the one-shot task on the host · open the ADR-0101 verifier window ·
provision the host-local ingest secret · enable `copier_emitter_oneshot_enabled` / the verify surface · the real
#46 emitter attestation · any authorization or broker order. No deploy, no migration, no host contact, no secret
handled, no flag enablement, no verifier window, no #48 contact, no order here.

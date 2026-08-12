# ADR-0041 — Hosted Workspace Observation Trust Model

- **Status:** Accepted (Sponsor decision, 2026-08-12). Notion is authoritative for the approved ADR lifecycle;
  the PM owns advancing/closing that status. This git-side record is the concise implementation evidence.
- **Supersedes/Extends:** ADR-0034 (Hosted Workspace state core / observation chain), ADR-0040 (Signed Host
  Executor / LocalSystem daemon), STREAM 9E (live host observation bridge).
- **Scope:** Trust semantics of the Hosted Workspace *observation* signal. Does **not** change execution
  authority (Execution Engine, ADR-0034 Decision D) — see "Execution is independent" below.

## Context

STREAM 9E built the live host observation bridge: for an eligible Hosted Workspace, the backend asks the host
(over the signed `OBSERVE_WORKSPACE` op) to run that account's **session-bound observer** — which runs **as the
tenant** (`guvfx_u_<id>`) because that is the only context that can reach the session-bound MT5 IPC — and returns
a snapshot that the certified producer → manager → single-writer chain turns into canonical lifecycle state.

Two adversarial reviews established a hard boundary:

1. The observer's broker facts (`observed_login`/`observed_server`, `attach_succeeded`/`ipc_available`,
   `trade_allowed`) come from a **tenant-written handoff** (`C:\GuvFX\accounts\<id>\_obs\observation.json`,
   which the tenant necessarily has Modify on, since the observer writes it *as the tenant*).
2. We added an **independent LocalSystem corroboration** (process / owner / session / runtime + a live external
   TCP connection + a LocalSystem timestamp, gathered by the LocalSystem daemon and returned inside the
   daemon-signed response). This closes the "no real process / totally-offline terminal" forgeries and anchors
   freshness on a clock the tenant does not control.
3. **But LocalSystem physically cannot corroborate the MT5 IPC facts** (the exact broker login, the IPC attach,
   `trade_allowed`) — those exist only inside the tenant session, which is the entire reason the observer runs
   there. A tenant who can **execute arbitrary code in its own session** can therefore forge those fields to
   positives matching its *own* account and drive its *own* workspace to `EXECUTION_READY`.

The forgery requires writing the handoff file, which in a RemoteApp-only session running MT5 requires escaping
the sandbox to run arbitrary code (MQL5 file I/O is confined to `MQL5\Files`). **That escape is exactly what the
RemoteApp / AppLocker isolation certification governs.** The residual is therefore *equivalent* to whether the
tenant can execute code in-session.

## Decision

**A Hosted Workspace observation is a BOUNDED workspace-readiness signal that is trusted ONLY after RemoteApp
isolation has been behaviourally certified.** It is **not** an execution-authority signal.

The certification dependency is made explicit and enforced:

```
REMOTEAPP_ISOLATION_CERTIFIED
        ↓
HOSTED_OBSERVATION_CERTIFIED
        ↓
WORKSPACE_READY
        ↓
AUTONOMOUS_ONBOARDING_CERTIFIED
```

We do **not** redesign the observation architecture, add a second state machine, or add further corroboration
layers that still cannot independently prove MT5 IPC state. We accept the architectural conclusion:
**observation generated inside the tenant session is trustworthy only if the tenant cannot execute arbitrary
code inside that session.**

## Enforcement (code)

- New trust anchor flag `HOSTED_REMOTEAPP_ISOLATION_CERTIFIED` (`hosted_workspace.flags`), **default OFF**,
  settings-first-then-env, read live. NO-FAKE-READY: set only after the behavioural escape-attempt
  certification actually passes on the target host.
- `live_observe.live_observe_fn` returns `None` (produces no observation, nothing is ingested, no advancement)
  unless `hosted_remoteapp_isolation_certified()` holds — the **first** gate, before the observation-enabled
  flag, the state check, or any host contact.
- `run_hosted_observations.resolve_observe_fn` selects the live observe transport only when **both** the trust
  anchor and `HOSTED_MT5_OBSERVATION_ENABLED` are on; otherwise the fail-closed dark placeholder.
- The LocalSystem corroboration built in STREAM 9E is **kept as defence-in-depth** (it still closes the
  no-process / offline-terminal / stale-replay forgeries and enforces server-derived identity/session/runtime).
  It is not removed.

## Residual risk (stated explicitly)

- **If RemoteApp isolation were ever broken**, observation integrity is also broken: a code-executing tenant
  could advance its **own** account's readiness display/eligibility to `EXECUTION_READY` without a genuine
  broker session on the expected account. Bounded to the tenant's own account — **no cross-tenant, Customer Zero
  hard-refused**.
- **Execution integrity is NOT broken** in that scenario. Execution has its own independent runtime-identity
  validation (Execution Engine order-time identity authority, ADR-0034 Decision D): an order is pinned to the
  expected login/server and re-checked at order time, so a forged `EXECUTION_READY` over a wrong/absent
  connection yields **no phantom order**. Live trading is additionally gated (`HOSTED_MT5_EXECUTION_ENABLED` +
  per-workspace arm + the order-time bridge gate), all OFF.

## Consequences

- The live observation channel is DARK until `HOSTED_REMOTEAPP_ISOLATION_CERTIFIED` is set — which happens only
  after the RemoteApp/AppLocker behavioural escape-attempt certification passes on the host (the outstanding
  `REMOTEAPP_ISOLATION` gate, incl. the documented `%WINDIR%` LOLBIN residuals).
- `HOSTED_OBSERVATION_CERTIFIED` and everything downstream (`WORKSPACE_READY`, `AUTONOMOUS_ONBOARDING_CERTIFIED`)
  are gated behind `REMOTEAPP_ISOLATION_CERTIFIED` by definition.
- Tests lock the enforcement: an armed-but-uncertified configuration produces no observation and no advancement;
  the resolver stays dark; the corroboration-agreement and freshness/execution-ready locks remain.

## Alternatives considered (and rejected)

- **Remove the tenant-writable handoff via LocalSystem-captured direct execution** (CreateProcessAsUser +
  stdout): raises the bar to process injection but cannot fully close a code-executing tenant, changes the
  observer launch mechanism, is high-risk Windows-API work, and is largely untestable off-host. Rejected — it
  still reduces to "the tenant cannot run code", which is the isolation certification.
- **Broker-endpoint IP corroboration**: closes "no broker / wrong broker / MetaQuotes-only" but needs fragile
  per-broker IP intelligence and still cannot distinguish two accounts on the same broker or corroborate
  IPC/`trade_allowed`. Rejected as a partial layer that does not change the root conclusion.

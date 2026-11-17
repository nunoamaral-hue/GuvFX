# ADR-0117 — Copier production arming pipeline: real signed actuators, verifier-window coupling, governed execution driver, atomic arm/teardown state machine

- **Status:** Proposed (repository-only; DARK). PM owns lifecycle status in Notion.
- **Packet:** GFX-COPIER-PRODUCTION-ACTUATORS-PIPELINE-153
- **Date:** 2026-11-17
- **Supersedes:** nothing. Additive to ADR-0049 (signed bridge provisioning), ADR-0051/0057/0083 (copier + LIVE lifecycle), ADR-0074 (event ingestion / projection), ADR-0094 (immediate dispatch), ADR-0097 (one-action arm), ADR-0101 (bounded verifier window), ADR-0104/0112 (emitter key / keyring), ADR-0114 (atomic lifecycle arming), ADR-0115 (runtime close-authority guard).

## Context

The pkt-152/153 production-readiness audit proved that the ADR-0114 governed arm **cannot complete in production**, and that the previously-prepared control package was built on an incomplete mechanism:

1. **`emitter_enabler=None` dead end.** `arm_copier_live_lifecycle` always passes `emitter_enabler=None`;
   `lifecycle_arming.arm_live_lifecycle` therefore returns `_teardown("emitter_enabler_unavailable")` on every
   `--commit` and rolls fully back to DARK. No production code supplies a signed-host-executor-backed emitter
   actuator (grep: zero non-test assignments). `teardown_copier_live_dark` likewise passes
   `emitter_disabler=None`/`emitter_key_remover=None`.
2. **Standing emitter key provisioner not staged.** `deploy/copier-emitter/operator/Provision-GuvfxCopierEmitterKey.ps1`
   (pins account 46, refuses #48, ACL-restricted hidden-input write of `…\46\ingest.key`) exists in the repo but
   had never been placed on the host.
3. **Verifier window omitted.** The backend's ingest key is resolved **only** from a bounded ADR-0101 verifier
   window (`trade_copier.verifier_window`, tmpfs, TTL ≤ 900 s, stdin-only, key-id `copier-ingest`), never the
   environment. The arm did not gate on an open window, and the prior control package never opened one — the
   `/event-ingest/` endpoint would 503 on every emission.
4. **Immediate-push authority already exists but is DARK-gated and un-arm-wired.** `COPIER_EVENT_PROJECTION_ENABLED`
   promotes a verified `CopierIngestedRevision` (`source='EVENT'`) into exactly one authoritative `MasterEvent`
   on the shared poll/event ledger + baseline (ADR-0074); `COPIER_IMMEDIATE_DISPATCH_ENABLED` runs
   project→replicate→execute→lifecycle→mutation-dispatch→bounded-reconcile inline on signed ingest (ADR-0094),
   with push/poll dedup on one `MasterEvent.sequence` space. The slow observer + replication are the only
   stages scheduled in production (cron, every 2 min); `execute`/`mutation-dispatch`/`lifecycle-dispatch` are
   **not driven** by any supervised production process.
5. **Test seams masked the gaps.** The ADR-0114 synthetic tests inject fake actuators, so the suites were green
   while production had no actuator wiring at all.

Production remains DARK (`dc1f7685`); #46 and #48 flat; no authorization, arm, flag, window, key or broker action
has occurred.

## Decision

Wire the missing **governed production** pieces so that one atomic command can safely move the exact #46→#48
route through a single fail-closed state machine, with no partial arm and no stranded follower position. All of it
is **repository-only and DARK-by-default**: every new capability is gated behind the existing
`hosted_host_executor_enabled()` + keyring/key_id/base_url DARK factory and the copier flag stack, so nothing can
act until a Sponsor arms it out of band and the signed host-executor daemon is built + certified (a separate,
Sponsor-gated step — never in this packet).

### State machine (authoritative)

```
DARK → PREPARED → ARMED → EXECUTING → TEARDOWN → DARK
```

| State | Backend-provable facts | Host facts |
|-------|------------------------|-----------|
| **DARK** | no ACTIVE authz, no ENGAGED arm, no OPEN verifier window, 0 copier jobs/tickets, flags OFF, `execution_banner='SHADOW — EXECUTION OFF'` | admission OFF (`MT5_ALLOW_LIVE` absent), emitter task DISABLED/triggerless, no `ingest.key` |
| **PREPARED** | bounded authz granted (OPEN+MODIFY+FULL_CLOSE, ≤0.01, 1 order, ≤15 min); OPEN verifier window covering INGEST with TTL ≥ authz remaining; host key `-Verify` attested | `ingest.key` present (ACL-restricted); admission still OFF; emitter still DISABLED |
| **ARMED** | exactly one ENGAGED arm bound to the authz; all contract gates green | admission provisioned ON for #48; emitter `GuvFX_CopierEmitter_46` ENABLED |
| **EXECUTING** | governed driver holds the single lease; OPEN→MODIFY→FULL_CLOSE replicated for exactly one order | emitter emitting signed events; bridge admitting the bounded live order |
| **TEARDOWN** | flatness proven; driver stopped; arm disengaged; authz revoked; window closed | admission restored OFF; emitter DISABLED; `ingest.key` shredded |

Transition rule: **arm succeeds completely or tears down completely** (`arm_live_lifecycle` already rolls the
entire stack back on any post-validation failure); teardown is idempotent and independently callable from any
state; execution OFF is the default after restart/deploy (flags default OFF; the driver holds no lease at start).

### 1. Signed host actuators (`hosted_workspace.host_executor`)

Add narrowly-scoped, allow-listed `SignedHostExecutor` operations, each mapping to **one** typed op on
`host_protocol` (signed request, nonce/replay-safe, `verify_hosted_response`), sending only `account_id` + a typed
op — the host derives identity/paths and re-asserts confinement:

- `enable_copier_emitter(runtime_root)` → `ENABLE_COPIER_EMITTER` — enables the fixed, pre-installed, DISABLED
  `GuvFX_CopierEmitter_46` task (exact reviewed action `run_emitter.py run --config …\46\config.json`, principal
  `guvfx_u_46`). **Account-pinned to #46** before any host contact (mirrors `trigger_emitter_oneshot_attest`).
- `disable_copier_emitter(runtime_root)` → `DISABLE_COPIER_EMITTER` — re-disables the same task. Idempotent.
- `remove_copier_emitter_key(runtime_root)` → `REMOVE_COPIER_EMITTER_KEY` — the host runs the reviewed provisioner
  `-Remove` (shred + delete `…\46\ingest.key`). **#46-pinned.** Idempotent (already-absent ⇒ ok).
- `read_copier_host_state(runtime_root)` → `READ_COPIER_HOST_STATE` — **value-blind** read-back: emitter task
  enabled/disabled + triggerless + principal, `ingest.key` present + ACL-ok (metadata only, never the key),
  admission posture. Read-only on the host.

Admission provision/revoke already exist (`activate_tenant_bridge_live_authorized` /
`restore_tenant_bridge_execution_off` on the follower; ADR-0049) and are reused unchanged.

**Fail-closed** on: account other than the pinned one; Customer Zero / reserved ids (`_send` guard); arbitrary
command/task/path (the host derives them; the op sends none); wrong principal/path/hash, stale/replayed signature,
unknown key-id, identity mismatch (the `host_protocol` sign/verify + host re-derivation); missing rollback state.
The new ops add no params the host trusts; the emitter ops send **no** caller params at all.

### 2. Production adapters (`trade_copier.live_host_actuators`)

A new module providing the four injected lifecycle interfaces as **real production adapters** that resolve the
DARK-gated signed executor and verify the signed attestation — removing the `emitter_enabler=None` dead end:

- `make_admission_provisioner(follower_account_id, authorization_id)` → wraps `provision_live(…, commit=True)`.
- `make_admission_revoker(follower_account_id)` → wraps `restore_off(…, commit=True)`.
- `make_emitter_enabler(master_account_id)` / `make_emitter_disabler(…)` / `make_emitter_key_remover(…)` →
  resolve `resolve_signed_host_executor(account_id=46)`; if `None` (DARK) return
  `{"ok": False, "enabled"/"disabled"/"removed": False, "reason": "host_executor_unavailable"}` (fail closed);
  else dispatch the matching signed op and verify the attestation (ok + account + operation echo).

Because the factory is DARK (executor `None`) until `HOSTED_HOST_EXECUTOR_ENABLED` + keyring/base_url are set and
the host daemon exists, a `--commit` arm in the repository-only phase fails closed at the emitter step with the
**truthful** reason `emitter_enable_failed` (host executor unavailable) and rolls back — never `emitter_enabler
is None`, and never a partial arm. The wiring is correct and certification-ready; test fakes stay test-only (the
executor factory is never faked in production code).

### 3. Key + verifier-window coupling (`trade_copier.verifier_window` + arm gate)

The account-46 ingest key is a **human secret** entered once via the reviewed hidden-input wrapper; identical
bytes reach (a) the ACL-restricted host key file (`Provision-GuvfxCopierEmitterKey.ps1`, stdin) and (b) exactly
one backend ADR-0101 verifier window (`copier_verifier_window open`, stdin). The key never appears in argv, env,
logs, Git, Notion or chat. The arm **refuses** unless: an OPEN window exists, its purpose covers INGEST, its
remaining TTL ≥ the authorization's remaining TTL, and the host key `-Verify` attestation passed (value-blind).
A new contract gate `VERIFIER_WINDOW_NOT_READY` enforces this; teardown closes the window (shred) and removes the
host key, and any failure at either destination removes the other side and returns DARK.

### 4. Immediate-push authoritative path (reuse + harden)

The authoritative ordering already lives in `MasterEvent.sequence`. A signed, verified emitter event is promoted
into **exactly one** `MasterEvent` by the existing projection (`COPIER_EVENT_PROJECTION_ENABLED`, ADR-0074) under
the same per-master row lock + monotone sequence the observer uses; `COPIER_IMMEDIATE_DISPATCH_ENABLED` (ADR-0094)
makes it the immediate primary by running the governed passes inline. An **unauthenticated observation never
becomes execution authority**: only the HMAC+identity+nonce+timestamp+replay-verified event is persisted, and only
the projection (not the raw ingestion) writes a `MasterEvent`. Fast polling (observer) is the bounded fallback and
slow observer is reconciliation-only; push and a later poll of the same ticket dedup on the shared sequence/baseline
(no double copy; one source event ⇒ at most one authoritative event ⇒ one follower lifecycle). This packet adds no
new promotion authority; it verifies the dedup/fallback invariants under test and wires the arm to require the
projection + immediate-dispatch gates when the immediate path is the armed primary.

### 5. Governed execution driver (`run_copier_execution_driver`)

A single **supervised, bounded** management command that drives the existing stages
(replication → execute OPEN/MODIFY/FULL_CLOSE → mutation/lifecycle dispatch → reconciliation/containment) for the
fast-poll fallback and reconciliation, replacing ad-hoc manual invocation:

- Does nothing when the flags / authorization / arm are OFF (every stage re-reads its own gate).
- **One active lease** (reuses the `hosted_workspace.recovery_lease` single-flight pattern, new kind
  `copier_execution_driver`): a second driver fails closed.
- Crash-safe restart; idempotent claims (existing per-(master,ticket,revision) + ExecutionJob idempotency);
  bounded retry; explicit dead-letter / governed alert state; authorization expiry halts new OPENs while the
  per-op containment paths (MODIFY/FULL_CLOSE) remain available for an already-bound position per ADR-0083/0115;
  teardown waits for or safely resolves in-flight lifecycle work (teardown's flatness-first halt).
- A read-only status/readiness projection that exposes **no** secret.

The immediate-dispatch inline path (ADR-0094) remains the ≤1 s primary; the driver is the fallback/reconciliation
executor, not a competing truth.

### 6. Atomic arm + teardown (`arm_copier_live_lifecycle` / `teardown_copier_live_dark`)

`arm_copier_live_lifecycle` becomes the authoritative orchestrator. `--dry-run` reports every required gate and the
planned mutations. `--commit` runs, in order, with full reverse-order teardown on any failure: reverify both
accounts flat (read-only evidence + the required human-confirm token) → verify route/identities/mapping/caps/kill
switch → verify authz carries OPEN+MODIFY+FULL_CLOSE → verify host key + backend verifier window → provision
admission → enable emitter → start/confirm the governed driver → persist exactly one ENGAGED arm → read everything
back → return ARMED only if all pass. `teardown_copier_live_dark` (flatness-first, already idempotent): stop new
OPEN, contain/complete permitted MODIFY/FULL_CLOSE, stop driver, disable emitter, revoke admission, revoke
authz/arm, close window, shred key, verify zero active jobs/tickets and the DARK banner + flags.

## Invariants (binding)

1. Arm succeeds completely or tears down completely; no partial arm persists.
2. No live capability before every gate passes.
3. No OPEN without armed MODIFY **and** FULL_CLOSE authority + execution path (`OPEN_WITHOUT_CLOSE_REFUSED`).
4. No emitter enabled without a matching bounded backend verifier window (`VERIFIER_WINDOW_NOT_READY`).
5. No execution/dispatch driver without an active route authorization **and** engaged arm.
6. No authorization survives teardown (revoked).
7. No partial-close authority; no arbitrary account, task, command, endpoint or symbol.
8. #46 emitter only; #48 follower only; `XAUUSD → XAUUSD.std`; max volume 0.01; max orders 1; max
   authorization/window TTL 15 minutes.
9. Kill switch and expiry fail closed; revocation triggers containment and cannot strand an opened follower
   position (ADR-0115).
10. Teardown is idempotent and independently callable; execution OFF is the default after restart/deploy.

## Consequences

- The governed arm becomes certification-ready: the only remaining steps to a live demo are the Sponsor-gated host
  actions (build + certify the signed host-executor daemon; stage the provisioner; human key entry) and the
  human flatness/consent/master-trade gates.
- Repository-only + DARK: in this phase `--commit` still fails closed (executor unavailable) and rolls back —
  but for the truthful reason, with the real wiring in place and fully tested with fakes.
- No production/Windows mutation, no real broker order, no real secret, no verifier-window opened by this packet.
- Three independent adversarial review lanes (financial-safety/stranding/concurrency; security/signing/secret/
  test-seam; Windows-primitive/ACL/rollback/teardown) found **no CRITICAL**, **1 HIGH** (a `max_uses`-capped verifier
  window auto-closes after the first event and strands the follower CLOSE → the arm now refuses a use-capped window),
  **1 MEDIUM** (`_verify_host_ok` defaulted an absent account/operation echo to pass → now required present-and-equal,
  and the primitive emits `operation`), and several LOW (RULE-11 `Settings/Enabled` parse, empty-DACL ACL guard,
  RemoveKey directory guard, default-rollback emitter wiring, driver-status lease path, teardown DARK fail-closed on
  the load-bearing window re-count, RULE-5 doc drift) — all fixed. The partition against double/wrong-owner dispatch,
  the NULL-safe `next_job` exclusion, revoke/expiry fail-closed, the single-flight lease, and teardown's flatness-first
  halt were confirmed correct.

## Testing

Synthetic only, fake bridges/keys: the production command no longer passes `emitter_enabler=None`; each actuator
success/failure/read-back; arbitrary-command/task/account refusal; signed replay/stale/unknown-key rejection;
atomic arm success and full DARK rollback after every individual step; immediate event ⇒ exactly one `MasterEvent`;
immediate+poll race ⇒ one event/order; duplicate/replay ⇒ zero additional order; OPEN→MODIFY→FULL_CLOSE;
revoked-after-open containment; no modify/close authority ⇒ OPEN refused; partial close rejected; wrong
symbol/account/identity rejected; authorization/window expiry; driver crash/restart + single lease; dispatch retry
without duplicate; teardown from every intermediate state; secret/log/argv scan; deterministic latency ≤1 s target,
≤2 s ceiling; broker fill latency reported separately. Plus full `trade_copier` + `execution`, governance/evidence/
ADR checks, secret scan, migration checks, and Windows PowerShell 5.1 ParseFile + Pester where PowerShell assets
change.

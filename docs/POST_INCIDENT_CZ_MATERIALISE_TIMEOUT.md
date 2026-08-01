# Post-incident & remediation — Customer Zero MATERIALISE timeout / idempotency / retry

- Date: 2026-08-01
- Status: remediation engineering-complete on branch `fix/cz-materialise-timeout-idempotency`; **not deployed**
- Scope: engineering + read-only evidence only (Sponsor directive). No prod deploy, no DB mutation, no agent
  op, no slot cleanup, no Customer Zero retry.
- Related: [ADR-0023](ADRs/0023-materialise-timeout-and-ambiguous-reconcile.md),
  [ADR-0021](ADRs/0021-permanent-dedicated-runtime-onboarding.md), [ADR-0014](ADRs/0014-management-protocol-release-operation.md).

## 1. Incident

The first genuine controlled Customer Zero provisioning drove `ProvisioningJob #1` (AccountRuntime pk1,
runtime UUID `66972e0e-c803-49b5-8da2-a5df1c14e90d`, user #16 / TradingAccount #12) into `FAILED` at
**MATERIALISE**, although the golden copy **completed on the host**. Production was untouched throughout; no
process was launched, no broker op, no order, no trade.

### Timeline (UTC, 2026-08-01)

| Time | Event |
|---|---|
| 10:16:41 | Provisioner armed (`BETA_RUNTIMES_ENABLED` 0→1, provisioner-only file). |
| 10:16:42 | Worker claimed Job #1 → QUEUED→PROVISIONING (materialising). Agent `assign()` → **slot 2 / gen 4**; golden copy begins into `C:\GuvFX\beta\slots\2\terminal`. |
| 10:17:02 | `op_ambiguous_timeout` (`channel_timeout`) — client `requests.post(timeout=20)` fired at ~20s while the copy ran. |
| 10:17:02.796 / .954 | Two instant retries → `materialise_failed` (`runtime_busy`) — the copy still held the per-runtime lock. `MAX_ATTEMPTS=3` exhausted in ~0.3s. |
| 10:17:02.957 | PROVISIONING→FAILED; Job #1 FAILED (attempt 3). |
| 10:17:23 | Agent `stage_copy` recorded **COMPLETED** (`occupancy_sequence(2,4)`); copy finished ~41s after start. |
| 10:18:15 | Provisioner returned to DARK (0). |

## 2. Root cause (verified against code, not narrative)

Two backend (client) defects; the agent behaved correctly.

1. **One blanket transport timeout.** `beta_worker.make_http_transport()` used a single 20s timeout for every
   op. MATERIALISE copies the ~380 MB golden (measured **~41s** on the beta host), so the client timed out
   mid-copy → `op_ambiguous_timeout`.
2. **Blind re-POST + mis-classification.** On the ambiguous timeout the driver immediately re-POSTed
   MATERIALISE. The agent holds the per-runtime lock for the whole copy and stores its idempotent `(job_id,op)`
   result only *after* completion, so the resends got `runtime_busy`. `provisioner._step`'s generic
   `except Exception` mapped `runtime_busy` → `materialise_failed`, which on the exhausting attempt routed to
   the hard-`FAILED` branch.

**Verified lock/put ordering (RULE 11).** The directive's assumed ordering ("acquire lock; run; STORE result;
release lock") is *wrong*. `mgmt_agent_core.handle` releases the lock **before** `idempotency_store.put`, so a
resend in the microsecond window between release and `put` could re-enter the impl. This window is **benign**
for MATERIALISE (idempotent `assign` → same slot/gen; destination-guarded `stage_copy` → `ALREADY_COMPLETED`,
no re-copy, refuses partial) and was **not** the incident's cause (the incident's `runtime_busy` came from
resends *during* the copy, lock held).

## 3. Remediation (shipped on branch — client-side only, no migration, no agent change)

See ADR-0023 for the design. Files:
- `beta_worker.py` — per-op `(connect,read)` timeout map (`OP_TRANSPORT_TIMEOUTS`, `_op_read_timeout`), clamp,
  settings/env override.
- `provisioner.py` — in-attempt reconcile (`_reconcile`, poll-not-repost); `_step` reclassifies
  `runtime_busy`/timeout as "still running"; `PARTIAL_REASONS` fail-closed + quarantine; `LEASE_TTL_SECONDS`
  300→1200 + `assert_lease_covers_op_timeouts()`.
- `run_beta_provisioning_worker.py` — fail-closed lease/timeout coupling guard at startup.
- `billing/beta.py` — `BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS` default 120→900.

### 3a. Timeout & retry configuration reference

| Operation | connect | read (default) | Why |
|---|---|---|---|
| NEGOTIATE | 10 | 10 | read-only handshake; fast-fail a hung agent |
| VERIFY | 10 | 15 | process observation returns promptly |
| RELEASE | 10 | 30 | one atomic SQLite tx + audit walk |
| START | 10 | 60 | launch trigger + ~20s settle poll + margin |
| STOP | 10 | 90 | terminate trigger + ~30s settle poll + margin |
| TOMBSTONE | 10 | 120 | move ~380 MB / 586 files aside + verify |
| **MATERIALISE** | 10 | **300** | ~380 MB golden copy (measured ~41s) + verify; bounded ~7× |
| (unmapped) | 10 | 20 | scalar fallback |

- Override: `settings.BETA_AGENT_OP_TIMEOUTS` or env `BETA_AGENT_OP_TIMEOUTS` (JSON), each value **clamped** to
  `MAX_TRANSPORT_READ_TIMEOUT = 600`.
- Reconcile budget: `PROVISIONING_MATERIALISE_MAX_WAIT_SECONDS = 300` (backoff 5→30s).
- Coupling invariant (fail-closed at worker startup + CI): `LEASE_TTL_SECONDS (1200) > materialise_read +
  reconcile_budget`.
- Heartbeat: `BETA_PROVISIONER_HEARTBEAT_TTL_SECONDS` must stay **≥ MATERIALISE read** (default 900).

### 3b. Operator runbook — ambiguous long operations

- **Symptom:** worker log shows a reconcile poll after a MATERIALISE timeout / `runtime_busy`. This is
  **normal** for a long copy — do NOT restart the worker or re-arm; the reconcile is bounded (≤ ~600s total).
- **Recovery (expected):** the resend returns the agent's stored result once the copy completes → the runtime
  advances START→VERIFY→RUNNING on its own.
- **Quarantine (`ambiguous_timeout`):** the copy did not confirm within the budget. The terminal MAY be
  materialised. Do **not** re-arm blindly. Reconcile read-only (inspect the slot store `stage_evidence` for
  `stage_copy COMPLETED`, the `.guvfx_owner`/`.guvfx_golden_manifest` markers, and the slot dir), then use the
  operator reclaim path (§5) under Sponsor authorisation.
- **Quarantine (`partial_materialise`):** a proven-partial copy. The slot must be reclaimed
  (STOP→TOMBSTONE→RELEASE) before any re-materialise; never re-drive a partial.
- **Never** hand-edit `slots.sqlite`, the runtime DB, or the slot filesystem. Cleanup is via signed agent
  lifecycle ops only.

## 4. Orphaned slot-2 analysis (read-only; NOT cleaned)

Current agent + host state (verified 2026-08-01, read-only):

| Property | Value |
|---|---|
| Occupancy | slot **2**, generation **4**, runtime_uuid `66972e0e-…` (CZ) |
| Materialisation | **COMPLETE + integrity-valid**: `stage_evidence(2,4)` = `stage_copy COMPLETED`; 586 files / 396,694,304 bytes; `terminal64.exe` present (116 MB) |
| Owner marker | `.guvfx_owner` = `{gen 4, uuid 66972e0e-…, slot 2}` (present, matches occupancy) |
| Portable / golden markers | `.guvfx_portable` present; `.guvfx_golden_manifest` = `5.0.0.6073` |
| Process state | **none** (never launched — no `confirm_launch`, no birth evidence, no slot-2 `terminal64.exe`) |
| Scheduled tasks | `GuvFXBetaRuntime-2` (launch) + `GuvFXBetaRuntimeStop-2` (terminate) both **Ready + approved** |
| Quarantine / allocation-block | clear; generation ledger monotonic (slot 2: init→gen 4) |
| Backend DB | AccountRuntime pk1 = `FAILED`; Job #1 = `FAILED` — **diverged** from the agent slot store |

**RELEASE legality from the current occupancy.** `op_release` requires a **live-ABSENT** observation (✓, no
process) **plus recorded `tombstone` + `verify_cleanup` evidence** (✗ — only `stage_copy` exists). So RELEASE is
**not directly legal**; a TOMBSTONE (and, for a clean confirm-terminated record on a never-launched runtime, a
STOP first) must run before RELEASE. Reclaim sequence: **STOP → TOMBSTONE → RELEASE**. `op_release` explicitly
handles a proven-empty slot with no launch record (it sets `process_identity_verified` from the empty-slot
observation), so a never-launched occupancy releases safely once tombstoned.

**How the backend FAILED state affects RELEASE.** The agent RELEASE operates on the agent slot store + host and
is independent of the backend DB state — a `FAILED` runtime does not block the agent from releasing slot 2.
**However**, the backend has **no code path that issues STOP/TOMBSTONE/RELEASE for this runtime today**:
- `AgentWindowsProvisioner` (mgmt_client) implements materialise/configure/start/verify/stop/**teardown
  (=TOMBSTONE)** — there is **no `release()` method**, and **no command issues RELEASE**.
- `_drive_stop` only acts on a runtime in `HELD_STATES` (FAILED is not one) → no-op for CZ.
- `_drive_deprovision` (teardown=TOMBSTONE→REMOVED) can act on a FAILED runtime, but does **not** RELEASE, so
  the slot would be tombstoned yet never returned to Available (generation never advances). This is a
  pre-existing latent pool-leak.
- `reconcile_beta_provisioning` only re-drives **NOT_PROVISIONED** runtimes — not FAILED, not orphans.

**Conclusion (answers the directive's question).** Cleanup requires a **new governed reconciliation command**
(a backend RELEASE driver + a reclaim command), which does **not yet exist** and must be built under the
cleanup authorisation. It must use the supported signed agent lifecycle — never manual filesystem/SQLite edits.

## 5. Orphaned-slot recovery plan (NON-EXECUTED — Sponsor-gated)

To be built + run under a separate "Customer Zero – Orphaned Slot Cleanup and Controlled Retry Preparation"
authorisation. **No step executed in this phase.**

1. **[STOP gate]** Confirm the remediation is merged + deployed (backend recreate) and the provisioner is DARK.
2. Fresh verified prod DB backup (path/size/sha256/gzip-integrity), preserve the DARK provisioner file.
3. Build a governed backend RELEASE driver + reclaim command (engineering, reviewed, tested):
   - `mgmt_client.AgentWindowsProvisioner.release()` → signed `RELEASE` (RELEASE is already in
     `PROVISIONING_OPERATIONS` + the agent `op_release`; this is client wiring only).
   - `manage.py reclaim_beta_runtime` (operator-gated, enqueue-only): **RECLAIM** mode transitions the runtime
     to DEPROVISIONING and drives **STOP → TOMBSTONE → RELEASE** via signed ops; advances slot 2 gen 4→5 and
     returns it to Available. No manual slot fabrication.
4. **[STOP gate]** With the provisioner briefly armed under authorisation, run reclaim RECLAIM on CZ.
5. Prove via agent evidence (not manual DB reads): `slots(2)` back to `runtime_uuid=NULL`, generation **5**,
   `slot_generations` shows the `release`, `slot_audit` shows `slot_released`; slot 2 dir tombstoned under
   `C:\GuvFX\beta\tombstones\2\<occupancy_id>`.
6. Return the provisioner to DARK. Rollback: the reclaim is enqueue-only + signed; a partial run leaves the
   runtime still-assigned + tombstoned and a resend completes; DB restore only if ever needed.

## 6. Customer Zero retry plan (NON-EXECUTED — Sponsor-gated)

1. **Prerequisites:** remediation merged + deployed; slot 2 reclaimed (§5) and proven Available at generation 5;
   provisioner DARK; fresh backup.
2. **Reconcile the FAILED runtime/job → re-provisionable.** CZ's runtime is `FAILED` and Job #1 is `FAILED`;
   `_drive_provision` does not act on `FAILED`, so a bare re-enqueue is a no-op. The reclaim command's **RESUME**
   mode (governed, enqueue-only) transitions the runtime `FAILED → NOT_PROVISIONED` (or QUEUED) via
   `record_transition` and enqueues **one** fresh `PROVISION` job. Ensure exactly one claimable job
   (`uniq_active_job_per_runtime_op` guarantees at most one active job per (runtime, op)).
3. **Controlled attempt:** arm the provisioner-only flag 0→1; permit exactly one claim of the new PROVISION job.
4. **Observe** NEGOTIATE → MATERIALISE (now within the 300s read budget; a slow copy reconciles, never
   false-fails) → START → VERIFY → **RUNNING**; Job → DONE; immutable Verification Report with
   `broker_login_verified=False`.
5. **Immediate return to DARK.** Confirm ARM=0, heartbeat IDLE_READY, no second claim.
6. **Proof requirements for RUNNING:** AccountRuntime RUNNING; Job DONE; Verification Report `verified=True`,
   `broker_login_verified=False`; RuntimeEvent chain complete; slot/generation recorded; process stable; no
   quarantine; no duplicate job/runtime/occupancy; production unaffected (watermark unchanged, prod terminals
   4336/8748 alive).

Every arming / claim / reclaim step above requires explicit Sponsor authorisation. Cleanup, reconciliation and
retry are **not** combined into an automatic deployment.

## 7. Customer Zero evidence matrix

| Item | Data-plane (host) | Control-plane (agent slot store) | Backend DB | Notes |
|---|---|---|---|---|
| MATERIALISE | **COMPLETED** (586 files / 378 MB, exe present) | `stage_copy COMPLETED`, slot 2 gen 4 assigned | Runtime/Job **FAILED** | control-plane false-negative |
| Ownership | `.guvfx_owner` gen 4 uuid match | occupancy (2,4) | — | integrity-valid |
| Process | none | no launch/birth record | — | never started |
| Broker / trade | — | — | 0 acct-12 trades, 0 execjobs | broker-independent; no order |
| Production | terminals 4336/8748 unchanged | — | Trade max 430 / ExecJob max 20647 unchanged | no regression |

## 8. Distinctions preserved (RULE 5)

- **Functional data-plane success** (the copy completed on the host) ≠
- **Control-plane acknowledgement** (the client never received the ok due to the timeout) ≠
- **Backend lifecycle state** (`FAILED`) ≠
- **Agent slot occupancy** (slot 2 → CZ, gen 4, materialised).

These four were conflated by the incident and are kept distinct by the remediation + this record.

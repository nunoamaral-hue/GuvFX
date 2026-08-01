# Customer Zero — orphaned-slot reclaim + failed-runtime recovery (operator runbook + dry-run plans)

- Date: 2026-08-01
- Status: tooling engineering-complete on branch `feat/cz-orphan-reclaim-recovery`; **not deployed, not executed**
- Scope: engineering + non-executed plans only. NO production cleanup, recovery-apply, arming, or retry.
- Related: [ADR-0024](ADRs/0024-governed-orphan-reclaim-and-failed-runtime-recovery.md),
  [ADR-0023](ADRs/0023-materialise-timeout-and-ambiguous-reconcile.md),
  `docs/POST_INCIDENT_CZ_MATERIALISE_TIMEOUT.md`.

## Two durable authorities (keep distinct — RULE 5)

| Authority | What it holds | Repaired by |
|---|---|---|
| **Agent slot occupancy** (`slots.sqlite`) | slot 2 → CZ uuid `66972e0e…`, generation 4, materialised | **Phase 1 reclaim** (STOP→TOMBSTONE→RELEASE) |
| **Backend runtime state** (`AccountRuntime pk1`) | `FAILED`; `ProvisioningJob #1` `FAILED` (history) | **Phase 2 recover** (`REMOVED→NOT_PROVISIONED` + 1 job) |

Neither phase provisions. **Arming (Phase 3) is a separate Sponsor-controlled operation.**

## Commands (dry-run by default)

```bash
# Phase 1 — inspect (no mutation, no signed mutating request):
python manage.py reclaim_beta_runtime --runtime-uuid 66972e0e-c803-49b5-8da2-a5df1c14e90d
# Phase 1 — inspect + a read-only signed occupancy probe:
python manage.py reclaim_beta_runtime --runtime-uuid 66972e0e-c803-49b5-8da2-a5df1c14e90d --probe-agent
# Phase 1 — execute (STOP -> TOMBSTONE -> RELEASE), asserting the expected occupancy:
python manage.py reclaim_beta_runtime --runtime-uuid 66972e0e-c803-49b5-8da2-a5df1c14e90d \
    --expect-slot 2 --expect-generation 4 --apply
# Phase 2 — inspect / execute (backend only, provisioner stays DARK):
python manage.py recover_beta_runtime --runtime-uuid 66972e0e-c803-49b5-8da2-a5df1c14e90d
python manage.py recover_beta_runtime --runtime-uuid 66972e0e-c803-49b5-8da2-a5df1c14e90d --apply
```

**Safety rails:** BETA-only (a PRODUCTION runtime is refused); no active job; provisioner DARK (unless
`--allow-armed`); UUID/slot/generation/running probe match; a **stable job_id** (default the retained PROVISION
job) reused across every STOP/TOMBSTONE/RELEASE and every re-run — do **not** override it with a fresh id, or a
STOP re-send after TOMBSTONE quarantines the slot. On any unresolved agent step the runtime is **quarantined
and never marked REMOVED**. Never edit `slots.sqlite` / the slot filesystem by hand.

## 4. Slot cleanup dry-run plan (NON-EXECUTED — Sponsor-gated)

1. **[STOP gate]** Reconfirm PR#252 deployed (backend + provisioner on the fixed image) and this tooling
   deployed; provisioner DARK; Customer Zero `FAILED`, slot 2 gen 4 materialised, no process.
2. Fresh verified `pg_dump` (path/size/sha256/gzip); record the agent slot-store + generation baseline
   (read-only).
3. `reclaim_beta_runtime --runtime-uuid 66972e0e… --probe-agent` (**dry-run**): confirm the resolved target
   equals the expected — slot **2**, generation **4**, Customer Zero UUID, `running=False`, anchor `job_id` =
   Job #1.id, plan = STOP→TOMBSTONE→RELEASE.
4. **[STOP gate — Sponsor review of the dry-run].**
5. Under a later explicit apply authorisation: `--expect-slot 2 --expect-generation 4 --apply` →
   signed STOP (prove ABSENT) → signed TOMBSTONE (verify) → signed RELEASE → prove **slot 2 Available,
   generation = 5** (read the agent slot store: `runtime_uuid=NULL`, `generation=5`, `slot_generations`
   appends `(2,5,'release')`, `slot_audit` `slot_released`; slot dir under `tombstones\2\<occupancy_id(2,4)>`).
   Backend runtime → `REMOVED`.
6. Return evidence, **STOP**. No backend recovery in the same apply authorisation.
7. **Rollback:** the sequence is idempotent under the stable job_id; a mid-sequence failure quarantines the
   runtime (never REMOVED) and is re-drivable; DB restore only for an evidenced persistent mutation. RELEASE
   after commit is `runtime_not_assigned` (never a second generation advance).

## 5. Customer Zero recovery dry-run plan (NON-EXECUTED — Sponsor-gated)

1. Slot 2 already proven **released** (generation 5, Available) by Phase 1.
2. Provisioner DARK; Customer Zero backend runtime `REMOVED`.
3. `recover_beta_runtime --runtime-uuid 66972e0e…` (**dry-run**): shows the proposed transition
   (`REMOVED→NOT_PROVISIONED`, clear quarantine) + the proposed **single** PROVISION job + the current job
   inventory; creates nothing.
4. **[STOP gate — Sponsor review].**
5. Under a later explicit apply authorisation: `--apply` → governed `REMOVED→NOT_PROVISIONED` transition →
   **exactly one** active `PROVISION` job (asserted); Job #1 retained `FAILED`; provisioner stays **DARK**
   (the new job is inert — `process_one` returns `disabled`).
6. Return evidence, **STOP**. The actual retry to RUNNING is a third, separately-authorised phase (arm → one
   claim → observe → return DARK).
7. **Rollback:** idempotent (`uniq_active_job_per_runtime_op` + `enqueue_op` recovery); a re-run never
   duplicates the job.

## Evidence matrix (what proves each step)

| Step | Proof (read-only) |
|---|---|
| Reclaim target correct | dry-run probe: slot 2 / gen 4 / uuid `66972e0e…` / `running=False` |
| RELEASE done | agent slot store: `slot 2 runtime_uuid=NULL, generation=5`; `slot_generations (2,5,'release')`; `slot_audit slot_released`; tombstone dir `tombstones\2\<occ(2,4)=379ff98c…>` |
| Backend post-reclaim | `AccountRuntime pk1 = REMOVED`; new immutable `RuntimeEvent` (`RECLAIM`/`slot_reclaimed`); **no** ProvisioningJob created by reclaim |
| Recover done | `AccountRuntime pk1 = NOT_PROVISIONED`; exactly **one** active `PROVISION` job; Job #1 still `FAILED`; `RuntimeEvent` (`RECOVER`/`recover_reset`) |
| Still safe | provisioner `ARM=0`, HB `IDLE_READY`, `process_one → disabled`; production unaffected; no order/broker/strategy; account #1 untouched |

## Failure handling

- Any unresolved reclaim step (proven-partial / budget-exhausted ambiguous / channel error at exhaustion):
  runtime **quarantined**, **not** REMOVED; surfaced with the agent's sanitised code; re-drivable under the
  same job_id. Do not hand-repair the filesystem.
- `agent_release_unsupported` / `impl_integrity_mismatch`: the running agent predates RELEASE — do not proceed
  (should not occur; the golden/agent are unchanged).
- STOP blocks on `task_definition_drift` / `approved_task_definition_missing`: confirm the terminate task
  `GuvFXBetaRuntimeStop-2` is intact (B3P-2 install-gate state) before Phase 1.

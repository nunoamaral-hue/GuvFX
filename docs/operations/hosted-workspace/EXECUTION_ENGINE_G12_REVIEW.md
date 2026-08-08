# ADR-0034 Execution Engine — G12 completion adversarial-review record

Machine-readable disposition of the multi-lens adversarial review of the G12 completion seam (execution
provenance + telemetry + reconcile), on branch `feat/adr0034-execution-engine` (PR #315). Method: 5 finder
lenses → per-finding independent skeptic refutation → synthesis. Scope reviewed:
`execution/hosted_execution.py`, `execution/hosted_reconcile.py`, `execution/hosted_pin.py` (uuid stamping),
`execution/models.py` (`ExecutionJob` fields + `HostedWorkspaceExecution`), `execution/views.py`
(dispatch/complete hooks).

## Result

- Surviving HIGH: **0**
- Surviving MEDIUM: **0**
- Surviving LOW: **1** (CONFIRMED) — **FIXED** (a second lens reported the same defect; a third finding was
  REFUTED to NONE).

## Dimensions & dispositions

| Lens | Dimension | Disposition |
|------|-----------|-------------|
| order-authority | Can any new path place/close/modify/re-send an order, arm execution, or displace the bridge? | **CLEAN** — no findings. Reconcile never acts on `may_retry`; the dispatch/complete hooks only RECORD; nothing creates a job or calls `order_send`. A guard test asserts no `may_retry` consumer re-sends. |
| dark-failsafe | True no-op while OFF; a provenance/telemetry error cannot break claim/complete. | **CLEAN** — no findings. Flag-first checks; hooks are post-commit + swallow errors. |
| idempotency-integrity | Replay/concurrency on the append-only `(job,phase)` row + telemetry dedup. | **1 LOW (fixed)** — see below. Unique `(job,phase)` + seq-keyed dedup otherwise sound. |
| secret-leak | Credential/secret escape via rows, telemetry, HWX key, logs. | **CLEAN** — no findings. Only identifiers (uuid, `HWX-<digest>`, login/server) are persisted/emitted. |
| correctness | Classifier usage; outcome recording; uuid stamping; M3c canonical-state boundary. | **1 LOW (same defect as above, fixed)** — the code correctly does NOT mutate `canonical_state`. |

## The one surviving finding (FIXED)

- **`hosted_reconcile._record_reconciliation` — RECONCILED outcome truncated.** `HostedWorkspaceExecution.outcome`
  was `CharField(max_length=16)`, but the classifier constants `confirmed_executed` (18) and
  `confirmed_not_executed` (22) exceed 16 → silently truncated; the row is append-only, so it could not be
  corrected in place. Latent (no consumer read the field; the live verdict is still returned in
  `ReconcileResult.classification`), hence LOW.
- **Fix:** widened `outcome` to `max_length=24`; capped writes at `[:24]`; regenerated migration `0028`; added
  `test_reconciled_outcome_persisted_untruncated_for_all_classes` asserting all three codes round-trip
  untruncated (the pre-existing test only asserted row existence — the exact gap the review caught).

## REFUTED (not surviving)

- **"Ambiguous verdict frozen at first evaluation."** REFUTED → NONE. No re-reconciliation caller exists; the
  append-only one-row-per-`(job,phase)` behaviour is the intended provenance invariant; the verdict is
  recomputed fresh each call and returned live.

Invariants confirmed intact by the review: bridge is sole order-time gate; no order/attach/login; DARK;
fail-safe; idempotent; demo-only; no credential; no auto-resend; canonical M3c state untouched.

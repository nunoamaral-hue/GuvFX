# ADR-0125 — monitoring_reconcile never reports a rollback failure for an apply that was never dispatched

- **Status:** Accepted (repository-only; additive bug fix to existing tooling; migration-free; no production deploy,
  no flag, no schema/DB change, no broker/MT5/host contact).
- **Packet:** GFX-MONITORING-RECONCILE-ROLLBACK-ACCOUNTING-168 (Dashboard live-positions monitoring / correctness).
- **Date:** 2026-11-24.
- **Relates to / amends:** ADR-0111 (GFX-137) — `hosted_workspace.monitoring_reconcile`, the dedicated,
  monitoring-ONLY read-only-positions bridge reconciler. This ADR corrects one failure-accounting branch in that
  module; the ADR-0111 design, gates and safety invariants are otherwise unchanged. Backlog item (b) in
  `docs/NEXT.md`.

## Context

`reconcile_account` applies a per-tenant, execution-OFF bridge env through an injected `MonitoringReconcileOps`
transport (`apply_off_env(...) -> (ok, attestation, reason)`). On an attestation failure it **fails closed** and rolls
the account back to its captured byte-exact baseline, reporting `rolled_back` / `rollback_reason`.

The pre-fix attestation-failure branch was entered whenever the post-apply attestation did not verify:

```python
att_ok, att_reason = (_verify_off_attestation(...) if apply_ok else (False, apply_reason))
if not att_ok:
    rb_ok, rb_att, rb_reason = ops.apply_off_env(... baseline ...)     # "rollback"
    ...
    "rollback_reason": (None if rb_att_ok else "rollback_failed")
```

This conflated two structurally different situations, keyed by `apply_ok` — the transport's "did I actually dispatch
the signed env to the bridge" signal:

1. **`apply_ok=True`, attestation unverified** — the env *was* dispatched; the bridge **may now be mutated**. A
   fail-closed rollback to baseline is correct, and an unverifiable rollback is a genuine `rollback_failed`.
2. **`apply_ok=False`** — the env was **never dispatched**. The real adapter returns `(False, None,
   "host_executor_unavailable")` *before* contacting the host — e.g. on the **host-gated dormant path** where the
   signed executor is not provisioned. The bridge is **byte-unchanged**; nothing was mutated.

In case (2) the code nonetheless dispatched a second (also-doomed) baseline "rollback" and, because that dispatch
likewise failed on the dormant host, reported `rollback_reason="rollback_failed"`. That is a **fabricated failure**: it
asserts an undo was attempted and failed — implying the tenant bridge was left in a dirty/mutated state needing
operator intervention — for a reconcile that never changed anything. On the dormant path (the common case before host
provisioning) every dry-less `--commit` run of an allow-listed account emitted this false alarm, and a transient host
recovery between the two doomed calls could even have incremented the batch's `rolled_back` counter for an account that
was never applied.

The reconciler is monitoring-only and execution-OFF; this is a correctness/observability defect, not a safety hole
(no execution flag, order, or `MT5_ALLOW_LIVE` is involved, and `applied` was already correctly `False`). But it
violates the governance **evidence/no-fabrication** expectation: a failed or never-run step must be *explainable* and
must never surface as something it was not.

## Decision

Split the branch on `apply_ok` and make the adapter contract explicit:

- **`apply_ok=False` ⇒ never dispatched ⇒ no rollback.** Return truthfully `ok=False`, `applied=False`,
  `rolled_back=False`, `rollback_reason=None`, `reason="apply_not_dispatched:<reason>"`. No second host call is made;
  no rollback is claimed. The distinct `apply_not_dispatched:` reason prefix keeps the dormant-host outcome
  distinguishable from a true attestation failure in audit/observability.
- **`apply_ok=True`, attestation unverified ⇒ fail-closed rollback (unchanged).** The bridge may be mutated, so roll
  back to the captured byte-exact baseline; an unverifiable rollback remains a genuine `rollback_failed`.
- **Adapter contract (documented on `MonitoringReconcileOps.apply_off_env`):** `ok=False` MUST mean the env was never
  dispatched (bridge byte-unchanged); an adapter that has begun mutating the bridge must never return `ok=False`. The
  real `_RealMonitoringOps` already honours this (it returns `(False, None, …)` before the executor call).

The post-apply **read-back** failure branch is deliberately **unchanged**: it is reached only after `apply_ok=True`
*and* a verified attestation, so the bridge really is mutated and its rollback — and any `rollback_failed` — is
genuine.

## Consequences

- The host-gated dormant path no longer emits a false `rollback_failed`; the dormant outcome is a truthful refusal
  (`apply_not_dispatched:host_executor_unavailable`), counted as `refused`, never `applied`, never `rolled_back`.
- The batch `summary` accounting is now internally consistent in all mixed cases: `ok + refused == total`, `applied`
  counts only true applies, and `rolled_back` counts only genuinely-restored accounts — a never-dispatched account can
  no longer be mis-counted as rolled back.
- A genuine rollback failure (dispatched-then-unverified, or enable-verified-then-read-back-failed with a failed undo)
  is preserved and still surfaced, so the fix hides no real failure.
- Repository-only, migration-free: no lease, image, deploy, flag, schema or host contact. The real transport stays
  operator-run and is not exercised in CI.

## Adversarial review and known limitations

Multi-lens adversarial review (fabricated-failure/hidden-success, rollback correctness, accounting consistency,
tenant isolation, fail-closed/transaction-boundary) run against the change. The defect was reproduced by a
deterministic failing test before the fix (dormant path reported `rollback_failed`) and is green after. Residual
notes:

- **Real-ops coverage.** `_RealMonitoringOps` is operator-run and not exercised in CI; the suite drives an injected
  fake that models the `apply_ok=False` (never-dispatched) and `apply_ok=True`-bad-attestation contracts faithfully.
  The adapter contract is now documented so a future real adapter cannot silently return `ok=False` after mutating.
- **Scope.** The sibling `execution.monitoring_reconcile` (read-only route reconciliation, ADR-0075) has no
  apply/rollback accounting and is out of scope. No gate, render, identity-pin, or execution-OFF invariant of
  ADR-0111 is touched.

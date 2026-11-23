# ADR-0123 — One continuous governed deploy exclusion across a coordinated backend + frontend release

- **Status:** Accepted (repository-only; additive new tooling; migration-free; no production deploy, no flag, no
  schema/DB change, no broker/MT5/host contact, no lease acquired by this packet).
- **Packet:** GFX-DASHBOARD-COORDINATED-DEPLOY-LEASE-167 (Infrastructure & Operations / deploy safety).
- **Date:** 2026-11-23.
- **Relates to:** packet-127 `hosted_workspace.recovery_lease` (the governed single operational lease),
  packet-128 `scripts/guarded_deploy.py` (the backend-only lease-guarded deploy) and
  `scripts/deploy_recovery_gate.py` (the Django-free gate), and
  `docs/operations/hosted-workspace/RECOVERY_OPERATING_MODEL.md`.

## Context

The authorized GFX-165 consolidated Dashboard currency-integrity deploy (backend `analytics/portfolio.py` +
frontend `dashboard/page.tsx` + the co-shipped staff-only `mailbox-health` FE) is a **coordinated two-service**
release: one backend image and one frontend image, both built from the same target SHA. The Sponsor required a
single standing invariant for it — *"hold one governed deploy exclusion across the coordinated release"* — and
instructed that if the existing tooling could not retain that exclusion across both phases, the deploy must **stop
before mutation** rather than improvise.

It could not, and it stopped. The precise reasons:

1. **`guarded_deploy.py` is backend-only and self-releasing.** `run_guarded_deploy` acquires the `KIND_DEPLOY` lease
   at the start of its run and **releases it in its own `finally`** (on clean success or successful rollback). It
   exposes no way to accept an externally-held lease, skip the release, or hold the lease open for a later phase.
2. **The lease is a strict single-lease mutual-exclusion model.** `recovery_lease.acquire` refuses while **any**
   ACTIVE lease exists — *including a same-kind deploy lease* (`blocks()`: "ACTIVE lease of the SAME kind ⇒ blocked
   too"). So `guarded_deploy` cannot be nested inside an outer, pre-held deploy lease — its own `acquire` is refused.
   The only bypass, `--emergency-override`, is scoped to a **live recovery** lease and even then proceeds *without* a
   deploy lease.
3. **No frontend-side or coordinated two-service lease-aware deploy wrapper existed.** The frontend build +
   `--force-recreate guvfx-frontend` was unguarded by the deploy lease.

Consequently the only ways to run both phases were to split the release into two lease windows (a gap between them),
leave the frontend unguarded, or abuse the override — all of which the Sponsor forbade as improvising.

The lease exists specifically to prevent a backend deploy from racing a recovery/acceptance window and resetting
`beta.env` mid-flight (packet-126/127). A coordinated release that recreates the backend has exactly that hazard, so
the exclusion must be held **continuously** — including across the frontend phase, so that no recovery/acceptance or
second deploy can interleave at the handoff between the two services.

## Decision

Add one reviewed, repository-only entry point, **`scripts/guarded_coordinated_deploy.py`**, that holds **exactly one**
`KIND_DEPLOY` lease continuously across both services. It reuses the packet-127 lease primitives unchanged
(`recovery_lease.acquire`/`release`/`blocks`); it does **not** modify `guarded_deploy.py` or the lease module, so the
existing backend-only path and its guarantees are untouched.

Contract (enforced by the wrapper and its tests):

1. **One lease, acquired before the first mutating operation, held continuously** through: both rollback tags →
   backend build → frontend build → backend recreate → frontend recreate → backend verify → frontend verify →
   combined accept/rollback decision → release. There is **no emergency override** and **no nested same-kind bypass**
   (contract §11): a live recovery, a live same-kind deploy, or a malformed lease **refuses before any mutation**.
2. **Explicit required lease path** `/srv/guvfx/mt5_handoff/recovery_operational_lease.json` (fail-closed if empty —
   never a temp fallback), the same single source of truth the backend gate and in-container `recovery_ops` share.
3. **Exact-SHA deploy, independent of the wrapper's own repository revision.** Preflight fetches, checks out the exact
   target SHA, asserts `HEAD == sha`, and asserts a clean (trusted) working tree.
4. **Backend contract:** build the exact target; recreate **only** `guvfx-backend` with `--no-deps --force-recreate`;
   verify `GUVFX_GIT_COMMIT == sha` **and** the `org.opencontainers.image.revision` label `== sha` **and**
   `/api/health/` returns a real 200. Migration state is only ever *checked* (read-only), never applied, by this
   wrapper.
5. **Frontend contract:** build the exact target; **require** `NEXT_PUBLIC_API_BASE_URL` (fail-closed if empty); pass
   **no** `NEXT_PUBLIC_*_ENABLED` capability flag (DARK, reproducible); verify the served `/build-info.json`
   `gitCommit == sha` **and** the `org.guvfx.git_commit` label `== sha` **and** both capability flags are false
   **and** the public frontend returns 200; recreate **only** `guvfx-frontend` with `--no-deps --force-recreate`.
6. **Never `--remove-orphans`** and never any other service — structurally impossible (the pure command builders refuse
   any non-target service and never emit the flag; asserted by tests).
7. **Rollback-first, combined.** Capture and tag both current images **before** any build/recreate. A failure at any
   step (build, recreate, verify, an unexpected service-inventory change, an exception, or an interrupt) restores
   **both** services to the captured baseline and re-verifies **both** identities + health.
8. **Lease release discipline.** Release the single lease **only** after a clean success **or** a fully-verified
   combined rollback. If rollback cannot restore a healthy known baseline, **retain** the lease fail-closed (it keeps
   blocking further deploys and recovery and self-expires via TTL) and emit a sanitized operator alert with exact
   recovery steps.
9. **`--dry-run`** resolves and prints the complete plan (target, lease path, service names, build arguments, health
   checks, rollback-tag scheme, contexts) with **no** mutation and **no** lease acquisition.
10. **No secrets** in argv, logs, result, audit, or alerts (the wrapper reads only the single non-secret
    `GUVFX_GIT_COMMIT` env var and the non-secret `/build-info.json`; lease free-text is secret-guarded by the lease
    module).

The production I/O is an injected `CoordinatedDeployOps` abstraction; the real host implementation shells to git +
docker and is **not** exercised in CI. The whole orchestration and all structural guarantees are proven with a fake
ops under a file-only lease (`SimpleTestCase`, no Django/DB), so a backend-down host cannot bypass the gate.

## Consequences

- A coordinated backend + frontend release can now be run under **one continuous** governed deploy exclusion, closing
  the GFX-165 gap without improvising. The GFX-165 deploy remains Sponsor-gated; this packet only builds the tooling.
- The existing backend-only `guarded_deploy.py` is unchanged and remains the right tool for a backend-only deploy. The
  two share the one lease file, so a coordinated deploy and a backend-only deploy are mutually exclusive, as intended.
- This wrapper deliberately does **not** add a lease "hold/defer-release" handshake to `guarded_deploy.py`; a single
  self-contained coordinated entry point is a smaller, better-tested surface than threading an external lease through
  the backend-only path.
- Repository-only: no lease is acquired, no image is built or tagged, nothing is deployed or recreated by this packet.
  The real host path is operator-run, under a separate Sponsor deploy GO.

## Adversarial review and known limitations

A two-round multi-lens adversarial review (lease lifecycle, rollback completeness, fail-closed/refusal, structural
contract, secret-safety/test-integrity) was run against the wrapper and its tests. Round one surfaced a **critical**
defect — the post-rollback check reused the target-SHA deploy oracle, so a genuine rollback (which restores the prior
image) would have been misclassified `INDETERMINATE` and leaked the lease — plus a lease-lifecycle HIGH (a pre-mutation
error wrongly retaining the lease) and several MEDIUMs. All were fixed:

- Rollback verification uses dedicated `verify_restored_backend`/`verify_restored_frontend` that confirm the running
  container is the **captured baseline image id** (the prior commit, never the target SHA) plus health — so a correct
  rollback reaches `ROLLED_BACK` and releases the lease.
- The orchestrator releases the single lease whenever `not mutated` (gate refuse, preflight fail-closed, or a
  pre-mutation error), since production is then provably untouched.
- The two restores are attempted independently; every preflight probe fails closed as `PreflightError`; the frontend
  verify polls readiness before reading the served identity; the inventory guard is scoped to the managed compose
  project so unrelated transient `docker run --rm` jobs cannot false-positive it.

Round two confirmed the fixes (rollback-correctness and lease/mutation-semantics lenses: *fix confirmed, no new
defect*). Two residual **low-severity** items are recorded here per the evidence rule rather than changed in this
packet:

- **Rollback scope.** `ROLLED_BACK` attests that the two **target** services were restored to their healthy baseline.
  If a rollback were triggered by an unrelated managed-service inventory disruption (not a backend/frontend health
  failure), restoring backend+frontend does not resolve that third service; it is surfaced by the operator alert. The
  coordinated deploy uses `--no-deps` and never `--remove-orphans`, so it structurally cannot itself disrupt another
  service, making this near-unreachable. Coupling the lease to unrelated service health was deliberately avoided so an
  unrelated incident cannot block all deploys.
- **Real-ops coverage.** The real host `_RealCoordinatedDeployOps` is operator-run and not exercised in CI (the suite
  drives an injected fake). Preflight's `PreflightError` wrapping has a thin real-ops regression test; the frontend
  poll-before-build-info ordering is review-verified only. The structural guarantees (single-service recreate,
  `--no-deps --force-recreate`, no `--remove-orphans`, no capability flag, required API base, exact-SHA pinning) are
  proven by direct unit tests of the pure command builders.

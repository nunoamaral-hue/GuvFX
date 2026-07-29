# ADR-0021 — Production Deploy + Rollback Plan (Sponsor-gated; DO NOT execute without approval)

The permanent dedicated-runtime onboarding = **PR A (#240, merged)** + **PR B (#241)**. This plan deploys
the two **as one unit**. It is prepared for the Sponsor's deployment gate. **Nothing here is executed
until the Sponsor approves.** No order is placed; Customer Zero is not resumed by this plan.

Prod: OVH Linux VPS `ubuntu@100.119.23.29` (`/home/ubuntu/guvfx-prod`, docker-compose). Deploys are
**manual** — there is no CD; merging to `main` does not deploy.

## Pre-conditions (all must hold before deploy)
1. PR A + PR B both merged to `main`; CI green on the merge commit.
2. `make check` green; adversarial reviews closed.
3. `PROVISIONING_REQUIRE_BROKER_LOGIN` stays **OFF** at deploy time (broker-INDEPENDENT). It is flipped ON
   only **after** isolated disposable-demo host certification (a Sponsor-supplied demo account) passes —
   see `docs/ADR-0021-PRB-EVIDENCE.md`.
4. **Golden-Reference STOP-check — BEFORE.** Capture the baseline (routing ti_signals→asn8→acct1 +
   wayond→asn7→acct1, one arm/source, ExecutionControl kill/DEMO/auto state, both AUTO_DEMO levers, 0 open
   order-jobs, 0 open positions, last_job id, credential decrypt parity). Abort on any drift.
5. **Backups armed:** a fresh verified `pg_dump` (0-error test-restore + row-parity) and the current prod
   backend image tagged `rollback-preADR0021` (record the digest). Frontend prod copy tagged likewise.

## Migrations applied on deploy (additive, reversible, prod audited clean)
- `trading 0013` — CheckConstraint `brokeridentity_present` (+ RunPython abort pre-check).
- `terminal_provisioning 0009` — `ProvisionerHeartbeat` model.
- `terminal_provisioning 0010` — partial-unique `uniq_active_job_per_runtime_op` (+ RunPython abort pre-check).
Deterministic + reversible (scratch-DB round-trip proven). **MIGRATE-FIRST**: run via
`docker compose run --rm --no-deps <backend> python manage.py migrate` (the RunPython pre-checks abort
loudly on any incompatible row; prod preflight found 0), then recreate the web container.

## Deploy steps (manual, Sponsor-approved)
1. Snapshot + backup (pre-condition 5); Golden STOP-check BEFORE (pre-condition 4).
2. Build the new backend image from `main`; record the digest.
3. Apply migrations MIGRATE-FIRST (above); confirm `migrate --check` clean and row counts unchanged.
4. Recreate ONLY `guvfx-backend` (web). Leave `PROVISIONING_REQUIRE_BROKER_LOGIN` unset/OFF. Keep the
   existing beta flags as they are (this deploy changes onboarding CODE, not flag posture).
5. **Frontend reconciliation** — the deployed prod frontend (`/home/ubuntu/guvfx-prod/frontend`) has
   diverged from the repo. Apply the repo's `AccountConnectionStep.tsx` (state-driven) to the deployed
   copy, rebuild the frontend image, recreate; verify `guvfx.com` 200. (Repo is canonical; this is the
   divergence reconciliation flagged in the ADR.)
6. **Rebuild + recreate `guvfx-wayond-listener`** from the new backend image — the listener runs a
   SEPARATE image (backend+telethon) and hosts the auto_router; a stale listener would run old code
   (known trap). Verify in-listener routing unchanged.
7. **Golden-Reference STOP-check — AFTER.** Re-capture the baseline; assert byte-identical to BEFORE
   (routing / assignments / execution controls / runtime / 0 open positions/orders / Nuno path). Abort +
   rollback on ANY drift.
8. Smoke: health 200 (guvfx.com / api / bridge 401); onboarding create → dedicated runtime path; no order.

## Broker-login enablement (SEPARATE, later Sponsor gate — not part of this deploy)
Only after isolated disposable-demo **host certification** passes: set
`PROVISIONING_REQUIRE_BROKER_LOGIN=1`, ensure the customer account has a normalised `broker_server` (a
free-text broker_name cannot be validated), recreate backend + beta-provisioner, re-run the Golden
STOP-check. Never in the same window as the code deploy.

## Rollback
- **Code:** recreate `guvfx-backend`, `guvfx-wayond-listener`, frontend from the `rollback-preADR0021`
  image digests. (Deploys are image-swaps; no data change.)
- **Migrations (only if required):** `migrate terminal_provisioning 0008` then `migrate trading 0012` —
  each new constraint's reverse simply drops the constraint (proven reversible; no data mutation). The
  `ProvisionerHeartbeat` table is dropped on reverse; it holds only transient liveness, no customer data.
- **Data:** restore the pre-deploy `pg_dump` only as a last resort (the migrations are additive + reversible,
  so a code+migration rollback is normally sufficient with zero data loss).
- **Trigger:** any Golden STOP-check drift, any unexpected order/position, any customer runtime reaching
  RUNNING without the expected state, or health failure.

## Estate safety (invariant across deploy)
Nuno's PRODUCTION runtime / account #1 / `guvfx_u_1` / Golden assignments are `cohort=BETA`-excluded and
staff-bypassed throughout; the beta slots are the only customer execution surface. The Golden STOP-check
before + after is the gate.

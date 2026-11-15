# Recovery operating model — governed exclusive-window lease + bounded acceptance (packet-127)

- **Packet:** GFX-DASHBOARD-RECOVERY-OPERATING-MODEL-127.
- **Builds on:** ADR-0107 (established-account auto-recovery) + its HARDENING-118 and OPERATING-MODEL-127 addenda,
  and the pkt-126 production acceptance (first positive #50 relaunch with the pkt-118 hardening).
- **Status:** repository-only, DARK, **default-OFF**, migration-free. Nothing here enables recovery in production.

## 1. The conservative bounded-cohort operating model (adopted, not a Sponsor A/B/C/D question)

- recovery is **default-OFF globally**; production activation is **allow-list-only**;
- the initial steady-state cohort is **small** (`STEADY_STATE_COHORT_MAX = 1`) — established MONITORING-ONLY accounts;
- the host breaker is pinned **max-down = 1, max-relaunches = 1** for the initial cohort;
- `is_active` stays an **execution** gate and is **never** changed by recovery;
- **Copy-Trading participants are excluded fail-closed**; onboarding accounts are excluded;
- the stale threshold may be lowered to **300 s but never below** (the ~5-min collector cadence floor);
- recovery never logs in, trades, arms execution, or fabricates a snapshot.

An **approved steady-state configuration** is exactly this shape (enabled + allow-list ≤ 1 + breaker 1/1 + a clean
threshold ≥ 300 s). Any configuration that enables recovery *without* either an active governed lease **or** this
approved shape is treated as **leftover bounded-acceptance configuration** and the cron warns every cycle.

## 2. The governed exclusive-window lease (deploy ⊥ recovery)

A single mutually-exclusive **operational lease** coordinates deployment tooling and recovery/acceptance tooling.

| Property | Behaviour |
|---|---|
| storage | JSON state file on the handoff mount (`GUVFX_RECOVERY_LEASE_DIR`, default `BASE_DIR/.guvfx_handoff`) + a JSON-lines audit log beside it |
| kinds | `recovery_acceptance`, `deploy` — only **one** lease is live at a time |
| fields | owner, purpose, correlation-id, created-at, expires-at; **no secret** (secret-name guarded) |
| TTL | bounded (`60 s … 7200 s`); a lease **always self-expires** |
| EXPIRED | not blocking; safely **reclaimable** |
| MALFORMED | **fails closed** — blocks; never read as ABSENT |
| audit | acquire / release / reclaim / emergency-override appended, no secret |

**In-container CLI** (single source of truth): `python manage.py recovery_ops {lease-status|lease-acquire|
lease-release|lease-reclaim|check-deploy|check-arm|config}`.

**Deploy gate** (host, Django-free, works when the backend is down) — wire this into the deploy path **before**
recreating the backend:

```bash
python3 scripts/deploy_recovery_gate.py --lease-file "$GUVFX_RECOVERY_LEASE_FILE" \
  || { echo "deploy blocked by a live recovery lease"; exit 2; }
# emergency only, audited:
python3 scripts/deploy_recovery_gate.py --lease-file "$GUVFX_RECOVERY_LEASE_FILE" \
  --emergency-override "hotfix <ticket>, approved by <operator>"
```

Exit `0` = deploy permitted, `2` = blocked (live recovery/deploy lease, or malformed lease ⇒ fail-closed).

## 3. Bounded acceptance with GUARANTEED teardown

`hosted_workspace.recovery_acceptance.run_bounded_acceptance(ops, account_id, …)` acquires a recovery lease
(fail-closed vs a deploy lease), captures the byte-exact `beta.env` baseline, arms **only** that account
(allow-list-of-one, breaker 1/1), watches to PASS/FAIL/TIMEOUT, and in a **`finally`** block ALWAYS:

1. restores the **byte-exact** baseline; 2. recreates only the backend; 3. confirms both recovery flags read OFF;
4. releases the lease; 5. emits the teardown result.

A crash / timeout / exception therefore still tears down; a teardown that cannot complete raises an operator ALERT
and leaves the lease to self-expire — **recovery flags are never left silently enabled**. The host wrapper is
`scripts/recovery_acceptance_runner.py` (operator-run); the guarantee is unit-tested with a fake `HostOps` across
success / FAIL / timeout / exception in `tests_recovery_operating_model.py`.

## 4. Remaining production-activation step (separate, Sponsor-gated — NOT in this packet)

1. Wire the deploy tooling (the host `build_*.sh` / compose recreate path) to call `scripts/deploy_recovery_gate.py`
   first; verify it blocks while a recovery lease is live and that `--emergency-override` is audited.
2. For the first steady-state member (**#50**), set the approved steady-state configuration
   (`HOSTED_ESTABLISHED_RECOVERY_ACCOUNT_IDS=50`, breaker `HOST_MAX_DOWN=1` / `HOST_MAX_RELAUNCHES=1`, optional
   `HOSTED_ESTABLISHED_RECOVERY_SNAPSHOT_STALE_S` ≥ 300) and enable the flags — only under an acquired recovery lease,
   with the cron showing no `LEFTOVER`/`EMPTY allow-list` warning.
3. All of the above remains behind a separate Sponsor decision; this packet only *prepares* the model.

## 5. Prohibitions (standing)

No production recovery enablement in this packet; no terminal stop/relaunch; no `is_active` mutation; no Copy-Trading
change; no broker action; no fabricated snapshot; the lease carries no secret and the config metadata never dumps the
environment.

---

## 6. Deploy-gate wiring (packet-128) — the one shared guarded deploy wrapper

Every backend production deploy MUST go through `scripts/guarded_deploy.py` — the single shared, lease-guarded,
rollback-first wrapper. The ad-hoc host build/recreate scripts (`build_*.sh`, `deploy_*_recreate.sh`) are replaced
by (or must call) it. It:

1. **Acquires a governed DEPLOY lease before any mutation.** The lease module refuses while a **recovery** lease is
   live or MALFORMED — so a deploy cannot race an in-flight established-recovery/acceptance window. While the deploy
   lease is held, a recovery attempt fails closed (mutual exclusion).
2. Runs the existing **rollback-first** (tag the running backend image before `:latest` moves), **backend-only**
   (`up -d --no-deps --force-recreate guvfx-backend`, **never `--remove-orphans`**) build + recreate + verify.
3. **Releases the lease through guaranteed cleanup.** A clean success — or a successful rollback after a failed
   deploy — releases the lease. A deploy failure whose **rollback also fails** RETAINS the lease and ALERTS (never a
   false "clean" report); the lease then self-expires via its TTL. It needs only the stdlib `recovery_lease` file
   primitives, so a **backend/DB-down** deploy still enforces the gate.

```bash
export GUVFX_RECOVERY_LEASE_FILE=/srv/guvfx/mt5_handoff/recovery_operational_lease.json   # same mount the backend uses
python3 scripts/guarded_deploy.py --sha <target> --compose-dir /home/ubuntu/guvfx-prod \
  --app-dir /home/ubuntu/guvfx-app --owner <operator> --release-prefix <release> \
  --lease-file "$GUVFX_RECOVERY_LEASE_FILE" --health-url https://api.guvfx.com/api/health/
# emergency only (explicit, audited, redacted; the ONLY way past a LIVE recovery lease — never a malformed one):
python3 scripts/guarded_deploy.py ... --lease-file "$GUVFX_RECOVERY_LEASE_FILE" \
  --emergency-override "hotfix <ticket>, approved by <operator>"
```

The `--lease-file` MUST point at the **same lease file the in-container recovery writes** (the handoff mount); the
wrapper **refuses fail-closed** (exit 3) if no lease file is configured — it never falls back to a temp path. Exit
`0` = deployed + lease released; `3` = refused / not cleanly deployed (investigate; the lease may be retained). The
wrapper runs build+recreate only (never `manage.py migrate`) — migrations remain a separate, documented operator step
and `verify()`'s health 200 does not imply migration health.
`scripts/deploy_recovery_gate.py` remains the lightweight standalone pre-check.

## 7. #50 steady-state model — resolved from ADR-0107 + merged code (NOT assumption)

The merged pkt-127 code decides this: `liveness_recovery._is_approved_steady_state_config()` returns True only when
recovery is enabled **and `_live_recovery_enabled()` is False** (demo-only), allow-list ≤ 1, breaker 1/1, clean
threshold. #50 is a **LIVE** monitoring-only account (`is_demo=False`), so a LIVE recovery configuration is **never**
an unleased "approved steady-state" and always raises the LIVE-without-active-lease warning.

**Therefore the approved #50 steady-state model is B — all recovery flags OFF except during bounded governed
leases** (Option A, continuously-enabled DEMO/monitoring-only recovery with LIVE OFF, applies only to a *demo*
cohort; it does not fit the LIVE #50). #50 recovery runs only inside a bounded governed recovery lease (the pkt-126
acceptance shape), then returns to DARK. This is consistent with ADR-0107 (recovery default-OFF globally;
activation allow-list-only; LIVE requires an active governed lease). If a future operator sets a continuously-enabled
LIVE #50 config, the cron will warn every cycle — by design.

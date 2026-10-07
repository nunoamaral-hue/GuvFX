# First LIVE-Monitoring Certification — Runbook

**Status:** PREPARED, **NOT EXECUTED**. This is the exact procedure for the first production LIVE **monitoring**
certification. Running it is a **separate Sponsor-authorized step**. Nothing in this runbook is authorized to run
yet. **No real-money order is authorized at any point.**

**Goal:** prove that a real LIVE broker account can be provisioned → logged in by a human → exactly identity/
environment matched → reach **CONNECTED / MONITORING** with balances / equity / open positions / manual-trade
activity visible in the dashboard & analytics, **while automated execution remains provably impossible.**

**Target config (Sponsor-stated):** LIVE monitoring **ON** · LIVE recovery **OFF** · LIVE execution **OFF** ·
self-connect **OFF**.

---

## 0. Why D4e is load-bearing for this cert (read first)

Prod backend already has `HOSTED_LIVENESS_RECOVERY_ENABLED=1` and `HOSTED_SESSION_RECONCILER_ENABLED=1` (the
master demo recovery gates, long-on). **Before D4e**, LIVE infrastructure recovery was widened by the *monitoring*
flag — so turning on `HOSTED_LIVE_MONITORING_ENABLED` for this cert would have **simultaneously armed LIVE terminal
relaunch + session restore** (master gates already on). **D4e** gives LIVE recovery its own
`HOSTED_LIVE_RECOVERY_ENABLED` gate (default OFF, decoupled), which is what makes "monitoring ON, recovery OFF"
actually achievable. Keep `HOSTED_LIVE_RECOVERY_ENABLED` **absent/unset** throughout.

> ⚠ **Name-collision caution (D4e review LOW):** `HOSTED_LIVE_RECOVERY_ENABLED` (the D4e LIVE widen, must stay OFF)
> is one letter-cluster away from `HOSTED_LIVENESS_RECOVERY_ENABLED` (the demo master, already `=1` — leave it).
> Do not touch the latter. Only ever ADD `HOSTED_LIVE_MONITORING_ENABLED=1` for this cert.

---

## 1. Preconditions

- [ ] D1–D4e merged + DARK-deployed (backend image carries D4e; `hosted_live_recovery_enabled` exists).
- [ ] Current prod flags verified (expected): `HOSTED_LIVE_MONITORING_ENABLED` absent, `HOSTED_LIVE_RECOVERY_ENABLED`
      absent, `HOSTED_LIVE_EXECUTION_ENABLED` absent, `HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED=0`.
- [ ] `LiveExecutionAuthorization` rows in prod = **0** (no account is execution-authorized).
- [ ] A **fresh Model-A LIVE account** chosen — a brand-new lifecycle instance, **NOT Account 43** (quarantined),
      not a re-add of a tombstone. Prefer a real MT5 account with **zero / negligible balance**.
- [ ] Capacity + estate unaffected: 25/35/36 untouched; Account 33 quarantined; Cold-Boot V2 paused; entitlement 20.
- [ ] Sponsor has explicitly authorized THIS cert run.

Pre-flight verification (read-only, safe to run before authorization):
```bash
ssh ubuntu@100.119.23.29 'docker exec guvfx-backend printenv | grep -E "HOSTED_LIVE_|HOSTED_SESSION_RECONCILER_ARM" | sort'
ssh ubuntu@100.119.23.29 'docker exec guvfx-backend python manage.py shell -c "from execution.models import LiveExecutionAuthorization as L; print(\"authz_rows\", L.objects.count())"'
```

---

## 2. Exact target flag configuration

Backend flags live in **`/home/ubuntu/guvfx-prod/beta.env`** (loaded by `guvfx-backend`).

| Flag | Target | Action |
|------|--------|--------|
| `HOSTED_PERSISTENT_MT5_ENABLED` | `1` | already on — leave |
| `HOSTED_LIVE_MONITORING_ENABLED` | `1` | **ADD** (the only change this cert makes) |
| `HOSTED_LIVE_RECOVERY_ENABLED` | **unset** (OFF) | **do not add** |
| `HOSTED_LIVE_EXECUTION_ENABLED` | **unset** (OFF) | **do not add** |
| `HOSTED_SESSION_RECONCILER_ARM_SELFCONNECT_ENABLED` | `0` | already 0 — leave |
| `HOSTED_LIVENESS_RECOVERY_ENABLED` | `1` | already on (demo master) — **do not touch** |
| `HOSTED_MT5_EXECUTION_ENABLED` | `1` | already on — leave (this is the general exec-subsystem gate; LIVE is still blocked by condition-11 because `HOSTED_LIVE_EXECUTION_ENABLED` is OFF + no §3 authz) |

Apply (only when authorized):
```bash
# On the VPS, add exactly one line to beta.env, then recreate ONLY the backend.
ssh ubuntu@100.119.23.29
cd /home/ubuntu/guvfx-prod
cp beta.env beta.env.preLIVEMON            # rollback copy
printf 'HOSTED_LIVE_MONITORING_ENABLED=1\n' >> beta.env
docker compose up -d --force-recreate --no-deps guvfx-backend     # NEVER --remove-orphans
docker exec guvfx-backend printenv | grep HOSTED_LIVE_MONITORING_ENABLED   # expect =1
```

---

## 3. Procedure

1. **Create the fresh LIVE instance.** Via the owner's Add-Account (D2) with `account_type=live` (→ `is_demo=False`).
   This creates a new `TradingAccount` lifecycle instance + hosted workspace in `PROVISIONING`/`WAITING_FOR_LOGIN`.
   Confirm it is a new pk (not a revive) and not Account 43.
2. **Provisioning completes** (per-tenant slot, `/portable` terminal, observer). No login yet.
3. **Human MT5 login** (the human step). A human opens the account's MT5 terminal via the Terminal Access /
   Guacamole viewer and logs in **once** with the **real broker credentials** (operator/owner-entered in the MT5
   GUI — GuvFX does not auto-enter real-money credentials). Thereafter the observer attaches read-only.
4. **Exact identity + environment match.** The observer's `ExpectedAccount` now carries the account's ACTUAL
   environment (D3) and the matcher requires the observed terminal's demo/live class to **equal** LIVE; a wrong
   login or demo/live mismatch stays **fail-closed** (`proj_account_match` never becomes True). Verify match.
5. **Customer confirm.** The owner explicitly confirms the discovered account is theirs (stamps
   `workspace_confirmed_at`). This narrows eligibility and is required for MONITORING.
6. **CONNECTED / MONITORING reached.** `evaluate_monitoring(account).eligible == True` (reason `RW_MONITORING_OK`):
   connected + matched + confirmed + fresh. The derived `CONNECTED_MONITORING_EXEC_UNAUTHORIZED` display state is
   reachable.
7. **Verify the monitoring surfaces** (the cert evidence): balances / equity / open positions visible; any manual
   trade the human places in the terminal appears in the account's trade history / dashboard / analytics; the
   portfolio dashboard and analytics render the LIVE account correctly.

---

## 4. PROVE automated execution remains impossible (mandatory evidence)

Run inside the backend container (read-only). Expected results noted:
```bash
docker exec guvfx-backend python manage.py shell -c "
from trading.models import TradingAccount
from execution.readiness import PersistentWorkspaceProvider
from execution.live_authz import is_live_execution_authorized, live_execution_permitted
from hosted_workspace.flags import hosted_live_execution_enabled, hosted_live_recovery_enabled, hosted_live_monitoring_enabled
from execution.models import LiveExecutionAuthorization
a = TradingAccount.objects.get(pk=<LIVE_PK>)
p = PersistentWorkspaceProvider()
print('monitoring_eligible', p.evaluate_monitoring(a).eligible)          # expect True
d = p.evaluate(a)
print('execution_eligible', d.eligible, d.reason)                        # expect False  RW_REAL_ACCOUNT_NOT_ENABLED
print('live_exec_flag', hosted_live_execution_enabled())                 # expect False
print('live_recovery_flag', hosted_live_recovery_enabled())              # expect False
print('monitoring_flag', hosted_live_monitoring_enabled())               # expect True
print('authorized', is_live_execution_authorized(a))                     # expect False
print('permitted', live_execution_permitted(a))                          # expect False
print('authz_rows', LiveExecutionAuthorization.objects.filter(trading_account=a).count())  # expect 0
"
```
Additional impossibility layers to record:
- **Router:** the account is not a routable execution target (auto_router excludes non-demo without the exec flag + authz).
- **Bridge:** `MT5_ALLOW_LIVE` is **not** set for its runtime (`bridge_config.allow_live_for_account(a)` is False) ⇒
  the standalone bridge refuses any live order independently.
- **Recovery:** with `HOSTED_LIVE_RECOVERY_ENABLED` OFF, the account is **not** a liveness/session recovery
  candidate (demo-only); confirm it never appears in a recovery pass.

Capture all of the above into an evidence manifest (per `evidence/schema/`): exact commands, actual output,
`PASS` only where the criterion actually ran, and an explicit list of what was not covered.

---

## 5. Abort / rollback

```bash
ssh ubuntu@100.119.23.29
cd /home/ubuntu/guvfx-prod
cp beta.env.preLIVEMON beta.env            # restore (removes HOSTED_LIVE_MONITORING_ENABLED)
docker compose up -d --force-recreate --no-deps guvfx-backend     # NEVER --remove-orphans
```
Then, if required, tombstone the cert account via the owner Remove flow (Model-A: row retained, credentials
destroyed, a re-add would be a new instance). Removing monitoring returns a LIVE account to categorically inert.

---

## 6. Explicitly NOT authorized by this runbook

- Enabling `HOSTED_LIVE_EXECUTION_ENABLED`, `HOSTED_LIVE_RECOVERY_ENABLED`, or self-connect.
- Running the §3 LIVE authorization ceremony for real execution, or creating any `LiveExecutionAuthorization`.
- Granting a production runtime `MT5_ALLOW_LIVE`.
- Placing, modifying, or closing any real-money order through GuvFX automation.

Each of the above is a **separate Sponsor decision** beyond this monitoring-only certification.

---

## 7. Evidence checklist

- [ ] Flag state before/after (printenv) captured.
- [ ] Account is a fresh Model-A LIVE instance (new pk), not Account 43; balance zero/negligible.
- [ ] Human-login → exact LIVE identity + environment match (screenshot/observation record).
- [ ] `evaluate_monitoring.eligible == True`; CONNECTED/MONITORING reached.
- [ ] Balances / equity / positions / manual-trade visible in dashboard + analytics.
- [ ] Execution-impossibility block (§4) output captured: `execution_eligible == False / RW_REAL_ACCOUNT_NOT_ENABLED`,
      `authz_rows == 0`, flags as expected, bridge `MT5_ALLOW_LIVE` unset, not a recovery candidate.
- [ ] No real-money order placed. Estate 25/35/36 untouched; 33 quarantined; entitlement 20; Cold-Boot V2 paused.

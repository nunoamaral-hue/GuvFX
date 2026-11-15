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

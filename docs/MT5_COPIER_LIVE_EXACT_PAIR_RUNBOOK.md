# MT5 Copier — LIVE exact-pair acceptance runbook (ADR-0057)

**Status: PREPARED, NOT EXECUTED.** This runbook is the deployment + one-order acceptance procedure for the
governed LIVE copy capability. Nothing in it has been run. Every step is Sponsor/operator-gated. The code
ships DARK (all flags OFF, no authorization rows, no host change). Do not execute any step without explicit
Sponsor approval for that step.

Target pair (the ONLY pair this acceptance covers): master **#46 TradersWay** → follower **#48 IS6FX**,
`XAUUSD → XAUUSD.std` (verified), follower volume **0.01**, max concurrent **1**, **one** future OPEN.

## Hard stops (never without explicit Sponsor approval of that exact step)

Deploy · run the migration · set any flag ON · create a `CopierLiveAuthorization` · change `is_active` ·
re-provision/restart a bridge or runtime · place/close/modify any order. Each is a separate approval.

## Pre-flight (read-only; safe)

1. Confirm prod source == this PR merged, migration `trade_copier/0008` **planned** (not yet applied):
   `docker exec guvfx-backend python manage.py showmigrations trade_copier | tail`.
2. Confirm all flags OFF and zero copier orders (the P0 baseline probe), and `BrokerRuntimePause`/kill switch
   empty. Confirm #46/#48 identities and the verified `CopierSymbolMap` unchanged.

## Phase 1 — DARK deploy (migration-first)

1. Apply the migration only: `docker exec guvfx-backend python manage.py migrate trade_copier 0008`.
   (Two new tables; no data change; no behaviour change — all flags still OFF.)
2. Deploy the backend image carrying this PR per `reference_prod_deploy_mechanism` (image-based,
   `--build-arg GIT_COMMIT`, `up -d --no-deps --force-recreate`, never `--remove-orphans`). Verify
   `build-info.json` parity.
3. Verify still dark: `execution_status()["execution"] == "off"`, `copier_live_execute_enabled()` False.

## Phase 2 — Enable the follower runtime LIVE boundary (host)

Rollback prepared FIRST: note the current follower bridge env + `is_active`. Then, Sponsor-gated:

4. Set follower **#48 `is_active=True`** (currently False) — required by admission + the account-stopped gate.
5. Turn the DARK global gate on: `HOSTED_LIVE_EXECUTION_ENABLED=1` (backend env). This alone authorizes
   nothing (no authorization exists yet).
6. Deploy the LIVE-authorized bridge env to **#48 only** via the ADR-0049 signed integrator (this replaces the
   previously-unsupported manual `.bat` edit). The command must run with the arm stack present (so
   `allow_live_for_account(#48)` resolves True and `render_bridge_env` emits `MT5_ALLOW_LIVE=1`), AFTER the grant
   in Phase 3. Dry-run first, then `--commit`:
   ```bash
   cd /home/ubuntu/guvfx-prod && docker compose exec -T -e COPIER_ENABLED=1 -e COPIER_EXECUTE_ENABLED=1 -e COPIER_LIVE_EXECUTE_ENABLED=1 -e HOSTED_LIVE_EXECUTION_ENABLED=1 guvfx-backend \
     python manage.py provision_authorized_live_copier_bridge --account-id 48 --authorization-id <ID>            # dry-run preview
   # then re-run with --commit appended to dispatch the signed ACTIVATE_TENANT_BRIDGE_LIVE_AUTHORIZED
   ```
   It signs the account-scoped operation, the host writes the backend-rendered env atomically, restarts ONLY
   `GuvFX_TenantBridge_48`, and returns a signed attestation (env checksum, `MT5_ALLOW_LIVE=1`, identity pin +
   guarded attach, listening). The command verifies the attestation and fails closed to an OFF restore on any
   mismatch. No other tenant's bridge is touched.

## Phase 3 — Human authorization ceremony (the grant)

7. A human operator runs (staff `--approved-by`), bounded to one order:
   ```
   docker exec guvfx-backend python manage.py grant_copier_live_authorization \
     --user-email <owner> --master-account-id 46 --follower-account-id 48 \
     --source-symbol XAUUSD --follower-symbol XAUUSD.std \
     --max-volume 0.01 --max-concurrent 1 --max-orders 1 --valid-minutes 120 \
     --approved-by <staff-email> --reason "ADR-0057 one-order LIVE acceptance #46->#48"
   ```
   It validates every static precondition and records the identity + watermark snapshots. Expected: one
   `ACTIVE` authorization id, bound facts echoed (login masked). It arms nothing.

## Phase 4 — Arm + one future OPEN

Rollback prepared: the disarm commands below are staged in a second terminal.

8. Arm the stack (backend env): `COPIER_ENABLED=1 COPIER_EXECUTE_ENABLED=1 COPIER_LIVE_EXECUTE_ENABLED=1`
   (replication/observer already run via cron). UI now reads `LIVE COPIER ARMED — AUTHORIZED PAIR ONLY`.
9. The operator opens **one** protected (complete SL+TP) XAUUSD position on master #46. Future-event-only: the
   watermark rejects anything at/before the grant, and the currently-open position cannot replay.

## Acceptance — prove four separate stages

- **A** master order observed → a new `MasterEvent OPEN` for #46 after the watermark.
- **B** replication intent created exactly once → one `CopierReplication` `SHADOW_RECORDED → EXECUTION_REQUESTED`.
- **C** execution job claimed + completed exactly once → one `ExecutionJob PLACE_ORDER` (`is_demo=false`,
  `execution_mode=LIVE`), claimed by the signal bridge, `SUCCESS`; `orders_consumed=1`, authorization `CONSUMED`.
- **D** matching #48 follower order → `XAUUSD.std` volume `0.01`, comment `CPY<replication id>`, auditable link
  MasterEvent → CopierReplication → ExecutionJob → idempotency key `copier-live:<pk>` → follower ticket.

Capture pre/post counters and the bridge log line for each stage.

## Return to OFF (after evidence capture)

10. Disarm: `COPIER_LIVE_EXECUTE_ENABLED=0` (then `COPIER_EXECUTE_ENABLED=0`). The one-order allowance is
    already spent (authorization `CONSUMED`), so no further order can be produced regardless.
11. Restore #48's bridge to execution-OFF via the ADR-0049 integrator (risk-reducing; needs no arm flags and no
    valid authorization), then clear the global gate + restore `is_active`:
    ```bash
    docker exec guvfx-backend python manage.py restore_copier_bridge_execution_off --account-id 48 --commit
    ```
    This re-renders #48's env WITHOUT `MT5_ALLOW_LIVE` and restarts ONLY `GuvFX_TenantBridge_48`, attesting OFF.
    Then set `HOSTED_LIVE_EXECUTION_ENABLED=0` and restore `#48 is_active=False` if continued LIVE operation was
    not separately authorized.
12. **The copied LIVE position is NOT auto-managed** — the operator manually manages/closes it on #48 (LIVE
    close/modify/partial-close are fail-closed in this increment; see `docs/KNOWN_ISSUES.md`).

## Emergency disarm (any time)

`docker exec guvfx-backend python manage.py copier_live_disarm --by <email> --reason "<why>"` engages a
durable kill switch that blocks ALL LIVE copier dispatch immediately, surviving a flag flip/restart, audit
preserved. Flipping `COPIER_LIVE_EXECUTE_ENABLED=0` is the second, independent disarm.

## Rollback

Revert the PR to return to the DEMO-only architecture; `migrate trade_copier 0007` drops the two tables.
At runtime, disarm (above) + revoke the authorization. No order is reversible by software — a placed LIVE
order is closed manually by the operator on the follower terminal.

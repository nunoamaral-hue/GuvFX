# T1 — Production DARK Deployment Runbook (PREPARED — execution NOT authorized)

Packet `T1_PRODUCTION_DARK_DEPLOYMENT_READINESS`. This runbook is **prepared for a future, separately-authorised
operator**. It is **not** an authorization to deploy. T1 merged to `main` at `48db801`; prod has NOT been deployed.

> **Mixed-version finding (decisive).** The new columns `AcquiredMessage.non_genuine` and
> `PendingSignalApproval.chat_id` are **NOT NULL with no DB default** (verified by introspection). Therefore
> **OLD code + NEW schema is UNSAFE** (an old writer's INSERT omits those columns → NOT NULL violation) and
> **NEW code + OLD schema is UNSAFE** (undefined column). This is **not** a zero-downtime rolling deploy: the
> acquisition writer must be stopped across the migrate→recreate step. The gap is covered losslessly by
> watermark catch-up (see §E) — no channel switch, no watermark reset.

## A. Preconditions (all must hold before starting)
1. `main` HEAD = `48db801` (contains the approved tree equal to reviewed `e29e194`); host `/home/ubuntu/guvfx-app` to be fast-forwarded to it.
2. `signal_intake` applied migrations = **0009** (0010/0011 pending). Confirm: `docker exec guvfx-backend python manage.py showmigrations signal_intake`.
3. Providers unchanged and ARMED: `ti_signals` chat `-1004480146594` (wm ≈ 889), `wayond` chat `-1003842321905` (wm ≈ 151). **Record the exact live watermarks immediately before** (they must only advance).
4. Flags: `SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED` **unset/OFF** (keep OFF); `PROVIDER_COMMANDS_ENABLED` OFF; `MT5_ALLOW_LIVE` unset; `BROKER_INTELLIGENCE_INGEST_ENABLED=1` (leave as-is).
5. A rollback image is tagged and present (prod keeps `guvfx-prod-guvfx-backend:rollback-*`); tag the current `:latest` (built from `a474f79`) as `rollback-preT1` before building the new image.
6. No concurrent migration/deploy of another subsystem is in flight.

## B. Backup & recovery
- **Known gap (standing):** there is no automated prod DB backup (see `docs/operations`/memory). Take a point-in-time
  logical dump of the `signal_intake_*` tables **only if separately authorised** (`pg_dump -t 'signal_intake_*'` into
  the host, not off-host; it contains signal text — treat as sensitive). **Do not trigger a new full backup/restore
  here unless already authorised.**
- Recovery point: the pre-T1 schema (migrations at 0009) + the `rollback-preT1` image.
- The migrations are reversible pre-cutover (§H).

## C. Deployment order (controlled; the only proven-safe sequence)
1. `git -C /home/ubuntu/guvfx-app fetch && git -C /home/ubuntu/guvfx-app checkout 48db801` (or ff `main`).
2. Tag current image: `docker tag guvfx-prod-guvfx-backend:latest guvfx-prod-guvfx-backend:rollback-preT1`.
3. Build new image: `docker build -t guvfx-prod-guvfx-backend:latest /home/ubuntu/guvfx-app/backend`.
4. **STOP the acquisition writer** (begins the brief window): `docker stop guvfx-wayond-listener`. (Telegram retains messages; they are caught up in step 7 via the watermark.)
5. **Apply migrations** (new image, one-off, DB only): `cd /home/ubuntu/guvfx-prod && docker compose run --rm --no-deps guvfx-backend python manage.py migrate signal_intake`. Expected: `0010`, `0011` applied; ~<1 s on current table sizes (rehearsed at prod scale: 0009→0010 ≈ 0.7 s, 0010→0011 ≈ 0.14 s).
6. **Recreate backend** (new image): `docker compose up -d --force-recreate --no-deps guvfx-backend`. **NEVER `--remove-orphans`.**
7. **Recreate the listener with the new image** (it is an AD-HOC `docker run`, NOT compose-managed — reuse its exact args + `--env-file`): `docker rm -f guvfx-wayond-listener` then re-run the new `:latest` with `python manage.py run_wayond_listener --live --health-file /tmp/wayond_health` and the same env-files/network as the prior container (capture via `docker inspect guvfx-wayond-listener` BEFORE step 4). On start it catches up from each provider's watermark (`iter_messages(min_id=…)`) — ends the window.
8. (Consistency, not safety-critical) recreate the other backend-image workers — `guvfx-mt5-trade-ingest-worker`, `guvfx-mt5-shadow-worker`, `guvfx-mt5-node2-order-worker`, `guvfx-tp-protection-watcher`, `guvfx-customer-notification-worker` — so the estate is one version. They only READ signal_intake (no inserts to the changed tables), so OLD workers are schema-read-compatible and their order is not critical.

## D. Service impact
- **Restarted/recreated:** `guvfx-backend` (brief API blip) and `guvfx-wayond-listener` (acquisition paused for the migrate+recreate window, ~1–2 min). Optionally the backend-image workers (§C.8).
- **Untouched (MUST NOT be restarted/modified):** `guvfx-postgres`, `guacd`/`guacamole`, `traefik`, `guvfx-mt5-validate-worker`, the Withdrawal V1 Gmail ingestion timer (`guvfx-broker-ingest.timer`) and `BROKER_INTELLIGENCE_INGEST_ENABLED`, all MT5 terminals/Windows host, all broker accounts. No Telegram source switch, no watermark reset, no second Telethon client.

## E. Verification (expected post-DARK state)
- `showmigrations signal_intake` → `0011` applied.
- Providers still ARMED; chats unchanged (`-1004480146594`, `-1003842321905`); **watermarks ≥ the pre-deploy values** (advanced only by normal ingestion — never reset/regressed).
- Data: `PendingSignalApproval` with `chat_id<>''` ≈ 857 (the 6 unlinked `wayond` approvals stay `''`); `AcquiredMessage` `non_genuine=True` = 121 (the historical `DROPPED_NOT_ARMED`); `AcquiredMessage` total unchanged except normal new ingestion.
- `SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED` still OFF → `SignalExecutionPlan.chat_id` still `''`; no new `ExecutionJob` attributable to the deploy; provider-command engine still DARK.
- Listener health file fresh; new signals acquire normally (INTAKEN) within the staleness window.

## F. Failure detection (signatures)
- Migration error / partial apply → see §G.
- Listener logs `NotNullViolation` / `UndefinedColumn` on insert → a code/schema version mismatch (writer not on the new image, or migration not applied) — stop and correct the ordering.
- A provider watermark **regresses** → never expected; abort and investigate (possible watermark reset — forbidden).
- Unexpected duplicate `PendingSignalApproval` for one `(source, chat_id, message_id)` → dedup/partition anomaly.
- Any new `SignalExecutionPlan` with a non-blank `chat_id` while the flag is OFF → flag leak; abort.

## G. Rollback
- **Migration failed mid-apply:** each operation is transactional; re-run `migrate` after fixing the cause, or reverse: `docker compose run --rm --no-deps guvfx-backend python manage.py migrate signal_intake 0009` (reverses 0011 then 0010; see §H limits), then redeploy the `rollback-preT1` image to backend + listener.
- **New code faulty after deploy:** reverse to `0009` (as above) **and** redeploy `rollback-preT1` to backend + listener (code and schema must move together — do not leave new code on old schema or vice-versa).
- Keep the `rollback-*` image tags; verify the chosen rollback image predates T1 (`rollback-preT1`).

## H. Migration-rollback limitation (do not force)
Reverse (0011→0010→0009) re-imposes the old `uniq_provider_message (provider, message_id)` / `uniq_source_message (source, message_id)` keys. This is **safe now** (one chat per provider → no two rows share `(provider, message_id)` across chats). It becomes **UNSAFE after a future Telegram channel switch** introduces genuine cross-chat rows with the same `message_id` (the old key would reject them). **Never weaken or drop a constraint to force an unsafe rollback**; after a cutover, roll back by code/image only, not by schema reversal.

## I. Abort conditions (cancel the deployment)
Any precondition (§A) fails; actual prod state contradicts this baseline; the migration cannot complete or threatens existing rows; the listener will not come up healthy on the new image; a watermark would need resetting or a Telegram restart/source-switch would be required to proceed; any trading-safety invariant (§12 of the packet) cannot be verified; or the rollback path cannot be established. A missing authorization is **not** permission to proceed.

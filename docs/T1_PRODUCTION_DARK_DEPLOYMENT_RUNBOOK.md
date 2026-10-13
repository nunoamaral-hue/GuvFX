# T1 — Production DARK Deployment Runbook (PREPARED — execution NOT authorized) · R1

Packet `T1_PRODUCTION_DARK_DEPLOYMENT_READINESS` (R1 corrections). Prepared for a future, separately-authorised
operator. **Not** an authorization to deploy. T1 merged to `main` at `48db801`; prod NOT deployed (host at `a474f79`,
`signal_intake` migrations at `0009`).

> **Two blocking findings gate this runbook (see the readiness report).** (1) Catch-up is **NOT guaranteed
> lossless** — `catch_up` fetches only the 200 oldest messages (single call) and the 600 s staleness window drops
> aged signals; a bounded code correction is required for a completeness guarantee. (2) Database recovery is **not
> ready** — no PITR, no automated backup. Do not treat this runbook as GO until both are resolved by the Programme
> Director / Sponsor.

> **Mixed-version is UNSAFE (verified).** `AcquiredMessage.non_genuine` and `PendingSignalApproval.chat_id` are NOT
> NULL with no DB default → OLD code + NEW schema fails inserts; NEW code + OLD schema hits undefined columns. The
> acquisition writer must be stopped across migrate→recreate.

## A. Preconditions
1. `main` HEAD = `48db801`; host `/home/ubuntu/guvfx-app` fast-forwarded to it.
2. `signal_intake` applied = **0009**; 0010/0011 pending (`showmigrations signal_intake`).
3. Providers ARMED, chats unchanged: `ti_signals` `-1004480146594`, `wayond` `-1003842321905`. **Record exact live watermarks now** (must only advance).
4. Flags: `SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED` OFF (keep OFF); `PROVIDER_COMMANDS_ENABLED` OFF; `MT5_ALLOW_LIVE` unset; `BROKER_INTELLIGENCE_INGEST_ENABLED=1` (leave).
5. **Pin rollback images by IMAGE ID (not the mutable `:latest`):** `docker tag 8e289a18ba29 guvfx-prod-guvfx-backend:rollback-preT1` (current backend) and `docker tag <current listener id, e.g. 7dd209f33a83> guvfx-wayond-listener:rollback-preT1`.
6. **(BLOCKING, separate authorization) a consistent pre-migration recovery point exists** — see §B.
7. No concurrent migration/deploy of another subsystem.

## B. Backup & recovery (BLOCKED — see readiness report)
- **No PITR** (`archive_mode=off`, `wal_level=replica`) and **no automated backup** on prod. The only capability is an on-demand logical `pg_dump` (pg_dump 15.15 in `guvfx-postgres`; DB ≈ 2.77 GB).
- **Recovery-point sequencing (CORRECTED — do NOT take the full dump inside the window):** a full-DB `pg_dump` of ≈ 2.77 GB takes minutes and MUST NOT run between the listener stop and the recreate — that would blow the acquisition interruption past the 600 s freshness window and age in-flight signals to STALE. Instead, take the full-DB recovery dump **BEFORE** the maintenance window (before §C.4, listener still running — a logical dump is a consistent MVCC snapshot). Its recovery point is then slightly earlier than the migrate; the few messages acquired between the dump and the migrate are *after* the recovery point and are re-acquired by catch-up on restart (subject to the §catch-up limits). A tiny `signal_intake`-scoped consistency dump MAY additionally be taken in-window (sub-second) for a precise schema-state snapshot, but a scoped restore risks cross-app FK inconsistency, so the full-DB dump remains the authoritative recovery artifact. All dumps on-host, mode 0600, owner `ubuntu`, encrypted at rest, retained ≥ until stable; **never off-host**; contains signal text → sensitive. **This procedure does not by itself guarantee a bounded interruption** — the interruption bound comes from excluding the dump from the window (and from §catch-up completeness, still BLOCKED).
- **Restore procedure (rehearsed in isolation — dump/restore round-trip preserves the partitioned-unique schema and coexisting rows):** `createdb` scratch → `psql -f dump` → verify counts → promote only under a full maintenance stop. A **full-DB restore reverts ALL subsystems** to the recovery point (trades, withdrawal evidence, etc. written since are **lost**) → it is a **last resort**, not the primary rollback.
- **Max recovery-point loss:** primary rollback (schema reverse, §G) = 0 rows lost; last-resort full restore = everything since the pre-migration dump (heavy blast radius). **Max restoration downtime:** full logical restore of ~2.77 GB ≈ minutes, all services down.
- **Messages acquired after the recovery point:** lost on a full restore; re-acquisition then depends on catch-up (subject to the §catch-up limits). **Never treat an image rollback as a database backup.**

## C. Deployment order (controlled maintenance; the only proven-safe sequence)
1. FF host repo to `48db801`.
2. Pin rollback image ids (§A.5).
3. Build new images from `48db801`: backend `docker build -t guvfx-prod-guvfx-backend:latest /home/ubuntu/guvfx-app/backend`; rebuild/retag the listener image `guvfx-wayond-listener:latest` from the same code (it runs the Django app).
4. **(if authorized) take the §B full-DB recovery dump — BEFORE stopping anything** (outside the window; see §B).
5. **STOP the acquisition writer:** `docker stop guvfx-wayond-listener`. **Record the window start.** Telegram retains messages (caught up in §C.8 — subject to the completeness limits in §catch-up).
6. **Apply migrations** (new image, one-off): `cd /home/ubuntu/guvfx-prod && docker compose run --rm --no-deps guvfx-backend python manage.py migrate signal_intake`. Rehearsed prod-scale ≈ 0.84 s; allow generously for lock acquisition under load (§F abort bound).
7. **Recreate backend:** `docker compose up -d --force-recreate --no-deps guvfx-backend`. **NEVER `--remove-orphans`.**
8. **Recreate the listener (new image) using the exact launch contract (§D):** `docker rm -f guvfx-wayond-listener` then the canonical `docker run` (§D). It catches up from each provider's watermark — **verify completeness (§E) before declaring success.**
9. (Version consistency, NOT safety-critical; they only READ signal_intake) optionally recreate the other backend-image workers. **Do not restart LIVE order workers / MT5 / Windows unnecessarily.**

## D. Listener recreation contract (reflects the CURRENTLY-RUNNING container)
**Capture the live config first (authoritative):** `docker inspect guvfx-wayond-listener` BEFORE stopping it and reconstruct the run command from THAT, not from `.bash_history`. Verified running config (2026-10-13): `--restart unless-stopped`, `--network guvfx-prod_default`, user `appuser`, **NO docker healthcheck** (health is the app-written `/tmp/wayond_health` file, not a container `--health-cmd`), no volume mounts, cmd `run_wayond_listener --live --health-file /tmp/wayond_health`. The authenticated session + creds (`TELEGRAM_API_ID/API_HASH/STRING_SESSION`, DB_*, `GUVFX_AGENT_TOKEN`, …) are provided as env (the running container's `--env-file`, hinted by `.bash_history` as `/home/ubuntu/guvfx-prod/wayond-listener.env` — **confirm against the live container's resolved env before relaunch**).
```
docker run -d --name guvfx-wayond-listener --restart unless-stopped \
  --network guvfx-prod_default \
  --env-file <the env-file the LIVE container uses>  \
  guvfx-wayond-listener:latest \
  python manage.py run_wayond_listener --live --health-file /tmp/wayond_health
```
- **Preserving the env-file preserves the authenticated Telegram session** → no new login, no second Telethon client, no source switch. Do not place its values in Git/docs. (Note: the `.bash_history` variant included a `--health-cmd check_wayond_listener …`; the running container does NOT have it — if a docker healthcheck is desired, add it deliberately, but match the live container by default.)
- **Rollback launch contract:** identical command with `guvfx-wayond-listener:rollback-preT1` (the pinned pre-T1 image id).

## E. Verification (expected post-DARK state) + catch-up completeness
- `showmigrations signal_intake` → `0011`. Providers ARMED, chats unchanged; **watermarks ≥ pre-deploy** (never reset/regressed). `PendingSignalApproval` `chat_id<>''` ≈ 857 (6 `wayond` stay `''`); `AcquiredMessage` `non_genuine=True` = 121. Flag OFF → `SignalExecutionPlan.chat_id` still `''`; no new `ExecutionJob` from the deploy; engine DARK.
- **Catch-up completeness (both providers) — NOT auto-guaranteed:** after relaunch, confirm for EACH provider that the live watermark has advanced to the channel's newest message id (no gap) and that `catch_up` reported `processed` consistent with the backlog. If the backlog exceeded ~200 messages, or the window exceeded the 600 s staleness window, catch-up is INCOMPLETE (messages abandoned by the 200-cap, or acquired STALE rather than tradeable). Treat such a deployment as a failed window (§failure/recovery).

## F. Failure detection (signatures) & timing bounds
- Migration error / partial apply → §G. Each migration is transactional (clean rollback on failure).
- Listener logs `NotNullViolation`/`UndefinedColumn` on insert → code/schema version mismatch; stop and correct ordering.
- A watermark **regresses** → never expected; abort.
- Unexpected duplicate `PendingSignalApproval` for one `(source, chat_id, message_id)`; or any `SignalExecutionPlan` with a non-blank `chat_id` while the flag is OFF → abort.
- **Timing/abort bounds (set before execution):** max acquisition interruption ≪ **600 s** (hard: the staleness window — beyond it, in-flight signals age to STALE); max migration lock wait (e.g. 30 s — abort before migrate if not acquired); max catch-up backlog **< 200 per provider** (above it, catch-up is incomplete). Abort BEFORE §C.6 if any precondition or the window budget is already exceeded.

## G. Rollback
- **Migration failed mid-apply:** transactional → re-run after fixing, or reverse (§H), then redeploy `rollback-preT1` images to backend + listener.
- **New code faulty:** run the §H detection query; if REVERSIBLE, reverse to `0009` **and** redeploy `rollback-preT1` to backend + listener (code+schema move together). If NOT reversible → last-resort full-DB restore from the §B recovery point (heavy blast radius) under a full maintenance stop.
- Image-only rollback is **insufficient alone** when the schema is already migrated (old code + new schema is unsafe) — code and schema must be rolled back together.

## H. Migration-reversal boundaries (three distinct states — do not conflate)
- `REVERSIBLE_IN_CLEAN_STATE` — no rows.
- `REVERSIBLE_WITH_EXISTING_DATA` — holds **only** while no `(provider, chat_id, message_id)` has both a `non_genuine=True` and a `non_genuine=False` row (0011 reverse), and no `(provider, message_id)` spans two chats (0009 reverse). **This coexistence can form pre-cutover** (PAUSED drop → re-armed genuine, same id) — verified: the reverse then fails with `could not create unique index`. **Detection (run READ-ONLY before any reverse):**
  ```sql
  SELECT count(*) FROM (SELECT provider_id,chat_id,message_id FROM signal_intake_acquiredmessage
    GROUP BY 1,2,3 HAVING count(*)>1) t;   -- >0 blocks 0011 reverse
  SELECT count(*) FROM (SELECT provider_id,message_id FROM signal_intake_acquiredmessage
    GROUP BY 1,2 HAVING count(*)>1) t;       -- >0 blocks 0009 reverse (cross-chat)
  ```
  (Both are **0** on the current pre-migration prod.)
- `RESTORABLE_FROM_RECOVERY_POINT` — when reverse is unsafe, recovery is via the §B dump restore only. **Never delete or rewrite immutable evidence, and never drop a constraint with data loss, to force a schema reversal.**

## I. Abort conditions
Any §A precondition fails; prod state contradicts this baseline; migration cannot complete or threatens rows; listener will not come up healthy or catch-up is incomplete; a watermark reset or Telegram restart/source-switch would be needed to proceed; any trading-safety invariant unverifiable; or the rollback path (reverse or recovery-point) cannot be established. A missing authorization is not permission to proceed.

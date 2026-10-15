# T1 — Production DARK Deployment Runbook (PREPARED — execution NOT authorized) · R1

Packet `T1_PRODUCTION_DARK_DEPLOYMENT_READINESS` (R1 corrections). Prepared for a future, separately-authorised
operator. **Not** an authorization to deploy. T1 merged to `main` at `48db801`; prod NOT deployed (host at `a474f79`,
`signal_intake` migrations at `0009`).

> **UPDATE 2026-10-14 (`T1_TELEGRAM_CATCHUP_COMPLETENESS_R1`): HIGH-1 catch-up correction now IMPLEMENTED +
> CERTIFIED (DARK, PR #497).** `catch_up` paginates beyond 200 (no abandonment), resumes from a durable per-provider
> CHECKPOINT (`catchup_checkpoint_message_id`, distinct from the contaminable watermark), bounds pages / wall-clock /
> flood-wait, and reports an explicit per-provider `COMPLETE` / `INCOMPLETE_CEILING` / `INCOMPLETE_ERROR` status. A
> NEW migration **0012** (additive `catchup_checkpoint_message_id`, watermark-backfilled, DARK) is part of this
> deploy → the migration path is now **0009 → 0010 → 0011 → 0012**. The old "fetches only 200 / 200-cap" statements
> below are superseded; completeness is now verified by the per-provider `COMPLETE` status (§E).
>
> **Remaining blocking finding: (2) Database recovery is NOT ready** — no PITR, no automated backup (HIGH-2). Do not
> treat this runbook as GO until HIGH-2 is resolved by the Programme Director / Sponsor.
>
> _(Original R1 blocking note, for the record: catch-up was NOT guaranteed lossless — `catch_up` fetched only the
> 200 oldest messages in a single call; that is the defect PR #497 fixed.)_

> **Mixed-version is UNSAFE (verified).** `AcquiredMessage.non_genuine` and `PendingSignalApproval.chat_id` are NOT
> NULL with no DB default → OLD code + NEW schema fails inserts; NEW code + OLD schema hits undefined columns. The
> acquisition writer must be stopped across migrate→recreate.

## A. Preconditions
1. `main` HEAD = `48db801`; host `/home/ubuntu/guvfx-app` fast-forwarded to it.
2. `signal_intake` applied = **0009**; **0010/0011/0012 pending** (`showmigrations signal_intake`). 0012 (`catchup_checkpoint`) is additive + watermark-backfilled (DARK) and UNCONDITIONALLY reversible (drop column; RunPython reverse is a noop).
3. Providers ARMED, chats unchanged: `ti_signals` `-1004480146594`, `wayond` `-1003842321905`. **Record exact live watermarks now** (must only advance).
4. Flags: `SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED` OFF (keep OFF); `PROVIDER_COMMANDS_ENABLED` OFF; `MT5_ALLOW_LIVE` unset; `BROKER_INTELLIGENCE_INGEST_ENABLED=1` (leave).
5. **Pin rollback images by IMAGE ID (not the mutable `:latest`):** `docker tag 8e289a18ba29 guvfx-prod-guvfx-backend:rollback-preT1` (current backend) and `docker tag <current listener id, e.g. 7dd209f33a83> guvfx-wayond-listener:rollback-preT1`.
6. **(BLOCKING, separate authorization) a consistent pre-migration recovery point exists** — see §B.
7. No concurrent migration/deploy of another subsystem.
8. **(R2 §6 — checkpoint-init reconciliation GATE, run BEFORE §C.6 migrate, read-only, fail-closed):**
   `docker compose run --rm --no-deps guvfx-backend python manage.py reconcile_catchup_checkpoint --gate`. It
   compares each provider's watermark (the 0012 seed) to the highest genuine acquired id and **exits non-zero** on any
   REVIEW / no-match. A PASS means the seed is CONSISTENT + behaviour-preserving — NOT a proof of historical
   completeness (DB evidence can't prove it: Telegram ids are non-contiguous). On REVIEW, do NOT apply 0012 / deploy;
   escalate (a watermark reset to re-scan history is a separate Sponsor-gated cutover, never done here).

## B. Backup & recovery (design CERTIFIED; production activation still BLOCKED)
- **Recovery foundation:** design + certified DARK tooling now exist — `docs/DB_RECOVERY_FOUNDATION.md`
  and `ops/recovery/` (backup/verify/restore/audit; isolated certification 64/64, least-privilege
  role). The recovery *design* is certified in isolation, but **`PRODUCTION_BACKUP_ACTIVE` is still
  BLOCKED**: no off-host storage, no automated schedule, and the root FS is **96% full (9.2 GB free)**
  — a hard blocker for an on-host dump/restore. Resolve those (Sponsor-gated) before relying on this
  for a real deployment. **Recovery-readiness gate:** validate recovery points with
  `ops/recovery/audit_report.sh --dir <dir> --gate-usable` (the usable-recovery gate: inventory **plus**
  mandatory per-artifact integrity verification). Plain `--gate` is inventory-only and must NOT be
  treated as proof a recovery point is restorable.
- **No PITR** (`archive_mode=off`, `wal_level=replica`) and **no automated backup** on prod (verified
  2026-10-14). The only live capability is an on-demand logical `pg_dump` (pg_dump 15.15 in
  `guvfx-postgres`; DB ≈ 2.84 GB, ~79% of it the `core_auditevent` telemetry table).
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
8. **Recreate the listener (new image) using the exact launch contract (§D):** `docker rm -f guvfx-wayond-listener` then the canonical `docker run` (§D). It catches up each provider from its durable CHECKPOINT (bounded, resumable, paginated) — **verify per-provider `COMPLETE` (§E) before declaring success.**
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
- `showmigrations signal_intake` → `0012`. Providers ARMED, chats unchanged; **watermarks ≥ pre-deploy** (never reset/regressed); each provider's `catchup_checkpoint_message_id` backfilled from its watermark by 0012 then advanced by the catch-up pass. `PendingSignalApproval` `chat_id<>''` ≈ 857 (6 `wayond` stay `''`); `AcquiredMessage` `non_genuine=True` = 121. Flag OFF → `SignalExecutionPlan.chat_id` still `''`; no new `ExecutionJob` from the deploy; engine DARK.
- **Catch-up completeness (both providers) — read the per-provider STATUS, do not infer from liveness:** the listener log line `wayond listener catch-up: provider=<slug> status=<STATUS> from=<checkpoint> checkpoint=<id> pages=N retrieved=N acquired=N …` is emitted once per provider. **Success requires `status=COMPLETE` for EVERY provider.** The container health file turning green is LIVENESS, not completeness — a provider can be INCOMPLETE while the listener is healthy (the listener also logs a WARNING: `catch-up INCOMPLETE for providers=…`). Confirm each provider's `checkpoint` advanced to (or past) the channel's newest acquired id with no gap.

### §catch-up — operational contract for an INCOMPLETE catch-up
- `status=COMPLETE` → backlog drained within all bounds; done.
- `status=INCOMPLETE_CEILING` (page ceiling) or `INCOMPLETE_ERROR` (fetch/flood-wait/time-budget/processing error) → the backlog is **NOT** fully drained, but **nothing is silently lost**: the durable `catchup_checkpoint_message_id` was advanced ONLY past genuinely-acquired messages, so a **re-run resumes from the checkpoint with no skip and no duplicate durable rows** (acquisition dedups; a non-genuine DROPPED_NOT_ARMED row never advances the checkpoint, so a paused→armed provider is never stranded). Action: re-run catch-up (restart the listener, or re-invoke) and confirm it reaches `COMPLETE`; if it will not converge within a window ≪ the 600 s staleness window, treat the deployment as a **failed window** (§G) — do NOT declare success on an INCOMPLETE pass.
- A message aged past the 600 s staleness window is recorded **STALE** (genuine, advances the checkpoint) — NOT a tradeable recovery; this is expected, not an incompleteness.

## F. Failure detection (signatures) & timing bounds
- Migration error / partial apply → §G. Each migration is transactional (clean rollback on failure).
- Listener logs `NotNullViolation`/`UndefinedColumn` on insert → code/schema version mismatch; stop and correct ordering.
- A watermark **regresses** → never expected; abort.
- Unexpected duplicate `PendingSignalApproval` for one `(source, chat_id, message_id)`; or any `SignalExecutionPlan` with a non-blank `chat_id` while the flag is OFF → abort.
- **Timing/abort bounds (set before execution):** max acquisition interruption ≪ **600 s** (hard: the staleness window — beyond it, in-flight signals age to STALE); max migration lock wait (e.g. 30 s — abort before migrate if not acquired); **each provider's catch-up pass must reach `status=COMPLETE` within a window ≪ 600 s** (an `INCOMPLETE` pass is a failed window — not a silent loss; it resumes from the durable checkpoint on re-run; §E). The catch-up itself is self-bounded (page size/count, `WAYOND_CATCHUP_TIME_BUDGET_SECONDS`, bounded + deadline-aware flood-wait). Abort BEFORE §C.6 if any precondition or the window budget is already exceeded.

## G. Rollback
- **Migration failed mid-apply:** transactional → re-run after fixing, or reverse (§H), then redeploy `rollback-preT1` images to backend + listener.
- **New code faulty:** run the §H detection query; if REVERSIBLE, reverse to `0009` **and** redeploy `rollback-preT1` to backend + listener (code+schema move together). If NOT reversible → last-resort full-DB restore from the §B recovery point (heavy blast radius) under a full maintenance stop.
- Image-only rollback is **insufficient alone** when the schema is already migrated (old code + new schema is unsafe) — code and schema must be rolled back together.

### G.1 Recovery decision matrix (recovery foundation)
Integrated with `docs/DB_RECOVERY_FOUNDATION.md` + the certified tooling `ops/recovery/`. **Routine
responses preserve committed data; a full-DB restore is a separately-authorized last resort; immutable
ledger rows are NEVER deleted to force an older uniqueness constraint.** The pre-deploy recovery point
is the §B logical backup (encrypted, off-host once activated — `PRODUCTION_BACKUP_ACTIVE` is still
BLOCKED, so today the point is the manual pre-window dump).

- **A. Migration fails BEFORE commit** → each migration is transactional → automatic rollback; **0 rows
  lost**. Fix forward and re-run `migrate`; no restore. Abort the window if it cannot complete.
- **B. Migration commits but application START fails** → schema is at `0012`, data intact. Prefer a
  **forward fix** (the new schema is additive + DARK). If the code is unrecoverable: run the §H
  detection query; if `REVERSIBLE`, reverse to `0009` **and** redeploy `rollback-preT1` (code+schema
  together). **0 rows lost.** No full restore.
- **C. Application starts but listener CATCH-UP is INCOMPLETE** → NOT a data-loss event and NOT a
  rollback trigger. The durable `catchup_checkpoint_message_id` means a re-run resumes without skip
  (runbook §E §catch-up). Re-run catch-up; if it will not reach `COMPLETE` within a window ≪ 600 s,
  treat as a failed window and roll back code+schema (B) — still **0 rows lost**.
- **D. Application faulty AFTER new records were written** → **preserve the new records.** Forward-fix
  in place where safe. If a code rollback is required, reverse schema (§H) only if `REVERSIBLE` and
  redeploy `rollback-preT1`; the post-deploy rows remain. A full-DB restore to the pre-deploy point
  would **discard** those rows (and unrelated trading / Withdrawal V1 activity) → **last resort only**,
  under a full maintenance stop, with explicit authorization.
- **E. Schema reversal BLOCKED by legitimate genuine/non-genuine coexistence** (§H detection query
  `>0`) → do **not** force it; never delete immutable rows to satisfy an old constraint. Keep the new
  schema and forward-fix the code, **or** (last resort, authorized) restore the full DB from the §B
  recovery point under a full stop, accepting the recovery-point loss window.

## H. Migration-reversal boundaries (three distinct states — do not conflate)
- **0012 (`catchup_checkpoint`) is UNCONDITIONALLY reversible** at any data state: its reverse drops the added column and its `RunPython` reverse is a noop (it never rewrites or deletes rows). The boundaries below concern the 0011→0010→0009 reverses only.
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

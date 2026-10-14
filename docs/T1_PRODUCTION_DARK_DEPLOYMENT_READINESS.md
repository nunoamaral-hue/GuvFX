# T1_PRODUCTION_DARK_DEPLOYMENT_READINESS_R1_REPORT

Packet `T1_PRODUCTION_DARK_DEPLOYMENT_READINESS_R1`. Readiness engineering only — **no deployment, no production
migration, no Telegram change, no flag activation, no production backup/restore performed.** Production inspection
was **read-only**; rehearsals ran against **isolated local/scratch databases** with **synthetic** data (no prod
credentials or Telegram content copied). Supersedes the initial readiness report; four HIGH gaps now resolved as
**evidence or BLOCKED**, with no unsupported PASS.

## UPDATE — 2026-10-14 (packet `T1_TELEGRAM_CATCHUP_COMPLETENESS_R1`)
This R1 report is a point-in-time assessment; the body below is preserved as the record of that
assessment. Deltas since:
- **HIGH-1 (catch-up completeness) — code correction now IMPLEMENTED + CERTIFIED (DARK).** The bounded,
  resumable, observable `catch_up` called for in "Exact remaining…" item 1 is merged into the branch
  (PR #497): it paginates beyond 200 (no abandonment), resumes from a durable per-provider CHECKPOINT
  (`catchup_checkpoint_message_id`, distinct from the contaminable watermark), bounds pages / wall-clock /
  flood-wait, and reports an explicit per-provider `COMPLETE` / `INCOMPLETE_CEILING` / `INCOMPLETE_ERROR`
  status (never a false COMPLETE). Certified in 48 synthetic catch-up cases + a renewed adversarial review
  (4 confirmed defects fixed and re-verified). The §7 "catch-up backlog < 200/provider" bound is
  **SUPERSEDED**: each provider's pass must report `COMPLETE` within the window, and the window must stay
  ≪ the 600 s staleness window; an `INCOMPLETE` pass is a FAILED window (re-run — it resumes from the
  durable checkpoint, no skip). See runbook §E + §catch-up operational contract. **Production completeness
  remains a deploy-time verification (not yet run) — NOT a production PASS.**
- **Migration 0012 now exists** (`0012_catchup_checkpoint`): additive `AddField catchup_checkpoint_message_id`
  + a `RunPython` that backfills the checkpoint from the current watermark (first-deploy behaviour unchanged,
  DARK). The deployment story is now **0009 → 0010 → 0011 → 0012** (the earlier "0010/0011 pending" is now
  **0010/0011/0012 pending**). 0012 is UNCONDITIONALLY reversible (drop column; `RunPython` reverse is a
  noop) — it does NOT carry the 0011 coexistence hazard (HIGH-3). Earlier notes that catch-up was
  "code-only, no migration" are superseded by 0012.
- **R2 corrections (2026-10-14) folded in:** the durable checkpoint is now the highest SAFELY-ACCOUNTED boundary —
  it never advances past the earliest UNACCOUNTED (ignored/non-genuine) retrieval of a pass, so a later genuine id
  cannot mask an earlier one (closes a silent-loss gap the R1 genuine-only guard left). FloodWait now REJECTS an
  oversized server-requested wait (fail-closed → INCOMPLETE) rather than shortening-and-retrying. **Checkpoint-init
  is now a mandatory pre-deploy GATE:** run the read-only `reconcile_catchup_checkpoint --gate` before applying 0012
  (see runbook §A.8); it is behaviour-preserving-consistent, NOT a historical-completeness proof. Renewed adversarial
  review (R2): 3 confirmed fixed + re-verified; `SignalUpdate` crash double-create closed (concurrent-race unique
  constraint deferred — KNOWN_ISSUES).
- **Overall verdict UNCHANGED: NO-GO.** HIGH-2 (production DB recovery) remains **BLOCKED** (separate Sponsor
  dependency; out of scope here). HIGH-1's code blocker is resolved, but deployment stays gated by HIGH-2.

## Verified baseline (read-only, 2026-10-13)
- Prod backend image `8e289a18ba29`; host repo **`a474f79`** (pre-T1). T1 `48db801` NOT on host/built/deployed.
- `signal_intake` applied through **`0009`**; **0010/0011/0012 pending** (0012 added by the R1 catch-up packet — see UPDATE). Existing unique keys present (`uniq_provider_message`, `uniq_source_message`, `uniq_provider_command`).
- Providers ARMED: `ti_signals` `-1004480146594` (wm 889), `wayond` `-1003842321905` (wm 151).
- Counts: AcquiredMessage 997 (DROPPED_NOT_ARMED 121, **0 blank chat**), approvals 863 (857 single-evidence→backfill, **6 wayond no-evidence→stay `''`**, 0 ambiguous), ProviderCommand 0, SignalUpdate 5. Tables ≤ 640 kB; DB 2.77 GB.
- Flags: `SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED` OFF, `PROVIDER_COMMANDS_ENABLED` OFF, `MT5_ALLOW_LIVE` unset, `BROKER_INTELLIGENCE_INGEST_ENABLED=1`.
- Isolated prod-scale migration rehearsal: 0009→0010 ≈ 707 ms, 0010→0011 ≈ 136 ms; result 857/6/121 (matches prod).

## HIGH-1 — Telegram catch-up completeness → **BLOCKED (R1-time) → code correction since IMPLEMENTED + CERTIFIED (DARK), see UPDATE**
_The assessment below reflects the R1-time (pre-correction) `catch_up`; the bounded/resumable/observable
rewrite that resolves it is now merged (PR #497) — see the UPDATE banner._
`WayondListener.catch_up` issues ONE `iter_messages(min_id=watermark, limit=200, reverse=True)` per provider at
startup. Proven by isolated synthetic tests:
- **Backlog 250, limit 200** → exactly **200 oldest acquired**, ids 201–250 **abandoned**, watermark jumps to 200 (the live `NewMessage` handler only receives messages arriving after connect → the gap is never fetched). **Signal loss for any backlog > 200.**
- **Message aged 1200 s (> 600 s window)** → outcome **STALE**, no approval (a STALE record is NOT a recovered tradeable signal).
- Distinction established: retrieval ≠ durable acquisition ≠ classification ≠ tradeable approval ≠ downstream eligibility. A stale/abandoned message is retrieved-or-skipped but **not** a tradeable recovery.
- **Observable completeness evidence** = per-provider watermark advanced to the channel's newest id with no gap; `catch_up` `processed` count consistent with the backlog.
- **Bounded corrective subsystem (RUNTIME — returned for PD authorization, NOT implemented here):** make `catch_up` paginate (loop, advancing `min_id`) until the backlog is exhausted instead of capping at 200, and emit a per-provider "caught_up_to=<id>" completeness signal; plus an operational hard bound that the maintenance window stays ≪ the staleness window. Until then, catch-up completeness cannot be certified.

## HIGH-2 — Production database recovery → **BLOCKED**
- **No PITR** (`archive_mode=off`) and **no automated backup** (no cron/timer; only historical on-demand `pg_dump` artifacts on host). `pg_dump` 15.15 available; DB 2.77 GB.
- **Recovery design:** a **Sponsor-authorized, pre-migration consistent logical dump** (taken after the listener stop, before migrate) is the recovery point; on-host, mode 0600, encrypted, retained until stable, never off-host (contains signal text). Image rollback is **not** a database backup.
- **Restore rehearsed (isolated scratch DBs, synthetic):** `pg_dump`→`psql` round-trip preserved row counts and the partitioned-unique schema, and the restored unique index still rejected a duplicate — the mechanism works.
- **Limitation:** a full-DB restore reverts **all** subsystems (trades, withdrawal evidence) to the recovery point → heavy blast radius, minutes of full downtime; a `signal_intake`-scoped restore risks cross-app FK inconsistency. There is **no demonstrable low-blast-radius recovery for this production**, and backup creation is not authorized here. → **BLOCKED** pending (a) a Sponsor-authorized pre-migration dump as an explicit deployment prerequisite and an accepted recovery-point-loss/blast-radius decision, or (b) standing up PITR/automated backup first.
- **Image provenance:** rollback images must be pinned by **image id** (`guvfx-prod-guvfx-backend:rollback-preT1` ← `8e289a18ba29`; `guvfx-wayond-listener:rollback-preT1` ← the current listener id), independent of the mutable `:latest`.

## HIGH-3 — Migration-reversal boundaries → **CERTIFIED (bounded)**
Three distinct, non-equivalent states, now explicit:
- `REVERSIBLE_IN_CLEAN_STATE` (no rows).
- `REVERSIBLE_WITH_EXISTING_DATA` — holds only while no `(provider, chat_id, message_id)` has both a `non_genuine=True` and `=False` row, and no `(provider, message_id)` spans two chats. **Current prod: 0 / 0 (read-only verified) → reversible now.** But this coexistence **can form pre-cutover** (PAUSED drop → re-armed genuine, same id) — **proven**: the 0011→0010 reverse then fails (`could not create unique index "uniq_provider_chat_message"`), evidence rows intact (transactional rollback).
- `RESTORABLE_FROM_RECOVERY_POINT` — the only recourse when reverse is unsafe.
A **READ-ONLY detection query** (runbook §H) must gate every reversal. Constraints are never dropped with data loss to force a reversal; immutable evidence is never rewritten.

## HIGH-4 — Complete database-writer isolation → **PASS**
Authoritative write-path inventory of `AcquiredMessage` / `PendingSignalApproval` / `ProviderCommand`:
- **Sole INSERT sites:** `services.intake_parsed` (approvals) and `acquisition.{acquire_message,_maybe_record_command}` (acquired/command/update) — reached in production **only** by the listener (`run_wayond_listener`).
- **Not writers:** no signal_intake API/URL exists (backend cannot insert via API); Django admin `has_add_permission=False` for all three; `ingest_wayond_signals_for_approval` is an **operator-invoked** management command (no daemon/cron); no signal-intake cron/timer on prod (the only timer is the unrelated Withdrawal V1 `guvfx-broker-ingest.timer`); execution workers (shadow/node2-order/tp-watcher/trade-ingest/auto-router) only READ signal_intake (schema-read-compatible) — they write ExecutionJob/Trade, not the changed tables. Approve/reject are UPDATEs that don't touch the new columns (old-code-safe).
- **Maintenance boundary:** stopping `guvfx-wayond-listener` isolates the sole continuous writer; recreate the backend on the new image; operationally, do not run the ingest command or add-via-admin during the window. No need to restart LIVE order workers / MT5 / Windows, and no need to stop Withdrawal V1 ingestion.

## §6 Listener recreation contract → **PASS** (see runbook §D)
Exact `docker run` captured (image `guvfx-wayond-listener:latest`, user `appuser`, network `guvfx-prod_default`,
`--env-file /home/ubuntu/guvfx-prod/wayond-listener.env`, `--restart unless-stopped`, `--health-cmd
check_wayond_listener …`, cmd `run_wayond_listener --live --health-file /tmp/wayond_health`, no mounts). The env-file
preserves the authenticated `TELEGRAM_STRING_SESSION` + API creds → **no new login, no second Telethon client**. A
rollback launch contract (same command, pinned pre-T1 image id) is documented. No secret values in Git/docs.

## §7 Maintenance-window failure bounds → recorded (not a guarantee)
Rehearsal 0.84 s is a measurement, not a production guarantee. Operational bounds (runbook §F): acquisition
interruption ≪ 600 s (hard — the staleness window); migration lock-wait cap (e.g. 30 s, else abort before migrate);
catch-up backlog < 200/provider (else incomplete — HIGH-1) **[SUPERSEDED — see UPDATE: catch-up now
paginates beyond 200 and reports per-provider `COMPLETE`/`INCOMPLETE`; the bound is now "pass reports
COMPLETE within a window ≪ 600 s", and an INCOMPLETE pass is a failed window]**; abort before migrate if
the budget is already exceeded; transactional migrations give clean rollback on partial progress.

## Certification matrix (§9 — PASS / BLOCKED / NOT_APPLICABLE; no unsupported PASS)
| Marker | Status |
|---|---|
| `T1_ENGINEERING_MERGED` | **PASS** (`48db801`; CI 10/10; 5801 backend tests OK) |
| `T1_PRODUCTION_SCHEMA_BASELINE_VERIFIED` | **PASS** (migrations 0009; constraints/indexes/counts read-only verified) |
| `T1_ALL_DATABASE_WRITERS_IDENTIFIED` | **PASS** (listener sole continuous writer; no API/cron/admin-add) |
| `T1_MIXED_VERSION_ISOLATION_PROVEN` | **PASS** (NOT NULL/no-default proven; isolation boundary = stop sole writer) |
| `T1_TELEGRAM_CATCHUP_COMPLETENESS_PROVEN` | **R1: BLOCKED → code correction CERTIFIED (DARK)** (PR #497: pagination + durable checkpoint + bounded resources + per-provider COMPLETE/INCOMPLETE; 48 cases + adversarial re-verify). Production-completeness check is a deploy-time verification (runbook §E), NOT yet run → not a production PASS. |
| `T1_MIGRATION_REVERSAL_BOUNDARIES_CERTIFIED` | **PASS** (three states; detection query; 0/0 now; coexistence-fail proven) |
| `T1_DATABASE_RECOVERY_READY` | **BLOCKED** (no PITR/auto-backup; only last-resort full dump; prerequisite dump unauthorized) |
| `T1_LISTENER_RECREATION_CONTRACT_READY` | **PASS** (exact contract incl. session preservation + rollback contract) |
| `T1_TRADING_ISOLATION_PROVEN` | **PASS** (source unchanged, flag OFF, engine DARK, MT5_ALLOW_LIVE unset, Withdrawal V1 untouched) |
| `T1_PRODUCTION_DARK_DEPLOYMENT_READINESS` | **BLOCKED** (gated by the two BLOCKED sub-certs above) |
| `T1_PRODUCTION_DEPLOYMENT` | **NOT AUTHORIZED** |
| `T1_TELEGRAM_SOURCE_CUTOVER` | **NOT AUTHORIZED** |

## GO / NO-GO
**NO-GO (BLOCKED)** for production DARK deployment as currently scoped. The always-on schema/code changes are
migration-safe and mixed-version-isolatable via the maintenance window, BUT two safety properties are not yet
established: **catch-up completeness** (HIGH-1) and **database recovery** (HIGH-2). A blocked readiness finding
remains BLOCKED — it is not a conditional pass.

## Exact remaining Sponsor-controlled actions / bounded corrections
1. ~~**Authorize + implement the HIGH-1 catch-up correction** (runtime): paginate `catch_up` beyond 200 + per-provider completeness evidence + window-bound < staleness.~~ **DONE (PR #497, R1) — implemented + certified DARK** (pagination, durable checkpoint, bounded resources, per-provider COMPLETE/INCOMPLETE). Production-completeness check remains a deploy-time verification.
2. **Resolve HIGH-2 recovery:** authorize a pre-migration consistent dump as a deployment prerequisite with an accepted blast-radius/recovery-point-loss decision, or stand up PITR/automated backup first.
3. **Then** issue a separate `T1_PRODUCTION_DARK_DEPLOYMENT` execution packet (maintenance-window procedure; flag stays OFF; source unchanged).

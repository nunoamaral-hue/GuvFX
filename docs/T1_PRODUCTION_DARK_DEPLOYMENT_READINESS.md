# T1_PRODUCTION_DARK_DEPLOYMENT_READINESS_REPORT

Packet `T1_PRODUCTION_DARK_DEPLOYMENT_READINESS`. Readiness engineering only — **no deployment, no production
migration, no Telegram change, no flag activation performed.** All production inspection below was **read-only**;
the migration rehearsal ran against an **isolated local test database** with **synthetic** prod-shape data (no
production credentials or Telegram content copied).

## Verified facts (GATE A/B — read-only prod inspection, 2026-10-13)
1. **Current production backend image/commit:** image `guvfx-prod-guvfx-backend:latest` (`8e289a18ba29`, built ~10 h ago); host repo `/home/ubuntu/guvfx-app` at **`a474f79`** (= the reported baseline, pre-T1). The T1 merge `48db801` is **NOT** on the host, not built, not deployed. Rollback-tagged images present (`rollback-pre*`).
2. **Verified `main` commit:** `48db801` (squash of reviewed `e29e194`).
3. **Schema/migration baseline:** `signal_intake` applied through **`0009_providercommand`**; `0010`/`0011` **pending**.
4. **Telegram provider state:** two providers, both **ARMED** — `ti_signals` (chat `-1004480146594`, wm 889, window 600 s) and `wayond` (chat `-1003842321905`, wm 151, window 600 s). A single AD-HOC `guvfx-wayond-listener` container (`run_wayond_listener --live`) serves both.
5. **Source & watermark:** unchanged; recorded above. No reset performed or required by this packet.
6. **Relevant table counts:** `AcquiredMessage` 997 (INTAKEN 857, DROPPED_NOT_ARMED 121, STALE 18, UPDATE 1; **0 blank chat_id**); `PendingSignalApproval` 863 (no `chat_id` column yet); `ProviderCommand` **0**; `SignalUpdate` 5; `SignalAuditEvent` 1794. Largest table 640 kB. Existing unique keys confirmed present: `uniq_provider_message`, `uniq_source_message`, `uniq_provider_command` (all three are the `0010` RemoveConstraint targets).
7. **Historical identity compatibility:** of 863 approvals, **857** have exactly one non-blank linked-`AcquiredMessage` chat → deterministically backfilled; **6** (all `wayond`) have no linked evidence → stay `''` (unresolved, never fabricated); **0** ambiguous. The 121 `DROPPED_NOT_ARMED` rows → `non_genuine=True`. Uniqueness cannot regress (new keys are strict supersets of the existing ones).
8. **Migration rehearsal (isolated test DB, synthetic prod-shape 997/863/121):** 0009→0010→0011 applied cleanly; result matched prod exactly — **857 approvals backfilled, 6 blank, 121 non_genuine**.
9. **Lock/concurrency assessment:** timings 0009→0010 ≈ **707 ms**, 0010→0011 ≈ **136 ms** (≈ 0.84 s total) at prod scale. `AddField` with a constant default is metadata-only on PG15 (no table rewrite); `AddConstraint` builds a unique index under a brief `ACCESS EXCLUSIVE` lock on ≤997-row tables (sub-second). No extended write-blocking at these sizes. **But** the writer must not run old code during the window (see 10/11).
10. **OLD code + NEW schema:** **UNSAFE.** `AcquiredMessage.non_genuine` and `PendingSignalApproval.chat_id` are **NOT NULL with no DB default** (verified by `information_schema` introspection). An old writer's INSERT omits them → NOT NULL violation. The AD-HOC listener (currently a 2-week-old image) would fail every acquisition after the migration.
11. **NEW code + OLD schema:** **UNSAFE.** New code references `non_genuine` / approval `chat_id` which do not exist pre-migration → undefined-column errors.
12. **Active trading isolation (GATE D):** source unchanged; all 997 acquired rows already carry a chat (replay dedups within `(provider, chat_id, message_id, non_genuine)`); the 121 DROPPED become `non_genuine=True` but their ids are ≤ watermark so catch-up never re-fetches them (no reintroduction); the edit path now re-applies the staleness window (no STALE resurrection); `ProviderCommand`=0 and engine DARK; no `SignalExecutionPlan` change while the flag is OFF; `MT5_ALLOW_LIVE` unset; Withdrawal V1 ingestion untouched. No live-broker action taken.
13. **Feature-flag state:** `SIGNAL_INTAKE_CHAT_AWARE_DOWNSTREAM_ENABLED` **unset (OFF)** in the backend env; `PROVIDER_COMMANDS_ENABLED` OFF; `MT5_ALLOW_LIVE` unset; `BROKER_INTELLIGENCE_INGEST_ENABLED=1` (unchanged).
14. **Proposed deployment sequence:** see the runbook §C — build → **stop listener** → migrate → recreate backend → recreate listener (new image, catch-up) → (optional) recreate other backend-image workers. A **brief coordinated window** (~1–2 min) is required because mixed-version operation is unsafe; the gap is covered losslessly by watermark catch-up within the 600 s staleness window.
15. **Proposed rollback sequence:** runbook §G — reverse to `0009` and redeploy the `rollback-preT1` image to backend+listener (code and schema move together).
16. **Required maintenance window:** yes — a short acquisition pause (listener stopped across migrate+recreate, ~1–2 min). No API hard-downtime beyond a backend restart blip. Within the staleness window → no signal loss.
17. **Known issues / unresolved risks:** (a) the ProviderCommand index-name drift — **harmless to this deployment** (no committed `0012` migration references it; `migrate` never touches it; it is only a dev-time `makemigrations` nuisance — §11 below); (b) no automated prod DB backup (standing gap) — rollback relies on the reversible migration + `rollback-preT1` image; (c) migration reverse is safe only pre-cutover (§H of the runbook).
18. **Documentation PR:** this report + the runbook + STATUS/NEXT corrections are delivered as a focused docs-only PR (no code, no deployment).
19. **GO / NO-GO recommendation:** **GO for a controlled DARK deployment, conditional on the maintenance-window procedure in the runbook** (stop-writer → migrate → recreate backend+listener). It is explicitly **NOT** a zero-downtime rolling deploy. All always-on code changes are safe once code and schema are moved together; the new downstream pathway stays DARK (flag OFF). No GO for any Telegram cutover or flag activation.
20. **Exact next Sponsor authorization required:** a separate `T1_PRODUCTION_DARK_DEPLOYMENT` execution packet authorizing (and scheduling) the maintenance-window procedure — permitting the listener stop/recreate and the production migration, while keeping the flag OFF and the source unchanged.

## §11 — ProviderCommand index-name drift: deployment impact
Evidence: prod has index `signal_inta_status_pc_idx` (hand-named in migration 0009); the model Meta leaves that index unnamed, so `makemigrations` wants a `RenameIndex` to `signal_inta_status_867cbc_idx`. **No such migration file is committed** (0010/0011 do not touch it). `manage.py migrate` applies only committed files, so it never attempts the rename; Django's migration-state consistency check does not fail on an *un-generated* migration. **Conclusion: harmless to the approved deployment sequence** — not a schema-validation or migrate blocker. Smallest corrective requirement (separate, non-deployment): name the model index to match `signal_inta_status_pc_idx` (zero schema change). Tracked separately; not added to T1.

## Certification markers
- `T1_ENGINEERING_MERGED` — **PASS** (`48db801`; CI 10/10; 5801 backend tests OK).
- `T1_SCHEMA_DEPLOYMENT_READY` — **PASS** (migrations preserve all records; backfill deterministic & prod-matched; sub-second locks; rehearsed on an isolated DB).
- `T1_RUNTIME_DEPLOYMENT_READY` — **PASS, conditional** (safe only via the stop-writer maintenance window; mixed-version operation is UNSAFE and must be avoided — not a rolling deploy).
- `T1_DEPLOYMENT_ROLLBACK_READY` — **PASS, bounded** (reverse + `rollback-preT1` image; reverse guaranteed pre-cutover only; no auto DB backup is a standing gap).
- `T1_PRODUCTION_DARK_DEPLOYMENT_READINESS` — **PASS** (GO conditional on the runbook procedure; evidence complete).
- `T1_PRODUCTION_DEPLOYMENT` — **NOT AUTHORIZED.**
- `T1_TELEGRAM_SOURCE_CUTOVER` — **NOT AUTHORIZED.**

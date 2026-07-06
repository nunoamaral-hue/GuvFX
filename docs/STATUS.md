# GuvFX — Project Status

> Update this file **whenever** project state changes. This is a current-state
> snapshot; deeper operational detail lives in `docs/RUNBOOK.md` and the handoff
> docs.

## Execution workstream log

- **2026-07-06 — E3-MONITOR-SCHEDULING: post-trade monitor chain scheduling prepared (repo-only, dry-run). ⏱️**
  New `execution.run_monitor_chain` management command runs the three shipped monitors in dependency
  order in one idempotent pass (`process_closed_trades` → `route_outcomes` → `dispatch_pending`) — it
  adds no execution logic, only wiring. Host-crontab overlay `deploy/monitor-scheduler/`
  (`crontab.monitor`, idempotent `install_monitor_cron.sh`, `verify_monitor_chain.sh`, runbook) matches
  the existing h1/m5/h4 scheduler pattern. **Safe at defaults:** internal records only; no order, no
  Telegram send (dispatch behind `NOTIFICATION_DISPATCH_ENABLED`, default OFF; transport dry-run —
  nothing transmitted), no WIMS. **Not deployed by merging** (install is a one-liner on the prod host).
  10 chain tests (empty-safe, in-order pipeline, idempotent, resilient, no-order/WIMS/transmit boundary,
  static AST); full backend suite 466 green; no migration. Follows E3-DEMO-PROMOTION (PR #92, `0fd36b1`).
  E3 remains RED.
- **2026-07-06 — E3-DEMO-EXECUTION-PROGRAMME: E3 roadmap (planning only); ~62% complete. 🗺️**
  Planning/architecture review only (no code, no deploy, no arming). New
  [docs/E3_ROADMAP.md](E3_ROADMAP.md) from a 6-agent read-only sweep of the current code + prod state.
  **Key finding: E3 needs no new execution engine** — the real order_send path already runs in prod
  for the strategy schedulers (`ExecutionJob PLACE_ORDER → trade-ingest worker → bridge /mt5/order →
  order_send → real demo ticket`), SYNC_POSITIONS auto-enqueue is DONE (views.py:390), and the whole
  close→outcome→candidate chain + monitors exist. **~62% complete.** Remaining engineering (~5 small/
  medium, repo-only, fail-closed): `SignalExecutionMode.DEMO` enum, `promote_plan_to_demo_jobs`, DEMO
  real-order payload, **populate `Trade.correlation_id` in upsert_trades (CRITICAL linkage)**, auto_router
  AUTO_DEMO wiring. Hard blockers are Nuno-only governance: Blueprint-06 ratification + recorded E3
  sign-off + risk-cap/node decisions. Ops: 3 monitor crons + deploy-to-shared-image (kill-switch window)
  + worker/node/bridge preflight; DB-backup + bridge-SPOF risk-accept for a supervised pilot. Real
  Telegram transport NOT on the critical path (dry-run suffices). 7-packet sequence to the first demo
  trade. **E3 RED.**

- **2026-07-06 — GFX-MIGRATION-READINESS: assessment + checklist; migration BLOCKED on GFX aging. 📋**
  Readiness/prep only (no migration, no Telegram login, no secret handling). New
  [docs/GFX_MIGRATION_READINESS.md](GFX_MIGRATION_READINESS.md): go/no-go assessment + gate
  checklist + consolidated procedure (references WAYOND_LISTENER_MIGRATION.md + DEPLOY_ISOLATED.md).
  **Verdict: NOT READY.** GFX (id 8661920471, @guvfx, DC5, real SIM, 2FA on, Wayond member) is only
  **~1 day** into its clean-aging window (last API login 2026-07-05; every login resets the clock +
  raises ban risk). Needs 7–14 clean days with NO API logins → earliest hardened attempt ~2026-07-12,
  ideally ~2026-07-19. Gates met: 2FA, Wayond membership/visibility. Gates pending: aging, session
  mint + persistence (reuse/15min/1h). Migration = secret swap + container recreate (no code change);
  rollback = revert the 3 TELEGRAM_* lines to personal + recreate. Personal-account exception stays
  live until GFX proven ≥1h post-cutover. Listener still read-only, provider un-armed. E3 RED.

- **2026-07-06 — AUTO-SHADOW-FOUNDATION: config-armed auto-router shipped, DISABLED BY DEFAULT (repo-only). ✅**
  Implements steps 1-3 of the ratified auto-execution architecture (PR #82). Merged main `f2ed0fc`
  (PR #83). Additive schema, safe defaults: `ExecutionControl.auto_execution_enabled`=False,
  `StrategyAssignment.execution_mode`=MANUAL, `ParserProfile.certification_level`=LOW. Fork =
  `signal_intake.signals.signal_acquired` (send_robust, new messages only) at the end of
  `acquire_message`; `signal_intake` never imports `execution` (one-way preserved); `execution.apps`
  connects the receiver. `execution/auto_router.py`: `effective_mode()` = AND of config gates (auto
  flag + SHADOW mode + kill-off + provider ARMED + source armed + certification≥MEDIUM + unique
  AUTO_SHADOW assignment); **edited signals hard-excluded**; fail-closed on any exception. Armed path
  reuses `approve → plan_demo_execution → promote_plan_to_shadow_jobs` → **PLACE_ORDER_SHADOW only**
  (no `order_send`, no executable job type). 21 tests; **full backend 382 green**; secret-scan +
  governance pass. Adversarial 4-lens boundary review = **GO** (no must-fix); hardened per its nits.
  Not deployed, not armed, no production behaviour change. **E3 RED.** NEXT auto milestones =
  close-monitor + profit-only Telegram, then auto-demo (real order_send — RED, own gated packet).

- **2026-07-05 — OPERATIONS-MODE: programme shifted to Operations Mode; Operations Dashboard created. 📋**
  Priority order is now stability → monitoring → observability → reliability → recovery → security →
  features. Created [docs/OPERATIONS_DASHBOARD.md](OPERATIONS_DASHBOARD.md) as the operational source
  of truth (all 11 prod containers + Windows MT5 box, deploy model, secrets map, monitoring state,
  backup/recovery, risk register, maturity). Read-only estate review (host snapshot + 7-agent repo
  analysis). Verdict: **"functional but pre-operational."** Top RED findings, each evidence-based:
  **(1) NO automated DB backup** (verified: no cron ubuntu/root, nothing in /var/backups, newest dump
  2026-02-19 ~4.5mo stale, no off-host) — total data-loss SPOF; (2) whole estate on one VPS (single
  Postgres/Traefik/Tailscale/Windows-bridge, no redundancy); (3) 9/11 containers have no healthcheck +
  trivial /health; (4) no confirmed alert delivery (RX-2 `reliability_tick` runs every min but sink
  unconfirmed); (5) exposed secrets un-rotated + password reuse (Nuno-held); (6) MT5 bridge SPOF
  (manual/autologon). Recommended next packet: **BACKUP-RECOVERY-BASELINE**. No production change; E3 RED.

- **2026-07-05 — WAYOND-LISTENER-GO-LIVE-TEMP-PERSONAL: listener DEPLOYED to production (isolated, acquisition-only). ✅**
  The read-only Wayond listener is **LIVE in prod** under the authorised temporary exception
  (personal-account session; target stays GFX). Nuno executed on the VPS with Claude guiding
  each step. **Fully isolated deploy** — the shared trading image `guvfx-prod-guvfx-backend`
  (used by `guvfx-backend` + trade-ingest + shadow workers) was **never rebuilt/restarted**:
  the listener image (`guvfx-wayond-listener:latest`) was built from a separate source dir
  (`/home/ubuntu/guvfx-listener-src`) + separate tag. Additive `signal_intake` migrations
  **0003–0006** applied to the prod DB (adversarially verified GO via a 3-lens Workflow: only
  those 4, no cross-app pull-in, running old-image backend unaffected; live `--plan` confirmed).
  Preconditions met: personal account **2FA on** (session mint prompted for password), fresh
  session **persisted** (`authorised=True` on reuse), stored only in the prod secret store
  (`/home/ubuntu/guvfx-prod/wayond-listener.env`, 600), never printed/committed. Container
  **`Up (healthy)`**; logs `connected (read-only)` → `catch-up processed=117` → `state=listening`.
  **Execution boundary proven:** provider `wayond` = **ONBOARDING (un-armed)**; all **117**
  messages `DROPPED_NOT_ARMED` (seen, zero intaken); **0 PendingSignalApproval**; ExecutionJob
  = 48 pre-existing (listener has no `execution` path → **0 created**). Env gotcha found+fixed:
  `docker run --env-file` mangles the quoted prod `.env` (and appuser can't read the 600 file) →
  captured the **resolved** creds from the running backend instead (documented in
  `DEPLOY_ISOLATED.md`). Rollback = single `docker rm -f guvfx-wayond-listener` (isolated).
  No order_send, no E3, no arming, no auto-approval, no trading-service change. **E3 RED.**
  NEXT: migrate to an aged **GFX** session (session swap + container recreate, no code change).

- **2026-07-05 — TEMPORARY-PRODUCTION-ACCOUNT-DEPLOYMENT: listener deploy artefacts finalised (repo-only; deploy is operational).**
  Prepared the read-only listener for a PRODUCTION deploy under the authorised temporary
  operational exception (personal-account session while GFX ages; target stays GFX). Repo work
  (Claude): `deploy/wayond-listener/Dockerfile` (backend image + Telethon); finalised
  `docker-compose.wayond-listener.yml` (build, `--live --health-file`, healthcheck via new
  `check_wayond_listener` command, json-file log rotation, `restart: unless-stopped`, no ports);
  listener liveness heartbeat (`write_health` + periodic loop on the client loop, `--health-file`);
  production `RUNBOOK.md` (2FA prereq → fresh session → secret store → deploy → health/observability
  → un-armed provider → rollback); `docs/WAYOND_LISTENER_MIGRATION.md` (Personal→GFX = new session
  + secret swap + restart, NO code changes). +4 tests (write_health atomic/no-raise, healthcheck
  fresh/stale/missing). **The actual deploy is operational — requires Nuno (prod access + a fresh
  2FA session mint); Claude cannot deploy or log into Telegram.** Listener stays acquisition-only,
  provider UN-ARMED, no execution, no order, no auto-approval. 360 backend tests green. E3 RED.

- **2026-07-05 — LIVE-VALIDATION-CLEANUP-AND-READINESS: acquisition track LIVE-VALIDATED + cleaned.**
  Consolidation. Live validation succeeded on REAL Wayond via Nuno's aged personal account as a
  TEMPORARY engineering account: listener connected read-only, caught up **117 live messages**,
  classified each (ENTRY_SIGNAL/UPDATE/UNKNOWN/QUARANTINED), heartbeat=listening, dry-run wrote
  NOTHING, no provider armed. Cleanup VERIFIED (all read-only checks): `~/.guvfx/` empty (all
  session files deleted); no `.session` credential tracked in git; no StringSession VALUE
  committed (only env-var refs); dev DB fully clean (0 providers, 0 ARMED, pers-val gone, 0 rows
  in AcquiredMessage/PendingApproval/SignalUpdate/MessageAmendment); `main` clean at 9743e43.
  Personal validation session revoked by Nuno. **Acquisition track is feature-complete, hardened,
  fixture- AND live-validated.** The ONE remaining gate to a production listener: an AGED GFX
  session that survives reuse (age 7–14 days clean, then one hardened attempt — Phase 2 proved
  the code holds a session on a trusted account). No repo behaviour change, no deploy, no arming,
  no order. E3 unaffected (RED).

- **2026-07-05 — TELEGRAM-SESSION-VALIDATION-STRATEGY: Phase 2 first (engineering validation account).**
  GFX has aged only ~2 days since the 2026-07-03 kills (below the 3–4 day floor) and the packet
  allows ONE login attempt — so Nuno chose to NOT spend the GFX shot now: validate the LIVE
  pipeline via a TEMPORARY engineering validation account now, and retry GFX properly-aged later.
  New governance doc `docs/ENGINEERING_VALIDATION_ACCOUNT.md` (temporary/non-production/no-secrets/
  no-arming/read-only/revoke-after). Phase-2 validation is operational (Nuno runs the live login —
  RED credential, interactive): provision the engineering account (separate 0600 session file,
  frozen device fingerprint matching the listener), verify persistence (get_me + reload + survives
  reuse via the scratch diag/verify scripts, now SESSION_PATH-configurable), then
  `run_wayond_listener --live --dry-run` (preview only, no writes, no arming). GFX Phase-1 attempt
  DEFERRED (ages for a proper single shot in ~1–2 weeks). No repo behaviour change, no deploy, no
  arming, no order. E3 unaffected (RED).

- **2026-07-04 — SIGNAL-ACQUISITION-LISTENER-DRYRUN-VALIDATE: end-to-end fixture validation (repo-only, no Telegram).**
  Proves the repo-built listener end-to-end against the CERTIFIED corpus as fixtures (no
  Telegram, no connect). New `listener/fixtures.py` `corpus_to_fixtures` (derives listener
  message dicts from the 21 certified real messages — real text + demo transport metadata,
  nothing fabricated) + `dump_wayond_fixture` command (writes a fixture JSON for
  `run_wayond_listener --fixture`). New `tests_listener_validate.py` (5 tests): dry-run
  writes NOTHING (21 previews: 8 ENTRY_SIGNAL / 10 UPDATE / 3 UNKNOWN); replay → 21
  AcquiredMessages (8 INTAKEN, 10 UPDATE, 3 QUARANTINED), watermark advances to the last id,
  edited entry surfaced flagged + PENDING (never auto-traded); second replay idempotent
  (deduped); certification stays CERTIFIED; replay creates **no ExecutionJob**. 361 backend
  tests green. Fixture-mode only — no login/session/API call, no deploy, no arming, no order.
  E3 unaffected (RED).

- **2026-07-04 — SIGNAL-ACQUISITION-LISTENER-BUILD: read-only Telegram listener (repo-only, NOT connected).**
  Builds the listener adapter in-repo so deployment is fast once the aged GFX session is
  ready — NO real Telegram login/session/API call, fake-Telethon tests only. New
  `signal_intake/listener/` package (Telethon-FREE, pure): `normalize.py` maps a Telethon
  message OR a fixture dict to the dispatcher dict `{message_id, chat_id, text, date,
  reply_to_message_id, edit_date, media}` (media = a small REFERENCE, never bytes);
  `adapter.py` `WayondListener` — provider lookup by chat_id, `acquire_raw` (feeds ONLY
  `acquire_message`; dry-run previews via `classify` without writing), watermark
  `catch_up` (iter_messages min_id), flood-wait handling (sleeps requested seconds,
  detected by class name — no Telethon import), heartbeat logging, and `run(client,
  events)` wiring read-only NewMessage + MessageEdited handlers (client/events injected;
  the single lazy Telethon import lives in the command). New `run_wayond_listener` command
  (default fixture/dry-run; `--live` guarded, needs env session + creds, never exercised by
  tests). Deploy skeleton `deploy/wayond-listener/` (compose service + runbook) — NOT
  deployed; gated on an aged authorised session. 18 fake-Telethon tests incl. a boundary
  proof (no execution import, no send/download call, sink = acquire_message only). 348
  backend tests green. No listener run, no deploy, no arming, no order. E3 unaffected (RED).

- **2026-07-03 — WAYOND-EDIT-DIFF: immutable edit-diff handling (repo-only, no order).**
  Closes the last edit blind spot before the listener. New `MessageAmendment` model
  (migration 0006) — an **immutable linked ledger** of an edit to an already-acquired
  message: `original` FK (never overwritten), `edited_text`, `edit_date`, `reparsed_kind`,
  `changed_fields` diff, `approval_reflagged`, unique `(original, edit_hash)` (idempotent).
  Dispatcher (`acquisition.py`): when an edit (`edit_date`) or changed body arrives for an
  existing `(provider, message_id)`, `_record_amendment` re-parses the edited text, diffs
  entry/SL/TP vs the original approval, and — if changed — **flags that approval
  `source_edited=True` for human RE-REVIEW (never auto-applies the edited values, never
  reverts/actions)**; an edited update records an **amended `SignalUpdate`** (record-only).
  True unchanged duplicates still dedup (no amendment). Admin: read-only amendment ledger.
  +6 tests (same-values→amendment-no-reflag, SL-change→reflag+original-value-preserved,
  idempotent, amended-update-record-only, true-dup→no-amendment) + ADR-009 allowlist updated.
  336 backend tests green; corpus still CERTIFIED (parser/corpus untouched); ADR-009 boundary
  intact. Repo-only. E3 unaffected (RED).

- **2026-07-03 — WAYOND-EDIT-MEDIA-DISPATCHER: implement ratified edit/media/reply policy (repo-only, no order).**
  Implements the policy ratified in PR #72. Dispatcher (`acquisition.py`): **media is now
  EVIDENCE, not a hard blocker** — a text-bearing media message is parsed (media reference
  retained in `raw_payload.media_evidence`; bytes never stored); a **screenshot-only** message
  (media, no parseable text) → QUARANTINED `media_only`. **Edited** messages are never
  auto-intaken: an edited tradeable signal → the existing human-approval gate (INTAKEN,
  `reason=edited_review`) FLAGGED via new `PendingSignalApproval.source_edited` (migration
  0005, admin list/filter/readonly) — still human-gated, never auto-traded; an edited update
  → recorded `SignalUpdate` (`raw_payload.edited=true`), never acted. **Reply-quoted updates**
  link to the originating `AcquiredMessage` via `reply_to_message_id`
  (`raw_payload.origin_acquired_id`, soft link). Originals immutable (an edit is a new record).
  Certification `classify()` updated to mirror the new policy (edit/media no longer force
  quarantine; only no-text does) + drift guard extended (screenshot-only case). Corpus V1
  relabelled: edited EURGBP entry QUARANTINED→ENTRY_SIGNAL, edited AUDCAD update
  QUARANTINED→UPDATE, 4 position-screenshot updates now `media:true` (still certify UPDATE).
  Corpus CERTIFIED (21, 0 unsafe/fail), confidence MEDIUM (QUARANTINED+UNKNOWN coverage now
  absent — edited msgs no longer quarantine; no screenshot-only/malformed real message).
  +5 dispatcher tests (edited-entry-flagged / media-only-quar / media+text-parsed /
  edited-update-recorded / reply-link). 285 signal_intake+intelligence+execution tests green.
  ADR-009 boundary intact (no execution import, no order_send). Repo-only. E3 unaffected (RED).

- **2026-07-03 — WAYOND-EDIT-AND-MEDIA-POLICY: dispatcher policy DESIGN (PROPOSED, no code).**
  Design-only governance doc `docs/WAYOND_EDIT_MEDIA_POLICY.md` resolving how the pipeline
  should treat edited / media-bearing / reply-quoted Wayond messages (exposed by corpus V1).
  Grounded in `acquisition.py`: no entry is ever auto-traded (PENDING approval + RBAC),
  updates are never acted on, and dedup-by-message_id silently swallows edits to
  already-ingested messages. Recommended MVP: **media → evidence not a hard block** (parse
  text-bearing media; quarantine screenshot-only), **edited entries → human review /
  edited updates → recorded** (never overwrite the immutable original), **reply-quoted
  updates → linked via reply_to_message_id**; edit-diff detection deferred. Challenges the
  PM default where the approval gate makes "quarantine media entries" over-conservative,
  and flags that scope-item-10 (edit changes entry/SL/TP) is only partly achievable under
  current dedup. Amber change → **Nuno ratification required before any dispatcher code**.
  GOVERNANCE PR held open. No code, no dispatcher change, no order. E3 unaffected (RED).

- **2026-07-03 — WAYOND-CORPUS-SEED-FROM-SCREENSHOTS: corpus V1 from real messages + parser fixes (repo-only, no order).**
  Extracted **21 real Wayond messages** from Nuno's screenshots (24 Jun–02 Jul) into the
  certified corpus (`wayond_corpus.json`) — 7 entries (6 BUY + 1 SELL), 9 updates (TP-hit /
  move-SL / SL-hit), 2 edited (quarantined), 2 chatter, 1 NFP warning. Certification surfaced
  **2 real parser gaps**: (1) SELL messages use `STOP LOSS:` / `TP1:` with COLONS (BUY do
  not) → the `Stop Loss\s+<num>` regex MISSED the real SELL entry (UNSAFE/FAIL); (2) `SL hit`
  updates were UNKNOWN (DEGRADED). Fixed with minimal, targeted parser changes
  (`intelligence/telegram_source.py`): optional colon in `_SL_RE`/`_TP_RE`, new `_SL_HIT_RE`
  → SL_HIT update. **Confidence LOW→MEDIUM** (0 unsafe, 0 fail, 0 degraded; only UNKNOWN
  coverage missing — no malformed message was in the screenshots). Finding flagged: Wayond
  EDITS some signals (EURGBP entry, AUDCAD update) → edit guard drops them; and many TP-hit
  updates carry MT5 position screenshots (classified by text; media handling is a dispatcher
  concern). Every extracted message is now a permanent regression case; +explicit parser tests
  for the SELL colon format + SL-hit. 115 signal_intake+intelligence tests green, no
  regressions. Repo-only. E3 unaffected (RED).

- **2026-07-03 — WAYOND-CORPUS-SEED-READY: real-message intake workflow (repo-only, no order).**
  Removes friction from seeding the certification corpus. New `signal_intake/staging.py`
  — `parse_paste` (split on `---`, optional leading `@type/@edit/@media/@reply/@stale/@id`
  directives), `stage_entries` (classify + PROPOSE type from the parser's observed result
  + flag needs-review: unconfirmed / a proposed trade / a signal-shaped message NOT read
  as tradeable), `promote` (append ONLY confirmed entries; skip unconfirmed/duplicate/
  bad-type). New `stage_wayond` (paste→draft, never the permanent corpus) and
  `promote_wayond` (reviewed draft→corpus) commands. `certification.py` gains
  `certification_confidence()` (LOW until ENTRY_SIGNAL+UPDATE have real PASSING examples;
  MEDIUM partial; HIGH full coverage) surfaced by `certify_wayond`. NO fabricated messages
  — proposals are review aids, ground-truth `expected_type` is Nuno's; only confirmed real
  messages promote. 14 new tests (paste parsing, staging/review flags, promote confirmed-
  only + dedup, confidence levels). Real corpus confidence = **LOW** (only the 1 WARNING).
  `docs/WAYOND_CERTIFICATION.md` documents paste format + commands. 79 signal_intake tests
  green. Repo-only. E3 unaffected (RED).

- **2026-07-03 — WAYOND-PARSER-CERTIFICATION: replay + certification framework (repo-only, no order).**
  Permanent regression suite that certifies the `wayond_v1` parser against **real**
  Wayond messages (no Telegram, no session, no listener, no order). New
  `signal_intake/certification.py` — a pure `classify()` mirroring the dispatcher's
  content precedence + a `_verdict()` whose only UNSAFE outcomes are a *missed*
  ENTRY_SIGNAL or a non-signal *read as* tradeable; `build_report()` over a corpus.
  New `wayond_corpus.json` (**real observed messages only**, seeded with the one
  genuinely-seen message — the "NFP today at 14:30 CET" WARNING — which certifies as
  safely quarantined). New `certify_wayond` command (prints report; exits non-zero on
  any UNSAFE/FAIL → CI-gate ready). Taxonomy: ENTRY_SIGNAL / UPDATE / WARNING /
  CHATTER / STALE / QUARANTINED / UNKNOWN. 11 tests incl. a **drift guard** proving the
  pure classifier agrees with the *real* dispatcher (`acquire_message`) on the safety
  group. Parser UNCHANGED (no real message required a change yet). 65 signal_intake
  tests green. Corpus grows as Nuno supplies messages; `docs/WAYOND_CERTIFICATION.md`
  is the operator guide. Repo-only. E3 unaffected (RED).

- **2026-07-03 — TELEGRAM-SESSION-RUN outcome + device-fingerprint hardening (repo-only, no order).**
  Ran the provisioning helper against the real dedicated **GFX** account (Nuno's hands).
  Login succeeded and a valid 0600 `StringSession` was minted, but Telegram **de-authorised
  it server-side within minutes** and logged the account out everywhere — reproducible with
  2FA OFF *and* ON. Root cause = brand-new account anti-abuse, **not** a code bug (session
  string verified complete: `has_auth_key=True`, `authorised=False` on reuse). Account is a
  real eSIM and recovers via official-app SMS re-login (not banned). `verification_result =
  FAIL/BLOCKED`; full trace in memory `project-telegram-session-deauth`. Remediation = age
  the account 7–14 days + stable device fingerprint, then retry. Hardening shipped here: the
  provisioning command now passes an **env-driven, stable device fingerprint**
  (`TELEGRAM_DEVICE_MODEL/SYSTEM_VERSION/APP_VERSION`, defaults applied) to `TelegramClient`
  instead of Telethon's defaults (which flag fresh accounts) — additive, +2 tests (15 total).
  Runbook updated with the aging + fingerprint + IP-consistency guidance. **No listener, no
  ingestion, no provider arming, no deploy, no order.** E3 unaffected (RED).

- **2026-07-03 — TELEGRAM-ACCOUNT-PROVISIONING-HELPER: interactive session generator (repo-only, no login run).**
  Safest/fastest path for Nuno to mint + verify the dedicated **GFX** Telegram account's
  Telethon StringSession. New `provision_telegram_session` management command
  (`signal_intake/management/commands/`): reads `TELEGRAM_API_ID/HASH` from env
  (never CLI/never logged), **lazy-imports** Telethon (backend/image stay lean —
  Telethon lives only in new `backend/requirements-telegram.txt`), logs in
  interactively (Nuno types the code Telegram sends — no 2FA yet, deferred),
  verifies identity via `get_me()` + optional read-only Wayond check
  (`get_entity` + one `get_messages(limit=1)` → latest id only, no content),
  prints **only safe metadata** (telegram user id, display name, username, chat
  title, chat id, latest message id — **never phone, never session**), and writes
  the session to a **600-mode** file (default `~/.guvfx/telegram_gfx.session`).
  The session is **NOT printed** unless an explicit `--print-secret` flag is given,
  which fences it in two loud SECURITY-WARNING banners. Cleanup/revoke note printed
  (terminate session on the GFX account → re-run; enable 2FA after verification).
  Operator runbook `docs/TELEGRAM_PROVISIONING.md`. 7 new tests (chmod-600,
  print-secret gating both branches, metadata excludes phone/session, missing-creds
  + missing-Telethon guards) — all run **without** Telethon or any real login.
  **Repo-only — no listener, no ingestion, no provider arming, no deploy, no order.**
  E3 unaffected (RED).

- **2026-07-01 — SIGNAL-ACQUISITION-MVP-CORE: provider platform Phase 1 (repo-only, no order).**
  The acquisition core (no Telegram/listener/session). New `signal_intake` models
  `SignalProvider` (status lifecycle ONBOARDING/ARMED/PAUSED/INACTIVE/RETIRED,
  chat-id trust boundary, per-provider window), `ParserProfile`, `AcquiredMessage`
  (append-only ledger + `(provider,message_id)` dedup key), `SignalUpdate`
  (recorded-not-acted); nullable `PendingSignalApproval.provider`; additive migration
  `0004`. New `signal_intake/parsers.py` registry (`wayond_v1` wraps the deployed
  Wayond parser) and `acquisition.py` **pure fail-closed dispatcher** `acquire_message`
  (dedup → armed → 5–10 min staleness → edit/media/empty guard → parser dispatch →
  route: tradeable→intake / update→recorded / else→quarantine → watermark +
  last_signal_at). `onboard_provider` command; admin (providers editable, ledgers
  read-only). The dispatcher imports **only** signal_intake (+ the shared parser) —
  never `execution` (AST-guarded), never `order_send`. Existing manual ladder
  unchanged; `intake_parsed` additively accepts a provider. 14 new tests (intake,
  dedup, stale, edit/media/empty/unknown quarantine, update, non-armed drop, unknown
  parser fail-closed, watermark, onboard, boundary). 203 signal_intake+execution
  tests green on local Postgres. Repo-only — **NO deployment, no listener, no order.**

- **2026-07-01 — SEC-CREDENTIAL-ROTATION: credential-lifecycle audit + rotation framework (repo-only, no order).**
  Last pre-E3 must-fix. **Repo-only + docs** — no prod secret rotated, no secret/`.env`
  printed (only credential *surfaces* reviewed). New `core.audit.log_credential_event`
  (`CREDENTIAL_CREATED/ROTATED/REVOKED`, secret-sanitising, fail-open) wired into
  `provision_shadow_worker` (create/rotate/revoke now audited — item 8, closes the
  WorkerIdentity-lifecycle gap). `docs/CREDENTIAL_ROTATION.md` covers all 10 items:
  redacted secret inventory (S1–S7, exposed ones flagged for Nuno rotation),
  zero-downtime worker-token dual-identity rotation, agent-token 2-sided rotation,
  Fernet `MultiFernet` re-encrypt approach, **legacy `X-Worker-Token` disablement plan**
  (`ENABLE_LEGACY_WORKER_TOKEN` defaults `true` — documented, NOT silently flipped),
  emergency revoke, leak-incident playbook, downtime summary, and the Nuno-held prod
  actions. 4 new tests (create/rotate/revoke audited, no secret in metadata, fail-open).
  190 execution+signal_intake+core tests green on local Postgres. No migration, no
  order_send, no deployment, no credential change.

- **2026-07-01 — E3-APPROVAL-RBAC: dedicated signal-reviewer permission (fail-closed, no order).**
  Pre-E3 must-fix (gap Area 6/7). Approving/rejecting a signal now requires the
  dedicated `signal_intake.review_signals` permission — plain Django-admin/staff
  access is no longer sufficient. Service layer enforces fail-closed
  (`services.can_review`: None/inactive/unauthorised/error → deny) with a persisted
  `APPROVAL_DENIED` audit written BEFORE the atomic block (survives the raise);
  admin approve/reject actions are hidden from unauthorised staff (Django action
  `permissions=["review"]`) with the service check as defence-in-depth. Who
  approved/rejected is recorded (reviewer FK + SIGNAL_APPROVED/REJECTED audit actor).
  New `manage.py grant_signal_reviewer <user> [--revoke]` (idempotent). Migration
  `signal_intake.0003` (Meta permission + audit event choice). **Behaviour change
  (intended):** unauthorised approvals now fail — operators must be granted the
  permission (E3 checklist item). 10 new tests; 182 execution+signal_intake tests
  green on local Postgres. No order_send, no deployment.

- **2026-07-01 — E3-NODE-ASSIGNMENT-ENFORCEMENT: terminal-node gate + audit (flag-gated, no order).**
  Pre-E3 must-fix (gap Area 5/8). Promotion now optionally requires the account to
  have an operator-declared **ACTIVE** `TerminalNode`: new
  `risk_controls.node_assignment_block_reason` (control 0) inside the fail-closed
  `evaluate_promotion_risk` — blocks `account_node_unassigned` / `node_not_active`
  with the persisted `PROMOTION_REJECTED` audit. **Flag-gated `RISK_REQUIRE_TERMINAL_NODE`,
  default OFF** (prod accounts currently ride the legacy null-node route — behaviour
  preserved; enable at E3 after the audit passes). New read-only
  `manage.py audit_node_assignments [--strict]` reports PASS/FAIL per account (the
  pre-E3 checklist item). 6 new tests (flag-off unchanged, unassigned/draining
  blocked + audited, active promotes with node snapshot, audit report + strict exit);
  172 execution+signal_intake tests green on local Postgres. No migration, no
  order_send, no deployment.

- **2026-07-01 — E3-RUNTIME-RISK-CONTROLS: pre-E3 runtime risk gates (shadow-only, no order).**
  Additive, fail-closed risk controls required before any demo-live path. New
  `execution/risk_controls.py` (`evaluate_promotion_risk`, pure/fail-closed) wired
  into `signal_promotion._validate`: per-account + per-symbol exposure, max
  open-positions/active-jobs, daily drawdown, and concurrent-position enforcement —
  each blocks via `PromotionRejected` (persisted `PROMOTION_REJECTED` audit). Exposure
  counts BOTH paths on the shared account (open `Trade`s + `PROMOTED`-plan leg lots,
  per Blueprint 06). Runtime staleness re-check added to the worker's
  `handle_shadow_job` (refuses a stale shadow job before `order_check`); `signal_timestamp`
  propagated into the shadow payload. No `order_send`, no E3 LIVE mode, no kill-switch
  change; within-limit fresh promotions and timestamp-less dry-run jobs are unaffected.
  10 new tests (each control blocks + clean promotes + fail-closed + worker staleness);
  166 execution+signal_intake tests green on local Postgres. No migration. Caps are
  env-overridable — see `backend/execution/RISK_CONTROLS.md`. Repo-only — **NO deployment**.

- **2026-07-01 — 006D-TZ-PROBE: broker-server timezone verified UTC+3 (summer), read-only, no order.**
  Nuno-authorised read-only probe (Option A): compared a fresh EURUSD M1 server-time
  bar (existing `/mt5/snapshots/rates`) against NTP-synced UTC on TradersWay-Demo
  (acct 1121106). Result: server = **UTC+3** (EEST) — raw diff 10776 s ≈ 3h, fresh
  bar (24 s residual, market open), NTP-synced host, VALID. No order/order_check/
  account change/restart/code change. Evidence: `docs/evidence/broker_timezone_evidence_v1.md`.
  **DST caveat:** summer offset only; re-probe after the late-Oct-2026 DST transition
  for the winter (likely UTC+2) entry. This clears one of the three E3 hard blockers
  (Blueprint doc 06 + Nuno E3 sign-off remain).

- **2026-07-01 — OPS-OBSERVABILITY-FOUNDATION: execution lifecycle logging + metrics (additive, no order).**
  End-to-end structured visibility for every shadow execution attempt, ahead of E3.
  A single `correlation_id` is minted at signal receipt and propagated
  approval → plan → shadow-job payload → worker (new nullable columns
  `signal_intake.0002`, `execution.0008`; fresh-id fallback for old rows). New
  `core/observability.py` emits single-line JSON to `guvfx.execution.lifecycle`
  and `guvfx.execution.metrics` (root console → stdout → Loki-ready), fail-open.
  9 lifecycle stages instrumented (signal_received → cleanup_complete) across
  `signal_intake.services`, `signal_planning`, `signal_promotion`, `views.next_job`,
  and the worker's `handle_shadow_job`. Metrics: worker_claim_latency, shadow_queue_depth,
  mt5_response_latency, validation_success/failure (→ success rate downstream),
  execution_duration. NO change to execution/risk/trading logic; no `order_send`;
  the worker AST guard (no order_send / no MetaTrader5) still holds. 9 new
  observability tests (correlation propagation + stage/metric emission); 157
  execution+signal_intake+core tests green on local Postgres. See
  `docs/OBSERVABILITY.md` (schema + deployment + rollback). Repo-only in this
  packet — **deployment is a separate step**.

- **2026-07-01 — EXEC-E2b-PERSIST: managed shadow worker service (repo/infra-only, no order).**
  Converts the ad-hoc `E2b-DEPLOY-D2R` dry-run into a managed, restart-safe form.
  Adds `deploy/shadow-worker/`: a compose service `guvfx-mt5-shadow-worker` that
  `extends` the normal worker (inherits image/volumes/network/shared env), overrides
  ONLY the identity/token/flag (`MT5_SHADOW_WORKER=1`, distinct `MT5_WORKER_ID`,
  token via `${MT5_SHADOW_WORKER_TOKEN}` — no secret committed, fail-fast if unset),
  and adds `restart: unless-stopped`; a `verify_shadow_dryrun.sh` post-deploy check
  (one dry-run job → `order_check` only → asserts no order/ticket/deal, cleans up);
  and a README runbook with deploy + **rollback** notes. Adds
  `manage.py provision_shadow_worker` — idempotent create/revoke of the distinct
  shadow `WorkerIdentity` + `shadow_worker` grant, secret read from env (never a CLI
  arg) and never printed, refuses to reuse the normal worker id. 6 provision tests +
  the shadow-only/order_check guarantees (existing `ShadowPollGateTests` /
  `ShadowWorkerTests`). No change to the normal worker service; no bridge change; no
  migration; **NO production change** (deploy is a separate, gated operational action).

- **2026-07-01 — EXEC-E2b-R2: shadow-only worker claim mode (repo-only, no order).**
  Closes the blocker found at E2b-DEPLOY-D2 preflight: with the R1 code a
  dedicated shadow worker (`MT5_SHADOW_WORKER=1`) still claimed the executable
  `PLACE_TEST_ORDER`/`PLACE_ORDER` types (unconditional in `claim_worker_job`),
  so run persistently it could win a real order and route it to the live
  `order_send` path (→ real demo ticket), failing the D2 no-order gates. R2 makes
  shadow mode **shadow-only**: `claim_worker_job()` now branches — flag ON returns
  `claim_next_job("PLACE_ORDER_SHADOW")` and nothing else (no executable claims,
  no default SYNC); flag OFF keeps the exact pre-E2b 3-claim sequence. A dedicated
  shadow worker therefore **structurally cannot** place an order, and its poll
  rate drops to 1 claim/loop (~30/min at the default 2s sleep — well under the
  100/min throttle, no special sleep config needed). Tests rewritten: shadow mode
  claims only `PLACE_ORDER_SHADOW` / never the executable types / never default
  sync / single claim per loop; normal mode unchanged + short-circuit. 140
  execution+signal_intake tests green on local Postgres. No bridge change, no new
  migration. Worker + tests + docs only — **NO deployment**. Unblocks a re-run of
  E2b-DEPLOY-D2 (persistent shadow worker).

- **2026-07-01 — EXEC-E2b-R1: env-gate shadow-worker polling (repo-only, no order).**
  Fixes the E2b polling regression found during the E2b-DEPLOY-D1 dry-run: the
  worker's unconditional 4th `claim_next_job("PLACE_ORDER_SHADOW")` pushed its
  poll rate (~120/min) over the 100/min request throttle and the live worker
  looped on HTTP 429. The shadow claim is now **opt-in** behind `MT5_SHADOW_WORKER`
  (default OFF): `mt5_trade_ingest_worker` extracts the claim sequence into
  `claim_worker_job()`, which makes the `PLACE_ORDER_SHADOW` claim ONLY when the
  flag is set. The normal worker keeps its exact pre-E2b 3-claim sequence
  (`PLACE_TEST_ORDER` → `PLACE_ORDER` → default SYNC), so its request rate is
  unchanged and below the throttle. The next_job endpoint still independently
  gates shadow jobs on `worker_permissions.shadow_worker`. 9 new poll-gate tests
  (flag default-off / on, sequence unchanged in both modes, shadow claim position,
  short-circuit, env-flag truthy/falsey parsing); the E2b order_check-only /
  order_send-0× guarantees are untouched. 126 execution tests green on local
  Postgres. No new migration. Worker + tests + docs only — **NO deployment**; the
  dedicated shadow worker (flag ON) is deployed only by a separate, gated action.

- **2026-07-01 — EXEC-E2b: shadow worker + bridge mt5.order_check() dry-run (no order).**
  First MT5 execution rung — proves the full pipeline shadow job → worker → bridge
  → MT5 validation while guaranteeing `mt5.order_send()` is NEVER called. Worker
  (`backend/mt5_trade_ingest_worker.py`) gains `handle_shadow_job` + `agent_order_check`
  and a `PLACE_ORDER_SHADOW` claim; for `execution_mode=SHADOW` it POSTs
  `/mt5/order_check` (never the live `/mt5/order`), completes SUCCESS storing the
  validation (retcode/margin/latency — no ticket/deal/order id) or FAILED; LIVE and
  unknown modes fail closed. Bridge (`scripts/mt5_signal_bridge.py`) gains
  `shadow_order_check` + `/mt5/order_check` route: same demo validation
  (is_demo/trade_mode/symbol/lots) and the EXACT SAME MT5 request as
  `execute_demo_order`, then `mt5.order_check(request)` — never `order_send`.
  `execute_demo_order` is byte-for-byte unchanged (additive only). 15 tests
  (mocked MT5): order_check called once / order_send **0×**, shadow request ==
  live request, live path still calls order_send, demo enforcement preserved,
  invalid symbol / market-closed / non-demo / tick fail safely, worker routes
  SHADOW→order_check never live, LIVE/unknown fail closed, SUCCESS stores
  validation no ticket. 132 execution+signal_intake+strategies + governance green
  on local Postgres. No new migration. Backend/scripts only — **NO deployment**;
  no shadow worker runs until a WorkerIdentity is granted `shadow_worker` on the
  production box (a separate, gated operational action). E3 (real demo placement)
  remains gated.

- **2026-06-30 — EXEC-E2a: plan → suppressed, un-claimable shadow jobs (no order).**
  First rung creating real `ExecutionJob` records (under the recorded D17 sign-off),
  but suppressed and un-claimable. `execution.signal_promotion.promote_plan_to_shadow_jobs`
  promotes a PLANNED `SignalExecutionPlan` into one `PLACE_ORDER_SHADOW` job per leg
  (`execution_mode=SHADOW`), linking `ProposedOrderLeg.execution_job`. No order, no
  MT5/`order_send`/`order_check`/agent/network call, no executable PLACE_ORDER job
  (AST guard). Three suppression layers: the SHADOW flag, no deployed consumer
  requests the type, and a new **`next_job` endpoint guard** that serves shadow
  jobs only to a `worker_permissions.shadow_worker` caller (none exists). Added
  `ExecutionControl.signal_execution_mode` (SHADOW gate), `PROMOTED` statuses,
  `PromotionAuditEvent`, idempotency, re-validated gates, operator command
  `promote_plan_to_shadow`. 20 new tests; 142 execution+signal_intake+admin_ops+
  strategies + governance green on local Postgres. Backend only; no worker/bridge
  change, no deployment, no production access/migration. E2b (shadow worker +
  bridge dry-run on the production MT5 box) remains separately deployment-gated.
  Detail: `backend/execution/SHADOW_PROMOTION.md`.

- **2026-06-30 — EXEC-E1b-R2: fail-closed robustness cleanup (no order).**
  From the PR #48 review: `_signal_timestamp` now makes naive parsed timestamps
  timezone-aware (falls back to the aware `created_at`), so a naive Telegram
  date no longer raises `TypeError` during the staleness check; `_hold`/`_void`
  gained the same `IntegrityError`→existing-plan idempotency fallback as the
  PLANNED path; and invalid/NaN/Inf total-lot values now become a clean `HELD`
  (`volume_split_invalid`) instead of crashing. 11 new tests; 95
  execution+signal_intake + governance green on local Postgres. No schema change.
  Backend only; no production access/deploy/migration. E1b's no-order guarantee
  preserved (asserted).

- **2026-06-30 — EXEC-E1b: non-executable multi-leg demo execution plan (no order).**
  Added `execution.SignalExecutionPlan` + `ProposedOrderLeg` (non-executable —
  NOT ExecutionJobs, invisible to the worker claim path), `SignalSourceConfig`
  (per-source `auto_demo_execution_enabled`, default OFF), and `PlanAuditEvent`
  (append-only). Planner `execution.signal_planning.plan_demo_execution` reads an
  APPROVED `PendingSignalApproval`, carries `take_profits` through, splits into up
  to 3 legs (shared SL, one TP/leg) with a deterministic capped volume split,
  holds on missing SL/TP, voids on stale signal, and rejects on
  kill-switch/source-disabled/non-demo/symbol/per-group-cap — creating **no
  ExecutionJob, no order, no listener** (per-group caps, source+message
  idempotency, full signal→plan→leg audit). 27 new tests (incl. static no-order
  AST guard, `ExecutionJob.objects.count()` unchanged, worker-invisible); 84
  execution+signal_intake + governance all green on local Postgres. Operator
  entry: `manage.py plan_demo_execution`. Detail:
  `backend/execution/DEMO_EXECUTION_PLAN.md`. Backend only; no production
  access/deploy/migration. The EXECUTING rungs (E2 suppressed → E3 real demo)
  remain behind Nuno's recorded sign-off per the D17 governance gate.

- **2026-06-30 — EXEC-HARDEN-JOBS-R2: worker-action gating + clean kill-switch handling.**
  Gated the worker-protocol actions `next`(claim)/`complete` on
  `ExecutionJobViewSet` to validated worker credentials (or staff) via a new
  `IsWorkerToken` permission — ordinary authenticated users can no longer claim or
  complete jobs (closes the pre-existing claim-hijack). Translated
  `ExecutionKillSwitchEngaged` to a clean 503 on the `run_signal` and admin-retry
  endpoints and to a labelled clean skip in the H1/M5 schedulers, replacing
  unhandled 500s (no order is ever placed — the model guard fails closed first).
  Fixed the misleading `views.py` comment. 9 new tests + execution/signal_intake/
  admin_ops/strategies (84) + governance all green on local Postgres. No schema
  change. Backend only; no production access/deploy/migration.

- **2026-06-30 — EXEC-HARDEN-JOBS: lock down generic ExecutionJob creation.**
  Disabled the generic DRF write surface on `ExecutionJobViewSet`
  (`POST/PUT/PATCH/DELETE` → 405) so an ordinary authenticated user can no longer
  create or mutate an order-bearing job directly (pre-existing gap surfaced in the
  E1a review). `ExecutionJob`s now come only from sanctioned gated paths
  (strategy automation, `OpenTradeJobView`, `CreateDemoTradeJobView`, admin_ops
  retry). Order-defining serializer fields made read-only. Functional kill switch
  enforced at the **model layer** (`ExecutionJob.save()` blocks order-opening job
  types when `ExecutionControl.kill_switch_engaged` / `GUVFX_EXECUTION_DISABLED`),
  covering every creation path; `OpenTradeJobView`/demo endpoints fail closed with
  503; `CLOSE_TRADE` exempt (flattening). Single source of truth
  `order_creation_kill_reason`. 13 new tests + E1a/exec/strategies/governance all
  green on local Postgres. Removed 184 untracked iCloud ` 2.` duplicate strays that
  were breaking the migration graph (none git-tracked; `.gitignore` already lists
  the pattern). Backend only; no production access/deploy/migration.

- **2026-06-29 — EXEC-E1a: approval → ProposedSignalOrder bridge (no order).**
  Added `execution.ProposedSignalOrder` (non-executable candidate — NOT an
  `ExecutionJob`, structurally invisible to the worker claim path),
  `execution.ExecutionControl` (functional DB kill switch + signal-specific
  disable, replacing the MVP 501 stub), and `execution.ProposalAuditEvent`
  (append-only). Bridge `execution.signal_proposals.propose_order_from_approval`
  creates proposals only — places no order, queues no job, contacts no broker.
  Gates: approved-only, kill switch / env kill switch, demo-only, symbol
  allowlist, lot/daily/concurrent caps, one-per-approval. `/api/execution/kill-all/`
  is now functional (engages the DB switch; release is admin-only). Operator
  entry: `manage.py propose_signal_order`. 35 tests green on local Postgres
  (incl. `ExecutionJob.objects.count()` unchanged + static no-order AST guard);
  E0 ADR-009 boundary guard still green. Branch
  `feat/wayond-exec-e1a-proposed-orders` off `origin/main` (`49d5026`). Backend
  only; no production access, no deployment, no migration against production.
  Detail: `backend/execution/SIGNAL_PROPOSALS.md`,
  `docs/SECURITY_EXECUTION_MODEL.md` §1.4/§1.4a.

## Snapshot

- Date: 2026-06-28 (UTC)
- Canonical branch: `main` @ `148437ae8bc651f6eb818e15bd9a16cf9d3a993f`
- **Authority:** Notion is the source of truth for the full programme lifecycle
  (latest *GuvFX — Current State v0.52*). This file is the Git-side mirror and
  must be kept consistent with it. For the live data-acquisition frontier see
  [`docs/PROGRAMME_STATE.md`](PROGRAMME_STATE.md).
- Current governance merge: `c17b7b8` — PR #31 *Add governance convergence
  foundation* merged into `main`. This introduced the scoped Claude rules,
  authority/packet boundaries, the secret scanner + governance Make/CI gate, the
  Notion map, the evidence convention, and the active-packet pointer.
- Documented production routes: `https://guvfx.com` (frontend),
  `https://api.guvfx.com` (backend API), `https://guac.guvfx.com/guacamole/`
  (Guacamole MT5 desktop). These are the routes recorded in `docs/RUNBOOK.md`;
  route availability and live production health were **not probed** by
  GFX-PKT-004A or its R1 remediation.
- Research/data foundation: PR #32 and PR #33 are merged to `main`
  (`80ef2f8`), establishing the DuckDB research foundation and the versioned
  market-data contracts (GFX-PKT-005B / R1 / R2).
- **Synthetic market-data foundation (GFX-PKT-006C arc) — COMPLETE & MERGED.**
  006C + R1 + R2 + R3 + R4 + R4-R1 + **R4-R2** are all merged to `main`; the final
  R4-R2 (UTC-instant constructor/evidence reconciliation) merged via **PR #36**, so
  `main` is at `148437ae`. This delivered strict contracts, immutable raw landing
  with SHA-256/idempotency/quarantine, the `VERIFIED` timezone gate, synthetic M1
  bid-OHLC publication, one arbitrary-length-safe/immutable/unhashable UTC-instant
  primitive, and ordinary-quarantine provenance. It is **synthetic-only** — no real
  data, NAS, broker, agent acquisition or deployment lives in this repository.
- **LIVE PROGRAMME FRONTIER — real market-data acquisition (006D).** The active
  frontier is **NOT in this repository**. It runs in the dedicated private repo
  `nunoamaral-hue/guvfx-windows-history-agent` (`main` `46c81057…`; A0/A1/A2/A2-P1
  merged) plus a ladder of governed read-only probes executed over SSH/Tailscale
  against the Windows VPS MT5 terminal. All probes to date have PASSED: package
  import (P0/P1), terminal lifecycle (P2), session-dependent runtime accepted
  (H0/H1/ADR-DATA-017), source identity (P3), and history retrieval (P4: 6 EURUSD
  M1 rows). **First durable raw object (P5) — DONE (2026-06-28):** S1 provisioned the
  approved `GuvFXData` store and the first real GuvFX market-data object + provenance
  manifest are now published and SHA-256-verified there (immutable, content-addressed,
  idempotent). The next real gate is **broker-server timezone verification** before any
  normalisation or broad backfill. Full map: [`docs/PROGRAMME_STATE.md`](PROGRAMME_STATE.md).
- **Capability (Notion Capability Registry, v0.52):** 1 of 10 domains GREEN
  (*Trading* — production, live order path exists today); the other 9 AMBER. The
  *Market Data & Research Platform* domain is the weakest and gates strategy quality.

## Verified current state

Facts supported by code, Git history, or CI in this repository:

- Monorepo with a Django + DRF backend (`backend/`) and a Next.js frontend
  (`frontend/`); see `docs/ARCHITECTURE.md`.
- Backend local apps registered in `backend/guvfx_backend/settings.py`
  (`INSTALLED_APPS`): `users`, `core`, `trading`, `strategies`, `backtests`,
  `analytics`, `ai_helper`, `execution`, `hosting`, `mt5`, `wims`,
  `intelligence`.
- GuvFX/WIMS producer–consumer boundary is implemented: `intelligence` packages
  inputs into transient envelopes and delivers them; `wims` consumes via
  `ConsumptionContract`. WIMS never imports `intelligence` (ADR-009 boundary,
  documented in `backend/intelligence/README.md` and `backend/wims/README.md`).
- Auth is cookie-based JWT (`users.auth_cookie.CookieJWTAuthentication`) with
  DRF default permission `IsAuthenticated`; `USE_TZ = True`, `TIME_ZONE = 'UTC'`.
- Governance/evidence layer is present on `main` as of `c17b7b8` (PR #31):
  `.claude/rules/`, `scripts/check_no_secrets.py`, `tests/test_no_secrets.py`,
  `evidence/`, `packets/`, `make governance-check`.

## Active feature work

- **Flow A (`flow-a-shadow` branch)** — a shadow, execution-suppressed signal
  pipeline (`backend/flow_a/`: `signal_intake`, `candidate`, `evaluation`,
  `quality_gate`, `suppression`, `pipeline`, `replay/`, and the
  `run_flow_a_shadow` management command). It runs in shadow mode only and is
  **not** merged into `main` and **not** promoted to paper or live trading.
  Treat it as research/validation work bounded by its own branch and governance
  path.

## Known gaps and blockers

Current, evidenced items only:

- **Storage provisioned; first real object stored (S1 + P5 done 2026-06-28).** The
  approved `GuvFXData` root is live (validated by `scripts/check_data_root.py`) and
  the first immutable raw object + manifest are published & SHA-verified there.
  Backups: Phase-1 NAS-local (RAID) per sponsor decision; offsite deferred.
- **Broker-server timezone is UNVERIFIED** for the demo source (TradersWay-Demo) —
  **this is now the active gate.** MT5 bar times are broker-server time, not
  guaranteed UTC; no offset may be hardcoded and no normalised dataset may be
  published, and no broad backfill started, until this is evidenced (a Red probe).
- **MT5 runtime is desktop-session dependent** (autologon/kiosk console) per
  ADR-DATA-017; a true headless/service-managed model is unproven and deferred.
- **Live Trading path governance gap:** the GREEN *Trading* domain runs a real
  order path today (Windows bridge), governed by the legacy programme; Blueprint
  doc 06 requires reconciling it with the target execution architecture before any
  execution-layer packet — not yet done. Safety reference (how to stop it now,
  single points of failure, recovery): [`docs/LIVE_TRADING_RISK_WATCH.md`](LIVE_TRADING_RISK_WATCH.md).
- Local `make check` cannot complete on a machine without a `backend/.venv` and a
  reachable PostgreSQL (`127.0.0.1:5432`); backend Django tests need a running
  PostgreSQL. GitHub Actions is the approved full-integration gate.
- MT5 mouse input via Guacamole has been observed to be unreliable (clicks
  intermittently drop while keyboard navigation works); see
  `docs/KNOWN_ISSUES.md`.

## Last known green checks

Kept distinct: historical local evidence vs. current governance CI evidence.

- **Historical (2025-12-15):** Backend GitHub Actions CI (Django tests) and
  Frontend GitHub Actions CI (lint + build) reported green, with `make check`
  green locally at that time.
- **Current governance CI (2026-06-23):** GitHub Actions push run for merge
  `c17b7b8` (PR #31) — jobs `governance`, `backend`, and `frontend` all
  succeeded.

## Production operations

Production runs behind Traefik with Let's Encrypt TLS on a VPS; the
GuvFX backend/frontend/Postgres stack and the Guacamole + MT5 desktop stack are
operated separately. Do **not** duplicate the full procedure here — see
`docs/RUNBOOK.md` (sections "VPS Production (GuvFX)" and "RUNBOOK — MT5 Free
Desktop") for the authoritative restart, verification, and handoff-mount steps.
This document does not assert live-trading readiness; promotion to paper or live
follows the governance decision path, not status notes.

## Owners

- PM: Nuno Amaral
- Active coder: Nuno (current) → Clive (next)

# STATUS — MT5 Free Desktop (XRDP + VNC)

**Overall status:** ✅ STABLE / OPERATIONAL  
**Last verified:** 2025-12-18 (UTC)

### What is running
- XRDP listening on `:3389`
- xrdp-sesman listening on `:3350`
- Xvfb + Openbox running on `DISPLAY=:99` (VNC fallback)
- XRDP Xorg session allocated dynamically (e.g. `DISPLAY=:10`)
- MetaTrader 5 (Wine) auto-starts inside XRDP session

### Verified checks
- `terminal64.exe` running under user `mt5free`
- `wmctrl` lists:
  - `MetaTrader 5 - Netting`
  - `Login` window
- Guacamole RDP connection shows MT5 UI

### Persistence
- Wine prefix persisted via Docker volume: `/home/mt5free/.wine`
- Autostart script persisted via bind mount: `/home/mt5free/bin/autostart-rdp.sh`

### Notable guarantees
- No manual MT5 launch required
- No XRDP password re-entry after container restart
- Safe to rebuild container without losing MT5 state

## Incident Log

### 2026-03-17 — Traefik Stale Backend Routing (API Auth Failure)

**Classification:** Operational issue (routing layer) — NOT an architectural failure.

**Issue:** Intermittent API authentication failure due to inconsistent backend routing.

**Symptoms:**
- Browser login failure ("Failed to fetch")
- Intermittent 502 Bad Gateway responses
- CORS preflight failures
- Inconsistent API responses across requests

**Root Cause:** Traefik routing table contained multiple backend targets — one valid container IP and one stale (dead) container IP from a previous deployment. Requests routed to the stale IP returned 502 errors, causing an auth failure cascade.

**Affected Component:** `api.guvfx.com` → `guvfx-backend` service (Traefik routing layer only).

**Resolution:**
```bash
docker compose down --remove-orphans
docker compose up -d
```
This removed stale containers, rebuilt the Docker network, refreshed Traefik service discovery, and eliminated invalid backend targets.

**Validation:**
- CSRF endpoint: 10/10 success
- OPTIONS login preflight: 10/10 success
- Browser login: confirmed working
- API responses: stable

**Architecture Impact:** NONE
**Infrastructure Impact:** NONE
**Status:** RESOLVED

## Documentation Governance Mapping

This repository uses the following canonical mapping:

| Governance Reference | Canonical File |
|---|---|
| `GUVFX_IMPLEMENTATION_LOG.md` | `docs/STATUS.md` |
| `GUVFX_PLATFORM_STATE.md` | `docs/STATUS.md` (shared responsibility) |
| `GUVFX_TERMINAL_FARM_RUNBOOK.md` | `docs/RUNBOOK.md` |
| Incident / edge-case tracking | `docs/KNOWN_ISSUES.md` |

All references to `GUVFX_*` documents map to these files. The `docs/` directory is the single source of truth. Do not create duplicate documents with alternate naming or introduce parallel canonical structures.

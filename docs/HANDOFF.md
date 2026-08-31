# HANDOFF — live frontier pointer (2026-06-27)

## 2026-08-31 — Beta acceptance polish: POLISH_COMPLETE_CATALOGUE_APPROVAL_PENDING

- **Scope / decision.** Three bounded post-acceptance objectives: (A) capture+certify Pepperstone for Broker
  Catalogue V1, (B) make the native launcher windowless, (C) fix the "Broker account: Not yet" contradiction.
  Plus (D) preserve natural acceptance evidence and (E) regression. Merged PR #398 (main `3f429f8`).
- **Verified fact vs assumption.** *Verified:* C fixed + live (beta ws20 now `PepperstoneUK-Demo · ***344`);
  Pepperstone candidate captured + sanitised (RULE-11 positive control) + portable + integrity + golden-neutral;
  A5 approval primitive DARK + PENDING id=1 registered; B windowless binary compiles GUI(2) (RULE-11 control);
  full backend/target suites green; E regression intact. *Assumption / NOT re-driven:* the Pepperstone `<=10s`
  behavioural resolution timing is the already-proven IS6 preseed mechanism inferred from provenance (candidate came
  from a working PepperstoneUK-Demo connection) — a live interactive (xfreerdp) Pepperstone measurement was not
  performed; `servers.dat` is opaque so a plaintext broker-name needle is not a reliable presence test.
- **Two Nuno-gated next actions (both prepared, neither performed — human gates):**
  1. **Catalogue promotion.** Approve `ArtefactApproval id=1` (kind `broker_servers_dat`, ref `pepperstone/v1`, SHA
     `AFD6D65B43B5DF45...072C6B`) via `decide_artefact_approval --id 1 --approve --by <staff email>`, then arm
     `APPROVALS_ENABLED` + build A6 (Broker Catalogue V1 provisioning integration — design recorded in the survey:
     new `APPLY_BROKER_CATALOGUE` host op + `Apply-GuvfxBrokerCatalogue.ps1` + slot_preparation Stage 5-catalogue +
     `HOSTED_BROKER_CATALOGUE_ENABLED`). Only then do future Pepperstone tenants resolve fast. A6 was deliberately
     NOT built before approval (would be speculative infra).
  2. **Launcher switch.** The windowless binary is staged on host (`C:\GuvFX\_launcher_stage\guvfx_launch.exe`, SHA
     `320F4311542C97AD...E019EA`). The live atomic switch (replace `C:\GuvFX\launcher\guvfx_launch.exe` + re-pin
     `.guvfx_launcher_manifest` first line to the new SHA + re-mint the `GuvFX-NativeLauncher*` AppLocker
     FileHashRule + re-assert ACL SYSTEM/Admins Full, Users R+X) is Amber→Red: it re-mints a live security rule and
     the active beta uses this launcher, so it needs a genuine interactive RDP single-instance battery + no-console
     visual proof before switching. Rollback = current live SHA `CE2097…94765` (manifest + AppLocker rule + old exe).
- **Deviations from packet.** A6 deferred to post-approval (human gate); B live switch deferred to a gated interactive
  cert (Amber→Red). Both are prepared/verified, not performed. No beta purge this packet (per Objective A).
- **Exact tests.** `hosted_workspace+approvals+operational_events` 1102 pass; `approvals` 7; onboarding read-model 30
  (+3 new); frontend `HostedWorkspaceStatus` 6; `npm run build` + eslint clean.
- **Commit / branch state.** main `3f429f8` (merged #398). Prod images backend `55c2d34ac04b`, frontend
  `42ac8a40f5fb`; approvals migration `0001` applied. Host: candidate + staged winexe binary in place; live launcher
  UNCHANGED.
- **Rollback.** Code: redeploy backend `e0f343868153` / frontend `41630ed64863`; `migrate approvals zero` (DARK, no
  consumer). B/A leave no live-behaviour change to roll back (both gated).
- **One bounded next action.** Hand the two gated items above to Nuno; recommend approving the catalogue candidate
  first (unblocks A6) — do NOT switch the launcher or arm the catalogue without the interactive certs.

## 2026-08-27 — Fresh beta reset + Telegram closure: BETA_ACCEPTANCE_CLEAN_RERUN_READY

- **Scope / decision.** Prepare one clean disposable-beta rerun: (A) evidence-first purge of the disposable tenant,
  (B) Telegram connect-failure root cause + fix, (C) new-tab connect UX, (D) strategy-page Telegram discoverability,
  (E) DARK operator telemetry, (F) regression preservation. Mutation authorised for this packet.
- **Verified fact vs assumption.** *Verified:* purge negative-existence (DB + host) and sacred byte-for-byte
  preservation; the connect-failure root cause from the prod webhook log (`reason=chat_already_connected`) + the
  token row (`consumed=None`, no binding); frontend build+eslint+tests; full backend 4531 pass; bounded-observation
  intact post-purge (`polled=4 errors=0`). *Assumption (not visually confirmed):* the frontend Telegram UX changes
  are deployed (image `41630ed64863`, build+tests green) but were not clicked through in a live logged-in beta
  session — no beta credentials, and the tenant is now purged. Telemetry is deployed DARK (`OPERATIONS_EVENTS_ENABLED`
  off); it was proven by unit tests, not by a live operator-timeline read.
- **Operational reality the Sponsor must know for the rerun.** The Sponsor's personal Telegram (`NunoRAmaral`) is
  bound to Customer Zero (user 2). If he connects THAT Telegram to the new beta account he will again hit
  `chat_already_connected` (now with an actionable help message, but still refused — isolation is intentional). For a
  clean end-to-end Telegram test he must either use a DIFFERENT Telegram account for the beta, or first disconnect
  Telegram from Customer Zero himself (CZ is sacred — NOT touched here).
- **Deviations from packet.** None material. Telemetry deployed DARK rather than armed (arming
  `OPERATIONS_EVENTS_ENABLED` enables the whole operational-events subsystem — an Amber decision beyond this Telegram
  packet; the webhook `reason=` logs already provide diagnosability). Bot-reply-on-rejection was considered and NOT
  added (keeps the webhook fast + avoids a new outbound path in the ack; frontend help + telemetry cover it).
- **Exact tests.** `backend/.venv/bin/python manage.py test` -> 4531 pass (1 skip); `customer_notifications.tests_telemetry`
  7 pass; frontend affected tests 14/14 (incl. popup-blocked regression); `npm run build` green; eslint 0.
- **Commit / branch state.** main `939eeeb` (merged #397). Prod images backend `e0f343868153`, frontend
  `41630ed64863`. DB backup `guvfx_prePURGE36_20260827T125019Z.sql.gz` (sha `7e3d78afd8f6f945`).
- **Rollback.** Code: redeploy backend `ee5f3d629676` / frontend `c89528d70b31`. Data: restore the pre-purge dump
  (email is otherwise free for a fresh registration, which is the intended next step).
- **One bounded next action.** Hand `BETA_ACCEPTANCE_CLEAN_RERUN_READY` to the Sponsor to register the new beta and
  run the journey — do NOT create the replacement customer here. When he reaches Telegram, remind him of the
  bound-to-CZ constraint above.

## 2026-08-27 — Bounded Account Observation: DEPLOYED + ARMED + VERIFIED (P0)

- **Scope / decision.** Programme-Director-approved P0 fix for fresh-beta account-detection latency (~5-min
  effective cadence → ≤~30s). Implemented, tested, merged (PRs #395 + #396), deployed, armed, and verified in
  production read-only. Mutation was authorised for this packet (scheduler / timeout / recovery / UX + deploy).
- **Verified fact vs assumption.** *Verified:* the bounded concurrent cycle runs live (workers=8; 3.52s wall vs
  8.38s serial in a read-only probe); typed reasons + `recovery: relaunched=0` + `re-poll passes=2` are in the ops
  log; no `terminal64` process started after deploy (attach-only); identity-pin code untouched; TA32 is
  `EXECUTION_READY` and observable. *Assumption (not stress-tested):* the 18s/8s host deadlines were not forced to
  fire under a genuinely first-run busy tenant (TA32 is now warm, observe ≈3.5s); the deadline is deployed and one
  natural `observation_timeout` was observed, but a cold first-run reproduction was not staged. The frontend
  Detecting-slow copy is deployed (build green, strings in i18n) but was **not** visually confirmed in a live
  logged-in beta session (no customer credentials).
- **Deviations from packet.** None material. Added a second small PR (#396) to surface the bounded/recovery
  telemetry into the ops log — §8/§9 require it be *observable*, and #395 computed but did not print it.
- **Exact tests.** `backend/.venv/bin/python manage.py test hosted_workspace` → **971 pass**;
  `… terminal_provisioning` → **1328 pass**; `hosted_workspace.tests_bounded_observation` (9) +
  `tests_hosted_observation_command` (17, incl. 2 new). Frontend `npm run build` green. Host
  `Invoke-GuvfxObserver.ps1` `[Parser]::ParseFile` → PARSE-OK, `nonascii_count=0` (RULE 9).
- **Commit / branch state.** main `4041f7d` (merged #395 `2025f0a`, #396 `4041f7d`). Branches deleted. Prod images:
  backend `ee5f3d629676`, frontend `c89528d70b31`. Host observer files updated + daemon restarted. `beta.env`
  gains `HOSTED_BOUNDED_OBSERVATION_ENABLED=1` (backup `beta.env.bak.pre-bounded-*`).
- **Rollback.** Flip `HOSTED_BOUNDED_OBSERVATION_ENABLED=0` + recreate backend → byte-identical legacy path (host
  deadlines then simply shorten the legacy serial observe; images/scripts need no revert). Full revert anchors:
  backend `341e2c59d69b`, host `observer_attach.py` `9F4EE5DA…`, `Invoke-GuvfxObserver.ps1` `FFDD5F74…`.
- **One bounded next action.** Let TA32 complete its own manual "I confirm this is my trading account" step
  (customer action; do NOT click it) and watch the ops log confirm detection stays ≤~30s for the next fresh login —
  do not stage a synthetic first-run reproduction without Sponsor sign-off.

## 2026-08-20 — Customer Telegram product-policy candidate (no deployment)

- Current branch `feat/customer-telegram-policy-preferences`, baseline main `5d8c534`. The certified dedicated
  customer transport is preserved; only policy, presentation, preferences and customer UX change.
- Customer trade-open delivery is removed from observers/reconciliation and prohibited by strict enqueue and
  delivery allow-lists. Historical pilot rows remain unchanged. Unknown/malformed/raw events fail closed.
- Defaults: winners ON; losses and breakeven OFF; signal-safe durable TP progress ON; optional system/account
  messages ON. Connection confirmation remains essential. Language is snapshotted from GuvFX EN/JA.
- Adds customer × StrategyAssignment preference/pending intent and one-shot workspace-readiness intent. Neither
  path writes trading, execution, authorization, MT5, bridges, nodes, WorkerIdentity, CZ or support@ state.
- Focused gates are green: 94 backend notification tests including the 26-case policy battery; 307 frontend
  tests; full backend 4,460 (1 skip); lint 0 errors / 18 existing warnings; parity, 41-page production build,
  secret scan and `make check` green. 390 px and localized card evidence is in
  `docs/evidence/customer-telegram-product-policy/`. Exact-head CI remains pending.
- Production remains on the pre-policy pilot revision with flags ON and historical trade-open capability.
  No narrow production event switch exists; no production mutation was authorized or performed. Do not broaden
  beta notification use. Merge does not authorize deployment.
- Canonical detail: `docs/product/CUSTOMER_TELEGRAM_PRODUCT_POLICY.md`.

## 2026-08-19 — Customer Telegram reconciled RC (DARK install gate)

- Customer-only Telegram notification plane is updated in draft PR #371 on branch
  `agent/customer-telegram-notifications`, reconciled onto main `cd05c03f`; merge and DARK install are authorized
  only after exact-head CI. The bot/credentials/webhook/messages/pilot remain separately human-gated.
- Identity is Telegram private numeric `chat.id` established only through an atomic, one-use `/start` token.
  Delivery is a separate durable outbox/worker with owner checks, bounded retries, dedupe, EN/JA messages, and
  fail-open post-commit observers. Durable account-scoped TP progress and final aggregate outcomes include the
  owner's MT5 account number, realised PnL, and timestamp. Provider/WIMs ingestion and all execution authority remain separate.
- Monitoring adds secret-free binding counts and a durable heartbeat. DARK Settings has no actionable Connect
  button. The dedicated worker definition must be installed stopped, with both flags explicitly false.
- Reconciled verification is green: `make check` 4,366 backend tests (1 skipped), 46 frontend files / 288
  tests, lint 0 errors / 19 existing warnings, parity and production build; focused 92 backend notification/
  default-sizing tests and 7 Settings tests; no `customer_notifications` migration drift. Exact-head CI remains
  mandatory before merge.
- Adversarial bar: all 28 required cases covered, **0 HIGH / 0 MEDIUM**. Pilot is
  `beta.guvfx01@gmail.com` using an existing durable safe event; never manufacture a trade.
- Full review/evidence packet: [`docs/product/CUSTOMER_TELEGRAM_NOTIFICATIONS_POC.md`](product/CUSTOMER_TELEGRAM_NOTIFICATIONS_POC.md).
- Human gate: [`docs/operations/customer-telegram/PRODUCTION_ACTIVATION_RUNBOOK.md`](operations/customer-telegram/PRODUCTION_ACTIVATION_RUNBOOK.md).
- **STOP after DARK install:** no production bot creation, credential entry, webhook, worker start, flag ON,
  customer message, pilot, manufactured trade, or execution change.

> Concise pointer. **Notion is the source of truth** for the full programme
> lifecycle (*GuvFX — Current State v0.52*); GitHub holds implementation, tests and
> concise evidence. This file does not assert point-in-time PR status. Full state
> map: [`docs/PROGRAMME_STATE.md`](PROGRAMME_STATE.md).

- **Synthetic foundation (006C arc) — DONE.** 006C + R1…R4-R2 merged to `main`
  (R4-R2 via PR #36; `main` `148437ae`). Synthetic-only; no real data/NAS/broker in
  this repository.
- **Live frontier — real data acquisition (006D).** Runs in the dedicated repo
  `nunoamaral-hue/guvfx-windows-history-agent` (`main` `46c81057…`) + governed
  read-only VPS probes. Probe ladder PASSED through P4 (history retrieval feasible);
  **P5 (first durable raw object) is BLOCKED** at the storage gate.
- **Single blocker:** owner action **GFX-PKT-006D-S1** — provision/expose
  `GuvFXData` / `GUVFX_DATA_ROOT` to the Mac controller (NAS now on Tailscale).
- **PM:** Claude Code is acting PM (documentation/authoring/tracking + Green/Amber
  self-acceptance). New live-order/credential/risk-limit/promotion authorizations
  and lifecycle ratification remain Nuno's explicit gate.

---

# HANDOFF (2026-08-08) — ADR-0034 Execution Engine subsystem

- **Merged.** PR #315 (Execution Engine incl. G12 provenance/telemetry/reconcile) → `main` `cc84117` (CI
  green). The order-safety spine + provenance/observability are on main, DARK.
- **Open — capstone.** PR **#317** (`feat/adr0034-execution-capstone`, commit range on branch head) adds the
  durable workspace→node binding + provisioning contract + routing/claim enforcement + operator command +
  contract/arming/failure-matrix/cert docs. execution+hosted_workspace tests green; `make check` green;
  6-lens adversarial review 0 HIGH/0 MEDIUM (1 LOW fixed).
- **Open — capstone completeness remediation (P1-P5, DARK).** A final in-boundary completeness audit found
  the demo order was NOT actually reachable: **P1 (HIGH)** — the certified bridge only ever sent
  `X-Worker-Token` → shared `legacy-worker` (no `authorized_nodes`), so a hosted node-bound job was
  unclaimable. Fixed by giving the SAME bridge a HOSTED-mode modern-auth path (`X-Worker-Id`/`X-Worker-Secret`
  from `GUVFX_WORKER_ID`/`GUVFX_WORKER_SECRET`; fail-closed at startup if missing; legacy mode unchanged).
  **P2** RULE-11 positive control at the claim endpoint; **P3** structural single-path sweep (replaces a hand
  list whose "no MetaTrader5 import" claim was false); **P4** provision grant-block test; **P5** 8 arm
  reason-code + disarm assertions. A fresh 5-lens adversarial review returned **0 surviving HIGH/0 MEDIUM**
  (one HIGH candidate calibrated to LOW); the 5 LOW survivors were then **closed as hardening**: (a) the
  shared `legacy-worker` identity can never be node-aware — `provision_hosted_execution` refuses to grant it a
  node AND `views.next_job` forces it non-node-aware at the claim path; (b) cross-node regression test
  (node-A worker refused a node-B job); (c) import-surface positive control in the single-path sweep;
  (d) the compound arm branch split into two single-disjunct tests. **Amber decision (flagged):** the
  `next_job` guard touches the shared claim hot path — it is additive and behaviour-preserving (a no-op for
  the legacy row's normal empty-perms state; only restricts a mis-provisioned shared row). Focused 56/56, full
  `execution` 908/908, `make check` green. DARK/flags-OFF; no migration; no order placed. Pushed to #317.
- **Open — capstone round-2/3 completeness (7 items, DARK).** A full-boundary audit (7 lenses) confirmed the
  production code is behaviorally complete and closed 7 remaining items: completion endpoint provenance
  positive control; a hosted completion-side node-**membership** entitlement gate in `views.complete` (Amber:
  shared complete path, DARK-gated additive); `workspace.execution_ambiguous` added to the taxonomy + a
  code-derived emit-surface test; idempotency docstring corrected; fail-safe raise-injection tests; readiness
  split-disjunct + freshness future/None mutation tests. Two adversarial reviews returned 0 surviving
  HIGH/MEDIUM; two self-introduced LOW defects (completion gate node-liveness bug; hand-written emit literal)
  were corrected. Focused 90/90, full `execution`+`hosted_workspace` 1128, `make check` green (backend 3323).
  A round-3 convergence audit is the final gate. DARK/flags-OFF; no migration; no order placed.
- **CONVERGED — Execution Engine subsystem repository-complete (PR #317, head `cfb3121`).** Subsystem-led
  loop-until-dry: 4 completeness-audit rounds + 5 adversarial reviews → **0 surviving in-boundary gaps**
  (R3 closed orphan-recovery FINISHED provenance + durable-uuid completion gate + pre-send re-verify test
  pin; R4 closed the close-path identity loop-membership pin + completion-gate NULL-node fail-closed; R5 dry).
  Every fix-review caught + fixed self-introduced residuals. `make check` green (backend 3329); DARK/flags-OFF;
  no migration; **no order placed**. Marker `EXECUTION_ENGINE_REPOSITORY_COMPLETE — HOST_CERT_PENDING` — only
  the manual human-gated disposable-demo order remains (Nuno; Claude never trades). Two non-blocking pre-arming
  notes in `EXECUTION_ENGINE_CAPSTONE.md` §7c. **Next repo-buildable subsystem = customer-facing Onboarding /
  provisioning journey** (none exists; only the DARK operator `provision_hosted_execution` command).
  Workspace Delivery (#316) stays PARKED on the Sponsor host RDS/SPLA decision.
- **Verified fact vs assumption.** VERIFIED: repository-complete for the full subsystem boundary (binding,
  routing, claim, worker contract, persistence, idempotency, retries, reconciliation, concurrency, telemetry,
  API contract, cert harness, tests, mutation, review, docs); all flags OFF; no migration arms; legacy
  Provider-A unchanged. ASSUMED/UNPROVEN: the empirical end-to-end demo trade (blocked on the human action).
- **Exact stop / next action.** The ONLY remaining step is the **disposable-demo order**, which is a manual
  human action — **Nuno places+closes it** (Claude never trades, even demo). Runbook:
  `docs/operations/hosted-workspace/EXECUTION_ENGINE_CAPSTONE.md` §4. Marker:
  `EXECUTION_ENGINE_REPOSITORY_COMPLETE — HOST_CERT_PENDING`.
- **Parked, do not touch.** PR #316 (Workspace Delivery / RemoteApp) — separate subsystem; host RDS/SPLA is a
  Sponsor decision; it rebases its `HostedMt5Workspace` migration onto the new main later.
- **PM gate.** New live-order/credential/promotion authorizations remain Nuno's explicit gate.

---

# HANDOFF (2025-12-16)

> Outgoing coder updates this at the end of **every** session.

## What we were trying to achieve
- [x] Move all work to **GuvFX** (and prevent accidental pushes to GuvPay).
- [x] Merge continuity workflow PR (handoff system + docs/process).
- [x] Merge broker autocomplete edgecases PR with green CI + green `make check`.
- [x] Keep repo health green on `main` (`make check` passes).

## Current state (source of truth)
- Repo: **GuvFX**
- Default branch: `main`
- Remote safety:
  - `origin` must be `https://github.com/nunoamaral-hue/GuvFX.git`
  - No GuvPay remote should exist in this repo (or it must be push-disabled)
- Last commit on `main`: `818ac4c`
- CI status (latest): ✅ backend + ✅ frontend
- Backend: tests ✅ (2 tests passing via `make check`)
- Frontend: lint ✅, build ✅ (both via `make check`)

## What changed this session
- Repo hygiene / safety:
  - Ensured `origin` points to GuvFX (not GuvPay).
  - Removed accidental nested folder `GuvPay-pr/` and added `GuvPay-pr/` to `.gitignore`.
- Continuity workflow:
  - Continuity PR (v2) merged into `main` (handoff workflow, docs system).
- Broker autocomplete:
  - Edgecases branch fixed/cleaned (removed merge marker fallout) and merged into `main` with checks passing.
- Broker autocomplete verification:
  - Seeded `BrokerServer` entries (IS6FX, IC Markets, TradersWay, XM, Exness) and confirmed `/accounts` autocomplete (demo/live selections plus ↑/↓/Enter/Esc navigation) behaves as expected.

## VPS + MT5 handoff session (2025-12-16)

### What changed
- Production VPS is live: Traefik routes `https://guvfx.com`, `https://api.guvfx.com`, and `https://guac.guvfx.com/guacamole/` with Let’s Encrypt certificates on ports 80/443.
- Stacks run from `/home/ubuntu/guvfx-prod` (Traefik + backend + frontend + Postgres) and `/home/ubuntu/guacamole-stack` (Guacamole UI, `guacd`, `guac-db`, `mt5free-desktop`).
- Shared handoff directory `/srv/guvfx/mt5_handoff` (owner `10001`, group `1000`, mode `2770`) is mounted into `/app/.guvfx_handoff` (backend) and `/home/mt5free/.guvfx` (MT5); JSON configs like `account_1.json` sync between the services.
- MT5 automation runs via Openbox autostart: wallpaper + MT5 start + maximize + `$HOME/bin/apply-account-config` (fills login/server, optional password, no default submit).

### How to verify
- `docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}"`
- `docker compose -f /home/ubuntu/guvfx-prod/docker-compose.yml ps`
- `docker compose -f /home/ubuntu/guacamole-stack/docker-compose.yml ps`
- `docker logs --tail 80 traefik | egrep -i "acme|letsencrypt|certificate|error" || true`
- `curl -Ik https://guvfx.com --max-time 10 || true`
- `curl -Ik https://api.guvfx.com --max-time 10 || true`
- `curl -Ik https://guac.guvfx.com/guacamole/ --max-time 10 || true`
- `docker exec -it guvfx-backend sh -lc 'ls -la /app/.guvfx_handoff | tail'`
- `docker exec -it mt5free-desktop bash -lc 'ls -la $HOME/.guvfx | tail'`

### Known issues
- MT5 mouse input via Guacamole is still flaky; refer to `docs/KNOWN_ISSUES.md` for symptoms and log-based next steps.

### Next steps
- Fix the MT5 mouse input issue so automation clicks consistently reach the app.
- Harden `apply-account-config` (secure password handling and add optional `SUBMIT=1` flag to press OK when desired) and document the gating behavior.
- Consider baking the `apply-account-config` automation into the `mt5free-desktop` image so the pipeline can be versioned with the container.

## How to verify
- From repo root:
  - `git remote -v` (confirm `origin` is GuvFX)
  - `make check` (backend tests + frontend lint/build all green)

## Known issues / blockers
- Local git corruption risk (historical):
  - Some machines previously had a broken local ref named `master 2` which caused fetch/pull errors.
  - Fix (local only): remove `.git/refs/heads/master 2`, then `git pack-refs --all --prune`, then `git fetch origin --prune`.
- Product verification pending:
  - Broker autocomplete/keyboard navigation needs **verification with real broker server data** (see next steps).

## Exactly what to do next (in order)
1) **Clive start-of-session sanity check**
   - `git checkout main && git pull`
   - `git remote -v` (origin must be GuvFX)
   - `make check`
2) **Verify broker autocomplete on real data**
   - Go to `/accounts`
   - Type 2+ chars in “Broker server name”
   - Confirm: debounce, cancellation, correct suggestions, ↑/↓ highlight, Enter selects, Esc closes, mouse click selects, “No matches” state, error state.
   - If issues found: open a small `fix/...` branch, keep diff minimal, ensure `make check` green, PR into `main`.
3) **P1 cleanup follow-ups**
   - Confirm `.trash_duplicates/` is ignored and no duplicate “(1)” / “ 2” files are reintroduced.
4) **Retire risky old branches**
   - Avoid resurrecting old `feat/broker-autocomplete-flow` if it causes rebase conflicts; create fresh branches off `main`.

## Notes for the next coder (Clive)
- Follow `docs/CLIVE_RUNBOOK.md` and `AGENTS.md` rules.
- No unrelated refactors; no editing build output (`frontend/.next/`).
- Every session ends by updating: `docs/HANDOFF.md`, `docs/STATUS.md`, `docs/NEXT.md`, `docs/KNOWN_ISSUES.md` (if needed).

## VPS / MT5 handoff update (2025-12-18)

### What changed
- Production VPS is live with Traefik on `traefik-public` routing `https://guvfx.com`, `https://api.guvfx.com`, and `https://guac.guvfx.com/guacamole/` over Let’s Encrypt.
- Stacks live in `/home/ubuntu/guvfx-prod` (Traefik + GuvFX backend + GuvFX frontend + guvfx-postgres) and `/home/ubuntu/guacamole-stack` (`guacd`, Guacamole, `guac-db`, `mt5-free-vnc`).
- Shared mount `/srv/guvfx/mt5_handoff` (owner 10001, group 1000, mode 2770) is bind-mounted into `/app/.guvfx_handoff` and `/home/mt5free/.guvfx`; files are 660 so both containers share configs.
- Openbox autostart now draws the wallpaper, launches/maximizes MT5, and runs `$HOME/bin/apply-account-config` (uses `xdotool`/`wmctrl` on `$HOME/.guvfx/account_1.json`) to pre-fill the Login dialog without pressing OK.

### How to verify
- `docker ps`
- `docker logs --tail 200 traefik | egrep -i "acme|certificate|error" || true`
- `curl -Ik https://guvfx.com --max-time 10 || true`
- `curl -Ik https://api.guvfx.com --max-time 10 || true`
- `curl -Ik https://guac.guvfx.com/guacamole/ --max-time 10 || true`
- `stat /srv/guvfx/mt5_handoff`
- `docker exec -it guvfx-backend sh -lc 'ls -la /app/.guvfx_handoff | tail'`
- `docker exec -it mt5free-desktop bash -lc 'ls -la $HOME/.guvfx | tail'`

### Known issues
- MT5 mouse input via Guacamole remains flaky; see `docs/KNOWN_ISSUES.md` for the latest observations and log-based troubleshooting.

### Next steps
- Investigate the Guacamole mouse issue (logs, VNC flags, focus) so automation clicks can be trusted.
- Harden `apply-account-config` (per-account JSON flows, secure passwords, optional `SUBMIT=1` gate) and cook it into the `mt5free-desktop` image if that proves stable.

---

## 2026-08-04 — WP5.4 Trusted-Beta Operations Readiness & Arming Runbook (docs + governance only)

> Appended fresh (the pointer above predates the Aug-2026 broker-connectivity / WP5 arc; Notion remains the
> full-lifecycle source of truth).

- **Status:** PARTIAL is not applicable — this is a documentation + governance + validation-test packet with
  **no runtime effect**. Delivered on branch `docs/wp5-4-trusted-beta-ops-readiness`.
- **Scope:** authored `docs/operations/broker-connectivity/` (README, feature-flags.md/.json, arming-runbook,
  rollback-matrix, incident-response, support-playbook, monitoring-spec, trusted-beta-readiness, evidence-pack,
  readiness-checklist.json) + validation test `backend/operational_events/tests_wp54_readiness.py`; amended
  ADR-0029/0030/0032 with the operational arming contract; updated STATUS/NEXT/this file.
- **Verified fact:** all six broker-connectivity flags default OFF in code (definition sites cited in
  `feature-flags.json`); the validation test (17 checks) passes; nothing armed/deployed; Customer Zero +
  production untouched. Execution-gate arming requires WP6 PASS (not authorised/started).
- **Assumption:** host-side readiness items (validation image, tasks/ACLs, keyring, golden pin) are
  **HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL** and remain `PENDING` in `readiness-checklist.json`.
- **Deviations from packet:** none material. The "health app" is the `reliability` app (documented); no `mt5`
  migration belongs to the broker-connectivity programme (documented).
- **Out-of-scope access performed:** No. No deployment, no flag change, no credential access, no NEGOTIATE /
  VALIDATE_LOGIN, no order.
- **Recommended next packet:** WP6 multi-tenant certification (Sponsor-authorised), consuming this package's
  entry criteria and arming runbook. Do not arm any flag before WP6 PASS + Sponsor approval.

---

## 2026-08-04 — WP6 Multi-Tenant Certification PLAN (planning + governance + tests only)

- **Status:** documentation + validation-test packet with **no runtime effect**; branch
  `docs/wp6-multi-tenant-certification`. WP6 *planning* authorised + complete; WP6 *execution* NOT run.
- **Scope:** `docs/operations/broker-connectivity/wp6-*` (README + 12 area docs + `wp6-test-matrix.json`,
  `wp6-evidence.json`, `wp6-release-gate.json`) + `backend/operational_events/tests_wp6_certification.py`;
  WP6-gate notes appended to ADR-0029/0030/0032; STATUS/NEXT updated.
- **Verified fact:** the matrix covers every area A–L; execution-safety covers all 15 exposure-opening routes
  in `execution_entrypoints.json` (cross-checked by the test); operator-workflow covers all 17
  support-playbook workflows; the release recommendation is `null`; no case/gate item is PASS; capacity is
  entirely `TO BE MEASURED`. `make check` green; nothing armed/deployed; CZ + production untouched.
- **Assumption:** WP6 **execution** requires the disposable environment (Nuno demo account + Windows host) =
  **HOST-VERIFIED / OUTSIDE REPOSITORY CONTROL**; it is Sponsor-gated and not performed here.
- **Deviations from packet:** none material. The "health app" is `reliability`; two capacity signals (event
  lag, operator-API error rate) do not exist and are marked must-be-ADDED, not merely measured.
- **Out-of-scope access performed:** No. No deployment, flag change, credential access, NEGOTIATE/
  VALIDATE_LOGIN, order, or production/CZ mutation.
- **Recommended next packet:** WP6 certification **execution** in a disposable environment (Sponsor-gated),
  producing the evidence that completes `wp6-release-gate.json`, then a Sponsor GO/NO-GO decision. Do not arm
  before WP6 PASS + Sponsor approval.

---

## 2026-08-04 — WP6A Shared-Environment Operational Certification (non-destructive; DARK)

- **Status:** PASS (engineered correctness) with a **GO WITH CONDITIONS** Internal-Pilot verdict. Branch
  `docs/wp6a-shared-env-certification`. No arming/deploy/destructive testing; flags OFF; CZ + prod untouched.
- **Scope:** `docs/operations/broker-connectivity/wp6a-certification.md` (WS A–I), `wp6a-certification.json`,
  `wp6a-pilot-recommendation.md`; validation test `backend/operational_events/tests_wp6a_certification.py`;
  STATUS/NEXT updated.
- **Verified fact (executed 2026-08-04, main@b3e0bba):** 387 backend tests (19 broker-connectivity modules) +
  46 frontend Operations-UI tests = 433, all OK; full make check green. Module counts sum-checked by the test.
- **Assumption / conditions:** live broker-login on the HOST is NOT proven (first demo VALIDATE_LOGIN failed
  at an ACL gap; ADR-0027 Phase 2 not host-certified) — the primary Internal-Pilot condition. build-5833
  ACTIVE, verify_image on host, golden pin, deployed commit, and a verified DB backup are HOST-VERIFIED /
  OUTSIDE REPOSITORY CONTROL.
- **Deviations from packet:** none material. WP6A certifies engineered correctness only; WP6B (isolation/
  concurrency/load/capacity/failure/recovery) is explicitly deferred and NOT claimed complete.
- **Out-of-scope access performed:** No. No live accounts, no production/CZ mutation, no service stops, no
  failure injection, no flag enablement.
- **Recommended next packet:** WP6B multi-tenant isolation certification in a disposable environment
  (Sponsor-gated), and — for any Internal Pilot — close the broker-login host ACL gap + prove one demo
  VALIDATE_LOGIN on the host.
# EN/JA beta parity stream (2026-08-18)

The P0 closed-beta activation journey and EN/JA contract tests are on `feat/beta-en-ja-parity`, based on
`4224486e8e1433327dd4065e86820efecbe8ebbe`. Configure, My Strategies, Hosted onboarding, the Hosted MT5
viewer, Hosted Workspace account status, activation confirmation, core plan/onboarding copy, customer-safe
errors, and locale helpers are localized. See `docs/product/BETA_EN_JA_AUDIT_2026-08-18.md` for the verified
14-image acceptance matrix and the remaining P1/P2 backlog. Production was not touched.

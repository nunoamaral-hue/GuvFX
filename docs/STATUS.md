# GuvFX — Project Status

> Update this file **whenever** project state changes. This is a current-state
> snapshot; deeper operational detail lives in `docs/RUNBOOK.md` and the handoff
> docs.

> **⚠ TRUTH CORRECTION (2026-08-05) — broker-login validation.** Some entries below (notably the
> 2026-08-02 *"Broker Login Validation Primitive: HOST CERTIFICATION COMPLETE — production-ready"* line)
> read as if a **live** broker login was certified. It was **not**: only the **credential-free** validation
> path is host-certified; **no live credentialed `VALIDATE_LOGIN` has ever succeeded** (prod DB: 0
> `HEALTHY`/`demo_ok`; every live attempt failed with `login_timeout`/`validation_runner_unavailable`). The
> `demo_ok`/`HEALTHY` success appears only in **mocked** tests. Historical entries are retained as written;
> for the corrected, authoritative wording see **`docs/VALIDATION_TRUTH_CORRECTION.md`**.

## Execution workstream log

- **2026-08-08 — ADR-0034 Workspace Delivery / RemoteApp subsystem — repository-complete (DARK). NOT deployed. 🟢**
  Branch `feat/adr0034-workspace-delivery` (base = M3c-merged `main` `059b448`). Builds the seam by which a
  GuvFX user opens **their own** persistent MT5 as a Guacamole **RDP RemoteApp** and reconnects to the
  **same persistent Windows session** — entirely DARK behind two OFF flags, no production caller, and with
  **no RDS role on the host** (the seam generates the exact payload a future RDS deploy will consume).
  **New:** `mt5/guac_json.build_remoteapp_rdp_payload` (+ shared `_dedicated_rdp_base_parameters` extraction
  leaving the certified dedicated payload byte-identical; `normalize_remote_app` enforces `||` + empty
  fail-closed); `hosted_workspace/delivery.authorize_workspace_delivery` — the **security linchpin**:
  owner-bound (resolve by unguessable `workspace_uuid`, strict owner equality, **no staff mint bypass**),
  everything server-derived (host/username/program/args/credential; client overrides nothing; password rides
  ONLY inside the AES token), DARK-first (zero DB queries while OFF), fail-closed matrix of stable `DA_*`
  reason codes; `delivery_persistence` single delivery-state writer (row-locked, scoped `update_fields`,
  canonical/legacy state untouched, REMOTEAPP_CONNECTED/DISCONNECTED telemetry, host-node assignment,
  session-reuse); `delivery_read_model` (secret-free; host operator-only) + `delivery_views`
  `GET /api/hosted-workspace/delivery-state/` (read-only, DARK 404 unless BOTH flags ON, IDOR-safe).
  Additive migrations `hosted_workspace 0003` + `mt5 0009` (deps clean, reversible). 50 focused delivery
  tests (owner/IDOR, no-secret token-decrypt proof, fail-closed matrix, mutation-adequacy) + real
  `MT5Session` reuse; `make check` green; multi-lens adversarial review. `remoteapp_ready`/`delivery_state`
  are read-model only — order authority stays `evaluate_binding`; the flag cannot arm execution.
  **STOP boundary:** the Windows RDS/RemoteApp install + SPLA licensing + AppLocker + TX-1 NTFS-ACL are a
  Sponsor/host change — see `docs/architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md` §8/§9 and the Host Change
  packet `docs/operations/hosted-workspace/WORKSPACE_DELIVERY_HOST_CERTIFICATION.md`.

- **2026-08-08 — ADR-0034 M3c Workspace Core — authoritative persistence + read model (DARK). NOT deployed. 🟢**
  Branch `feat/adr0034-m3c-workspace-core` (base = the M3b-2 host-cert docs commit). Closes the observation
  chain with the one seam that *persists* the M3a manager decision, records provenance, and emits telemetry —
  all DARK. **New:** `HostedMt5Workspace` canonical M3c fields (`canonical_state`/`canonical_reason`,
  `observation_version`/`decision_version`, latest-observation projection cache, `last_decision_at`/
  `last_transition_at`, `last_correlation_id`); append-only `WorkspaceTransition` (unique `dedupe_key`
  reused as the `OperationalEvent.dedup_key`); additive/reversible migration `0002` (apply→reverse→re-apply
  proven). **Writer** `persistence.persist_workspace_decision` — the SINGLE canonical-state writer:
  `select_for_update` row lock, stale-observation + stale-decision (illegal-transition vs the LOCKED state)
  rejection, version monotonicity, idempotent replay, atomic state+event, telemetry emitted ONLY from this
  seam ONLY on a real state change. **Consumer** `consumer.ingest_observation` (DARK no-op when flag OFF;
  wired by no production caller; never attaches/launches/logs in/orders). **Read model + DARK API**
  `GET /api/hosted-workspace/workspace-state/` (404 while `HOSTED_PERSISTENT_MT5_ENABLED` OFF, IDOR-safe).
  Persisted `execution_ready` is read-model ONLY — the order authority remains `evaluate_binding` in the
  bridge. 29 focused tests + writer mutation-adequacy; `make check` green (backend 3167). Multi-lens
  adversarial review recorded in the PR. Legacy `WorkspaceState` fields untouched; Provider-A accounts
  (no workspace row) unaffected. See `docs/architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md` §7.

- **2026-08-08 — ADR-0034 M3b-2 HOST CERTIFIED — observation chain PROVEN on the live host (Amber). NOT deployed. 🟢**
  `M3B2_HOST_CERTIFIED — OBSERVATION_CHAIN_PROVEN`. Empirical disposable-host certification executed on
  WIN-RD8VDS93DK7 under an isolated, Sponsor-authorised `C:\GuvFX\cert\` footprint (repo staged byte-identical
  to `main` `c81ac06`; isolated venv Django 5.1.2 + MetaTrader5 5.0.6090 + requests + psutil; `cert_settings`;
  #305–#312 all merged to main). GATES 0–14 all PASS: **never-launch** (absent/missing target → guarded attach
  refuses, `initialize` never invoked, no terminal spawned; `MT5_GUARDED_ATTACH` code-enforced), **never-login
  / no-credential-replay** (attach is path-only; disposable `accounts.dat` from Nuno's manual login *predates*
  every GuvFX attach), **cross-session guarded attach** (SSH session-0 harness → session-3 build-**5.0.0.6073**
  demo terminal, per Experiment H) → `account_match=true` on `****2587 / IS6Technologies-Demo / DEMO` →
  canonical `CONNECTED`, `execution_ready=false` **correctly gated** by `trade_allowed=false` (AutoTrading
  off); **wrong-binding** → `SUSPENDED / ACCOUNT_MISMATCH`; **wrong/missing target** → fail-closed, no launch,
  live terminal untouched; **broker liveness** = BTCUSD two nonzero strictly-increasing ticks; IPC
  `terminal_info().path` pin proves the disposable install (not the IS6 sibling). Adversarial review (12-lens)
  → FIX_REQUIRED (2 MEDIUM = the GATE-7 liveness *evidence* overstated a stale weekend XAUUSD first-tick;
  certified **code sound**) → sound BTCUSD re-run + `fresh` reframed as observation-record recency → re-verify
  **CERTIFY, 0 surviving HIGH/MEDIUM**. **Production blast radius ZERO** (IS6 4336/8748 + beta 316 never
  restarted; `:8788`/`:8791` owners intact). Cleanup: disposable terminal + workspace + `accounts.dat` +
  helpers removed; `cert/repo` + `cert/venv` PRESERVED (credential-free) as retained cert infra. Build-6073
  attach-fidelity gap CLOSED. **The Hosted-Workspace observation model is no longer hypothetical.** NEXT =
  M3c (Workspace Decision Persistence / authoritative consumer, DARK) — awaiting Sponsor packet; do NOT
  auto-start; M4 telemetry follows M3c.

- **2026-08-08 — ADR-0034 M-series MERGED to main + M3b-2 integration-cert entrypoint (Amber, DARK). Repo eng; NOT deployed. 🟡**
  Sponsor-authorised merge sequencing: PRs **#305→#306→#307→#308→#309→#310** merged to `main` in dependency
  order (M1 Guarded Attach, M2a state machine, M2b telemetry, M3a Manager, M3b-1 producer, M3b-2 agent) — each
  genuinely CI-green + CLEAN; **zero file overlap** between M1 (bridge) and the `hosted_workspace/*` stack.
  `main` now carries the whole certified observation chain (`make check` green; no hosted_workspace migration).
  New integration-cert branch `feat/adr0034-m3b2-integration-cert` (off merged `main`) adds the **operator-only
  disposable-host certification entrypoint**: `certification.py` (`classify_target_path` +
  `run_certification` — composes M1→M3b-2→M3b-1→M3a in a **single guarded attach**, emits a SECRET-FREE
  allow-list dict) + `management/commands/certify_workspace_observation.py` (refuses any non-disposable path
  BEFORE touching the host; accepts NO password; not a daemon/loop/service). Tests `tests_certification.py`:
  composition + single-attach + allow-list-only/secret-free + path classification + repo-level negative
  controls E (wrong binding→not ready) / F (wrong path→refused) / G (missing→fail-closed, no launch) / H
  (ambiguous→fail-closed). 166 hosted_workspace tests OK. **Empirical disposable-HOST certification (TESTs
  A–H on the live host, never-launch/never-login/blast-radius) is PREPARED, NOT RUN** — needs Nuno's manual
  broker login (HARD STOP) + a disposable broker-connected MT5 + host execution
  (`docs/operations/hosted-workspace/M3B2_HOST_CERTIFICATION.md`). Nothing deployed; production untouched.

- **2026-08-08 — ADR-0034 M3b-2: Hosted Workspace Agent — read-only observation pipeline (Amber, DARK). Repo eng; NOT deployed. 🟡**
  Branch `feat/adr0034-m3b2-workspace-agent` (stacked on M3b-1). First increment that touches the live
  Workspace Agent — **execution-adjacent + host-touching → Amber**. New pure orchestration
  `backend/hosted_workspace/agent.py` (`build_agent_snapshot` / `observe_workspace`): locate the EXPECTED
  terminal → **M1 Guarded Attach** → READ-ONLY `terminal_info`/`account_info`/`positions`/`orders`/`tick` →
  `RawWorkspaceSnapshot` → M3b-1 producer → `WorkspaceObservation`. **Observe only:** NEVER launches,
  NEVER `mt5.login()`, NEVER authenticates, NEVER places/modifies/closes an order; NO persistence, NO
  telemetry, NO recovery, NO state derivation. Fail-closed at every step (every host failure → an
  observation, never an exception, never a default positive); `clock` injected (deterministic). Reference
  adapter `agent_host.py` (`Mt5WorkspaceHost`) binds the pipeline to M1 + a live `mt5` by **injection** (no
  `MetaTrader5` import; passes ONLY `{path}` to the guarded attach — no credential replay). Tests
  `tests_agent.py` (mock host: oracle + AST mutation adequacy on the control flow + missing/duplicate/wrong
  process + attach failure/raise + account-unavailable + wrong login/server + trade-mode variants +
  empty/open positions + pending orders + tick present/absent + clock failure + never-launch/no-read-on-fail
  + AST no-login/no-launch/no-mt5-import proof + secret-free + exception safety) and `tests_agent_host.py`
  (spy mt5 + fake M1: M1-only, no login/order calls, read-only, release-once). **145 hosted_workspace tests
  OK.** No consumer wiring, no model, no migration, no flag (inherits M1's `MT5_GUARDED_ATTACH` at the attach
  boundary), grep-proven DARK. **Disposable-host certification is PREPARED, NOT RUN** — needs a
  broker-connected disposable MT5 (Nuno; credentialed login is prohibited for the agent) + live-host
  execution; runbook `docs/operations/hosted-workspace/M3B2_HOST_CERTIFICATION.md`. **Merge-sequencing of
  M1 #305 + the M3b-1 stack onto one base is a Sponsor Amber decision** (they are disjoint unmerged
  branches). M4 (telemetry emission) NOT started.

- **2026-08-08 — ADR-0034 M3b-1: Workspace Observation Producer (pure, DARK). Repo eng; NOT deployed. 🟢**
  Branch `feat/adr0034-m3b1-observation-producer` (stacked on M3a — real code dependency on
  `manager.WorkspaceObservation`). New pure/side-effect-free `backend/hosted_workspace/producer.py`: the
  trusted boundary that converts an untrusted `RawWorkspaceSnapshot` (raw host/MT5/attach facts) into the
  canonical M3a `WorkspaceObservation` and NOTHING else. `now` is **supplied** (no wall-clock);
  `previous_state` is **carried through**, never derived. **Reuses the certified
  `matching.evaluate_active_account_match`** (unchanged) via a readiness-neutral observation so only identity
  decides. Fail-closed throughout: `_is_true` (only literal `True`); `_is_number` (int/float, non-bool, and
  **finite** — NaN/inf rejected at the type gate); `_clean_identity` (blank/whitespace → None so the matcher
  denies); `_clean_trade_mode` (rejects bool so `False == DEMO(0)` cannot classify); `_compute_freshness`
  (deterministic; non-finite `tolerance` defaults to zero tolerance, never disabling the future guard); a
  top-level try/except collapses all six facts to False on any exception. Secret-free output (no
  login/server/password/token). **Derives NO lifecycle state, performs NO actions** (grep-proven DARK; no
  consumer, migration, wiring, flag, host, or execution). **REQUIRED 8-lens adversarial review** (execution-
  adjacent): first pass FIX_REQUIRED (3 HIGH NaN/inf freshness fail-open + 3 MEDIUM bool/whitespace identity);
  fixed producer-locally; **re-verification verdict CERTIFY** (all 8 lenses `broken=false`, 0 HIGH/MEDIUM
  surviving or introduced). Tests `tests_producer.py`: oracle A–S + account-match matrix + freshness
  boundary/stale/future/malformed + NaN/inf + bool/whitespace regressions + secret-free + no-state-derivation
  grep proof + exception safety + **AST mutation adequacy** on `_compute_freshness` and `_is_number` (every
  mutant killed). 99 hosted_workspace tests OK. M3b-2 (Workspace Agent host process — execution-adjacent,
  requires its own packet + adversarial + host certification) DEFERRED, not started.

- **2026-08-08 — ADR-0034 M3a: Workspace Manager decision engine (pure, DARK). Repo eng; NOT deployed. 🟢**
  Branch `feat/adr0034-m3a-manager` (stacked on M2b). New pure/side-effect-free
  `backend/hosted_workspace/manager.py`: `WorkspaceObservation` + `WorkspaceDecision` dataclasses +
  `derive_workspace_decision(obs)` — the ONE authoritative deriver of canonical workspace state (no Windows/
  MT5/persistence/telemetry-emit/execution). Answers only "given the observation, what should the state
  become?". **EXECUTION_READY derived ONLY when all of {attached, connected, account_match, fresh,
  trade_allowed}** (`_all_execution_conditions`); every transition validated by the M2 graph, illegal/unknown
  → fail-closed (hold previous, ERROR). Returns which telemetry event *should* occur (no emit). Tests
  `tests_manager.py`: oracle + illegal-transition + stale + execution-ready negatives + unknown-observation +
  **dual AST mutation adequacy** (gate + engine, every mutant killed) + a **576-case exhaustive graph-fidelity
  sweep** proving `execution_ready ⟹ all conditions`. 60 hosted_workspace tests OK. **No consumer, no
  migration, no wiring** (grep-proven DARK). M3b (Workspace Agent skeleton, execution-adjacent → adversarial
  review) next.

- **2026-08-08 — ADR-0034 M2b: Workspace telemetry taxonomy (DARK foundation). Repo eng; NOT deployed. 🟢**
  Branch `feat/adr0034-m2b-telemetry` (stacked on M2a — real code dependency on the state machine). New
  pure/inert `backend/hosted_workspace/telemetry.py`: `WorkspaceEvent` taxonomy (18 `workspace.*` events,
  ADR-0034 §6) + `EVENT_META` (each event → category/severity/canonical M2a state) + secret-free
  `build_workspace_event` builder (fail-closed to SYSTEM/ERROR `workspace.unknown_event`; redacts
  credentials, masks login) producing ADR-0032 `OperationalEvent` kwargs. **No emit sites, no model change,
  no migration, no consumer** (grep-proven DARK). Tests `tests_telemetry.py`: taxonomy completeness + meta
  validity + builder correctness + fail-closed + `_redact` **AST mutation adequacy** (secret-free = security
  property; every mutant killed). 46 hosted_workspace tests OK. M3 (Workspace Manager/Agent) next.

- **2026-08-08 — ADR-0034 M2a: canonical Workspace state machine (DARK foundation). Repo eng; NOT deployed. 🟢**
  Implementation-led development (Sponsor-authorised). Branch `feat/adr0034-m2a-state-machine` (base `main`
  `309db68`). New pure/inert `backend/hosted_workspace/state_machine.py`: canonical 9-state
  `WorkspaceLifecycleState` + `WorkspaceReason` codes + fail-closed `evaluate_workspace_transition` (ADR-0034
  §3 graph verbatim) + `to_canonical` legacy→canonical mapping (fail-closed to SUSPENDED/ERROR; never an
  execution state). **No model change, no migration, no consumer wired** (`makemigrations --check` clean;
  grep-proven DARK). Tests `tests_state_machine.py`: oracle + AST mutation adequacy (incl. in/not-in; every
  mutant killed) + graph fidelity + mapping completeness. 35 hosted_workspace tests OK. Adversarial review
  not required (no execution-path wiring). M1 (Guarded Attach, PR #305) precedes; M2b (telemetry) next.

- **2026-08-07 — ADR-0033 Increment 3: complete trade-operation identity safety (CLOSE/MODIFY). Repo eng; NOT deployed. 🟢**
  Branch `feat/adr0033-inc3-pilot-plumbing` (base `main` `c83e041` = merged PR #302). Extends the Inc2
  opening-order identity invariant to EVERY account-mutating op. New bridge gate
  `evaluate_mutation_identity`/`verify_mutation_identity` (connected + active login/server match; pin
  mandatory on the persistent-workspace path / `MT5_REQUIRE_IDENTITY_PIN`, env-optional legacy; NOT
  trade_allowed per E2 "where appropriate") wired as a pre-send check immediately before `order_send` in
  `close_position` + `modify_position`, with identity threaded from the `/mt5/close-position` +
  `/mt5/modify-position` bodies. E4 inventory: all 4 `order_send` sites now identity-gated (PLACE×2 Inc2,
  CLOSE+MODIFY Inc3); no other MT5 mutation primitive exists. Legacy account-#1 demo close/modify unchanged
  (no pin → connected check only). Tests: `tests_bridge_mutation_identity.py` (oracle + AST mutation
  adequacy + enforcement guard). **SCOPE:** this increment = complete trade-operation identity safety only.
  DEFERRED to a follow-up (NOT pilot-ready yet): durable routing wiring + server-side producer
  pin-derivation (B/C/D), observer pause/resume (G), host attach probe (I), read-only API (J), observability (K).

- **2026-08-07 — ADR-0033 Increment 2: execution-readiness abstraction + hardened order-time gate (DARK). Repo eng; NOT deployed. 🟢**
  Branch `feat/adr0033-inc2-readiness-abstraction` (base `main` `ac5a26b` = merged PR #301). Two-provider
  readiness abstraction (`execution/readiness.py`): Provider A (`temporary_validation`, default)
  reproduces the pre-ADR checks IDENTICALLY (regression proven: existing gate/dispatch/binding suites
  unchanged); Provider B (`persistent_workspace`) replaces ONLY `password_enc`+`VALIDATED` with
  attach-verified readiness, ANDed with is_active/disconnected + (at dispatch) health/pause; a wrong active
  account reports the specific `active_account_mismatch`. Order-time gate HARDENED
  (`scripts/mt5_signal_bridge.py`, additive, legacy-identical): mandatory identity pin (payload-sourced;
  enforceable as a terminal property via `MT5_REQUIRE_IDENTITY_PIN`; demo+live; fail-closed) + TOCTOU
  narrowing (authoritative re-verify immediately before `order_send` in both opening paths). Migration
  `trading 0015` (readiness_provider default temporary_validation for ALL existing rows +
  workspace_confirmed_at). Triple-dark; no new flag. Adversarial review (6 lenses) = 0 HIGH; 1 MEDIUM + 3
  LOW folded in. Docs: `docs/architecture/EXECUTION_READINESS.md`; ADR-0033 → Accepted-with-conditions.
  DEFERRED (additive, non-safety): observer pause/resume wiring, read-only workspace API, observability
  projection, routing implementation + producer pin-plumbing, host attach probe.

- **2026-08-07 — Hosted Persistent MT5 Workspace: Phase-1 foundation increment (DARK, additive). Repo eng; NOT deployed. 🟢**
  Branch `feat/hosted-mt5-workspace-foundation` (base `main` `1989b5f`). New app `backend/hosted_workspace/`
  (ADR-0033): `HostedMt5Workspace` sibling model (OneToOne TradingAccount, immutable-binding guard,
  secret-free `contract()`, `is_execution_ready` = display-only), a **pure fail-closed**
  `evaluate_active_account_match` (+ `WorkspaceObservation`/`normalize_observation`) mirroring the certified
  `evaluate_binding`, and three DARK Idiom-B flags (`HOSTED_PERSISTENT_MT5_ENABLED`,
  `HOSTED_MT5_REMOTEAPP_ENABLED`, `HOSTED_MT5_ACTIVE_ACCOUNT_POLLING_ENABLED`) + `feature-flags.json`
  inventory. **Inert:** nothing in execution/onboarding/delivery reads any of it; NO gate wiring, NO host
  script, NO API, NO frontend. 22 app tests pass incl. an AST operator-mutation adequacy proof on the matcher.
  This is a deliberate decomposition of the packet's full Phase 1 — the execution-facing wiring, Windows
  host tooling (ACL/RemoteApp/supervision), API, onboarding and observability are DEFERRED pending ADR-0033
  acceptance (4 design tensions) + a Sponsor-gated disposable-host pilot. Stores NO broker credential. Design
  + repo-truth note: `docs/architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md`; ADR `docs/ADRs/0033-...`.

- **2026-08-06 — Validation-Agent Monitoring Runner + Scheduler + Telegram Alert Delivery (IMPLEMENTATION + docs + tests). Repo eng; NOT deployed. 🟢**
  Branch `feat/agent-monitoring-runner` (base `main` `49cef11`, the merged PR #294). Completes the missing backend operations
  layer the two prior deploy attempts STOPPED on (merged monitoring was inert). **Durable state:**
  `AgentMonitorState` (migration `0011`, singleton pk=1) so hysteresis + per-alert cooldown survive a backend
  restart. **Runner:** `agent_monitor_runner.run_once` — probe → durable hysteresis → alert policy (cooldown,
  one-shot recovery, flap-counter decay) → delivery; only side effects are the signed-NEGOTIATE probe, the
  singleton row write, and an alert message. **Commands:** `run_agent_readiness_probe` (single-flight
  `select_for_update(nowait)` lock, exit codes 0/10/20/30/40/50, `--dry-run`, `--synthetic-state`),
  `test_agent_alert_delivery` (pre-arm synthetic-alert gate; no state/broker/customer), `agent_monitor_status`
  (read-only, secret-free ops evidence). **External delivery:** `TelegramAlertSink` (DEDICATED ops chat + its
  OWN token; factory refuses if ops chat == customer channel or token missing; failed send surfaced, never
  raised/suppressed) + `EmailAlertSink` fallback. **Scheduler:** `deploy/validation-agent-monitor/` cron
  installer (idempotent, dark, disableable). **Config:** 8 `settings.py` vars, all OFF/NULL by default
  (`VALIDATION_AGENT_MONITORING_ENABLED=false`, `AGENT_ALERT_SINK=null`, no Telegram/email destination); no
  secret committed. Contract `monitoring-runner-contract.json`; deploy package `monitoring-runner-deployment.md`;
  ADR-0013 addendum 2026-08-06c. Tests `tests_agent_monitor_runner.py` + `tests_agent_alert_sink_delivery.py`.
  `#12`/`#1`/`:8788`/host untouched. **STOP: deployment, flag-arming, and selecting a live destination remain
  separately Sponsor-gated.**

- **2026-08-06 — Validation Agent MINIMUM Production Hardening (IMPLEMENTATION + docs + tests). Repo eng; NOT deployed. 🟢**
  Branch `feat/validation-agent-min-hardening` (base `main` `f5d8389`, the merged PR #291). Turns the #291
  DESIGN into repository ENGINEERING for the minimum-for-beta set (RR-1/2/3/4/11). **Agent side:**
  `deploy/beta-agent/agent_lifecycle.py` (secret-safe lifecycle events, single-instance guard, launch
  classification) wired into `agent.py` (durable `agent_lifecycle.jsonl`; guard; optional hard refuse-to-bind
  `BETA_AGENT_REFUSE_UNSUPERVISED_LAUNCH`, default OFF; supervised launch markers); NEGOTIATE now advertises
  `agent_supervised` (bundle + byte-identical backend copy; manifest re-pinned `2026-08-06.1`, covers
  `agent_lifecycle.py`). **Backend (inert until scheduled):** `agent_health_probe.py` (signed-NEGOTIATE
  readiness probe, OWN connect/read split, 8 states, cadence + consecutive-success recovery),
  `agent_monitoring.py` (metric/alert computation), `agent_alert_sink.py` (Null/Logging sinks, named-owner
  required, no live external send — RR-11), `agent_status_presenter.py` (customer-safe vs operator-safe).
  **WinSW:** `winsw/GuvFXBetaAgent.supervised.xml` = TARGET supervised profile (Automatic+delayed, bounded-
  backoff restart FLOOR, launch markers) — NOT applied; DARK install-only XML preserved. **Frontend:**
  read-only `AgentStatusPanel` + `agent-status.ts` (unrouted). **Docs:** ADR-0013 addendum (supersede not
  rewrite), unsupervised-listener runbook (12th), WS-J repo audit, WS-L deployment/rollback package. No DB
  migration. `#12`/`#1`/`:8788` untouched. **STOP: all host/backend/service/firewall/live-validation actions
  remain separately Sponsor-gated.**

- **2026-08-05 — Validation Agent Production Hardening (design + docs + tests). Repo eng; NOT deployed. 🟢**
  Branch `docs/validation-agent-hardening` (base `main` `ba22df8`). Transition from forensics to operational
  engineering — DESIGN ONLY, no host/config/deploy/validation. New authoritative doc
  `docs/VALIDATION_AGENT_PRODUCTION_HARDENING.md` (lifecycle state diagram; startup-mechanism comparison →
  **WinSW service + armed supervision** recommended; health model; monitoring; five-plane logging; readiness
  review). Machine-readable + test-guarded artefacts under `docs/operations/validation-agent/`
  (health-model / monitoring-catalogue / readiness-review / runbook-index + `runbooks.md`) and an executable
  design spec `backend/terminal_provisioning/validation_agent_spec.py` (imported only by tests). Health states:
  STARTING/HEALTHY/DEGRADED/UNAVAILABLE/STOPPING/RECOVERY — a downstream (MT5/broker/IPC) failure is DEGRADED,
  never agent-UNAVAILABLE. Readiness = a signed NEGOTIATE (no unauthenticated /health). **Readiness gaps** RR-1
  no supervision, RR-2 no agent-down alert, RR-3 no lifecycle logging, RR-4 unenforced launch path, RR-5 no
  health state = the minimum-for-beta set; SPOF/host-auditing/upgrade-lifecycle/keyring-rotation = later.
  **WS-I** corrected outdated manual-start assumptions (RUNBOOK "never executed on a host" → commissioned;
  agent.py docstring: production=WinSW not ad-hoc `python agent.py`, manifest `2026-08-05.3`;
  OPERATIONS_DASHBOARD gained the `:8791` SPOF row) and flagged ADR-0013's auto-restart-RED classification as
  needing a superseding addendum (governance, NOT edited). 10-agent grounding + adversarial-review workflows.
  make check green. NOT deployed; no host/config/service/flag change; no live validation; #12/#1 untouched.

- **2026-08-05 — Validation transport-timeout taxonomy (connect vs read; kill the `login_timeout` mislabel). Repo eng; NOT deployed. 🟢**
  Branch `fix/transport-timeout-taxonomy` (base `main` `ba22df8`). **Forensic root cause (primary evidence, read-only):**
  account #13's latest `login_timeout` (correlation `validate-acct-13-d4079267879e`) failed at the **backend→agent
  TCP connect** — decrypt audit 21:09:58.577 → attempt 21:10:08.602 = **10.02s** = the `CONNECT_TIMEOUT=10`;
  reproduced live (backend socket connect to the configured agent `100.79.101.19:8791` fails with TimeoutError in
  10.02s; `:8788` open, `:8791`/`:8787` closed). MT5 was never invoked, the broker never contacted. **Defect:**
  `beta_worker.make_http_transport` collapsed `requests.ConnectTimeout` + `requests.ReadTimeout` into one
  `ManagementChannelTimeout` → `broker_login_validation` mapped it to `login_timeout`. **Fix (repo, DARK):** split —
  connect → new `ManagementChannelUnreachable` (a `ManagementChannelTimeout` subclass so provisioning/reclaim are
  unchanged) → `validation_agent_unreachable`; read → `ManagementChannelTimeout` → `validation_agent_timeout`;
  `login_timeout` reserved for a genuine agent/MT5 login-phase timeout (timeline now places it at `mt5_launched`,
  fail at `broker_login`). New taxonomy in `_TAXONOMY` + `_NON_AUTHORITATIVE_REASONS` + timeline `_REASON_FURTHEST_OK`
  (unreachable → fail at agent_received, never implies MT5 launched; timeout → agent reached, MT5 unknown) +
  customer wording (names neither broker/login/server) + operator hint ("Transport failed BEFORE the validation
  agent"). Docs: **Transport timeout taxonomy** (layer diagram) in `VALIDATION_OBSERVABILITY.md`; evidence rows 12–13
  in `VALIDATION_RELIABILITY_EVIDENCE_MATRIX.md`. Tests: connect→unreachable / read→timeout / neither→login_timeout;
  timeline stops before MT5; customer wording never says broker. Adversarial review (WS-I) could not overturn the
  taxonomy. make check green. NOT deployed; no Windows-host change; no env change (the `:8791` port fix is an
  operator action, flagged as evidence only); #12/#1 untouched.

- **2026-08-05 — Validation Reliability PHASE 4 (repository completion + terminology + honesty hardening). Repo eng; NOT deployed. 🟢**
  Same branch/PR #289. Sponsor-approved audit + adversarial review (9-agent workflow). **OPTION C UPHELD** — an
  adversarial lens mandated to overturn it on evidence alone could not: Option A is disconfirmed (2× `-10004` on
  the shared host), Option B unsupported (#12 proves Session 0 *can* succeed), and the deciding **rate is still
  unmeasured**; §7 threshold refined to also require the shared-load/concurrency sub-test before Option A.
  **Terminology (WS-C):** `login_timeout` copy no longer says "the broker didn't respond" (it is transport-
  ambiguous — neutral wording now); `validation_busy` given distinct customer wording + short outcome + explicit
  timeline mapping (agent reached, broker not); `mt5_/bridge_/runtime_unavailable` given a proper short outcome.
  **Classification (WS-C/agent):** `-10005` (RES_E_INTERNAL_FAIL_TIMEOUT — the internal-IPC-timeout sibling of
  `-10004`) now classifies as `validation_ipc_unavailable`, not `login_timeout` (a broker over-attribution);
  agent `manifest.json` regenerated (`2026-08-05.2`). **Consistency (WS-G):** `attempt_public()` no longer
  leaks `correlation_id` to the customer (matches the Phase-3 serializer decision). All other `server_unavailable`
  occurrences audited **Correct** (broker-reached). **Fidelity (WS-B):** per-stage state is DERIVED from
  `reason_code` — documented, incl. the known limitation that pre-WS-A historical rows persisted as
  `server_unavailable` will mis-render (immutable data; not rewritten). **Honesty (WS-B):** `browser_response`
  label now "Returned the result to you" (the backend cannot evidence render). **UI (WS-A):** timeline page
  surfaces the real error to staff (not the customer-sanitised wording), a11y contrast fixes, a ✓/✕/○ legend, a
  correlation-id copy button, shared `Card`. **New docs:** `VALIDATION_RELIABILITY_EVIDENCE_MATRIX.md` (WS-E,
  single source of truth), `operations/broker-connectivity/validation-failure-triage.md` (WS-D, locus diagnosis
  without SSH). make check green. NOT deployed; no Windows-host change; no signing change; no live validations;
  #12/#1 untouched.

- **2026-08-05 — Validation Reliability PHASE 3 (support timeline UI + history + consistency + plan). Repo eng; NOT deployed. 🟢**
  Same branch/PR #289. **Authoritative reliability recommendation = OPTION C (evidence insufficient)** — this
  supersedes any earlier log phrasing (e.g. the 2026-08-02 entry's "next = run in an interactive session / VM"):
  Session 0 both succeeded (#12) and failed (#13), the success/failure **rate is unmeasured**, so no
  architecture change (no dedicated VM) is recommended until the controlled reliability test fails the §7
  threshold. **WS-A** staff **Operations → Validation Timeline** page (`admin/operations/validation-timeline`,
  `useAdminRole`-gated) + `ValidationTimelinePanel`, searchable by correlation / account / attempt id (backend
  `resolve_correlation_id` + extended `GET /api/trading/validation-timeline/`). **WS-B** timeline enriched with
  the committed OperationalEvent projection (read-only, no host access). **WS-C** dual-state (Current /
  Latest-attempt / Last-successful) now also on `AccountCard`. **WS-D** `ValidationHistoryTable` redesigned
  (status icon / time / outcome / summary; correlation-id column staff-only). **WS-E** `VALIDATION_IPC_RELIABILITY_INVESTIGATION.md`
  restructured into Facts / Evidence / Hypotheses / Unknowns / Recommendations, each citing evidence, with an
  executable test plan (pass/fail/evidence/sample/abort/recovery/threshold). make check green. NOT deployed;
  no Windows-host change; no new live validations; #12/#1 untouched.

- **2026-08-05 — Validation IPC misclassification + status-integrity split remediation (WS-A/B/C). Repo eng; NOT deployed. 🟢**
  Branch `fix/validation-ipc-classification` (base `main`). **Root cause (primary host evidence):** the beta
  validation for #13 failed with MT5 **`-10004 "No IPC connection"`** — the MetaTrader5 Python↔terminal *local*
  IPC never came up (Session-0 GUI/window-station readiness), **before any broker contact**; the IS6 broker is
  demonstrably up (operator traded on it manually). The agent then **mis-mapped** that local IPC failure to
  `server_unavailable` (a broker outage). **WS-A (code):** agent `validate_login.classify_init_error` now routes
  `-10004`/IPC-text to a dedicated `validation_ipc_unavailable`, narrows `server_unavailable` to genuine
  broker-server-reached evidence, and sends ambiguous connection text to `could_not_verify`; backend `_TAXONOMY`
  registers the new reason (UNAVAILABLE/retryable); frontend shows a customer-safe *"couldn't start the secure
  broker-validation session … details weren't rejected … try again later / contact support"* with **no
  immediate-retry** button (retry-storm prevention); agent integrity `manifest.json` regenerated (version
  `2026-08-05.1`). **WS-C (code):** `run_broker_validation` no longer downgrades a durable **VALIDATED** status
  on a NON-authoritative outcome (busy/host-IPC/transient) — this fixes the #12 flip (a `validation_busy` retry
  had flipped Customer Zero VALIDATED→TECHNICAL_ERROR); the failed attempt is still recorded/shown as the latest.
  **WS-B (diagnosis only, NO host change):** `docs/VALIDATION_IPC_RELIABILITY_INVESTIGATION.md` — #12-vs-#13
  artefact comparison, ranked IPC hypotheses (Session-0 intermittency strongest), a credential-free controlled
  test plan + reliability threshold. **Recommendation = OPTION C (evidence insufficient): a dedicated validation
  VM is NOT recommended yet** — Session 0 both succeeded (#12) and failed (#13), so the reliability rate is
  unquantified; recommend a VM only if the controlled test fails the threshold.
  `make check` green (backend 2652 / frontend 126 / lint 0-err). **NOT deployed; no Windows-host change; #12/#1
  untouched.**

- **2026-08-05 — Validation-UX packet (Sponsor-directed). Repository engineering; NOT deployed. 🟢**
  Branch `fix/validation-ux-timeout` (base `main`), PR #288 — extends the earlier "Failed to fetch" fix into a
  complete validation interaction. **(1) Root cause** of the customer-visible `Failed to fetch` = gunicorn
  `--timeout 120` < the synchronous `VALIDATE_LOGIN` budget (backend read 175s > agent floor 165s > MT5
  120+45): the worker was killed mid-request, the connection reset, and the browser surfaced the raw
  `TypeError`. **(2) Backend** — `Dockerfile` gunicorn `--timeout 190` (> 175) + import-time contract assert +
  `tests_gunicorn_timeout_contract.py`. **(3) Frontend** — validation now runs in a **modal** (spinner + "up
  to two minutes", background disabled by the overlay, page never hangs); the result shows a customer-safe
  message with a **contextual next action** (Try again / Replace credentials) or guidance — never a dead-end
  dismiss, never a raw transport/DRF/model string (`api.ts` tags fetch-rejections `kind:"network"`;
  `toCustomerError` maps + deny-lists). **(4) Duplicate-click guard** (in-flight ref → one attempt per click).
  **(5) Graceful reconnect** — on a dropped connection, `recoverAttemptAfterTransportFailure` re-fetches
  history and shows the REAL committed result instead of a transport error. **(6) Timeout-chain audit** —
  `docs/VALIDATION_TIMEOUT_CHAIN.md`: browser→Traefik→gunicorn→Django→agent→MT5 verified consistent; **Traefik
  needs no change** (writeTimeout/responseHeaderTimeout unlimited; the request empirically survived past any
  shorter proxy bound to gunicorn's 120s kill). `make check` green. **NOT deployed** — deploy = a separately
  gated backend image rebuild (gunicorn 190) + frontend rebuild; Customer Zero #12 and live account #1
  untouched.

- **2026-08-05 (later) — Browser-product feedback incorporated (Sponsor-directed), still DARK. 🟢** On the same
  branch: `/broker-accounts` removed as a journey (redirect only); navigation now tells ONE ordered story via a
  default-open "Get started" group (**Broker Accounts → Marketplace → Live Trading**), each destination
  de-duplicated; the account label/page-H1 unified to "Broker Accounts"; every marketplace blocked state
  explains what's missing + the next action (generic cards: "add a trading account" + link; entitlement denial
  plain), and the dead affordances (Preview no-op, "Preview metrics unavailable" strip, empty "Structure"
  filter) removed. `docs/product/beta-journey-consolidation.md` reframed so **the browser is the product
  specification** (§4 authoritative). New `marketplace/blocked-states.test.tsx` (2). Re-validated green + a
  second adversarial review; still NOT merged/deployed, no flag armed.

- **2026-08-05 — Customer Journey Consolidation & Telegram Readiness. Repository engineering; DARK; flags OFF. 🟢**
  Branch `feat/ipr-journey-consolidation` (base main `dcea807`, 6 commits; NOT merged). **(A)** `/accounts` is
  now the SINGLE canonical broker-account page (WP4 broker journey when the build flag is ON, legacy content
  when OFF); `/broker-accounts` + `/broker-accounts/[id]` **permanently redirect** to `/accounts` + the new
  `/accounts/[id]`; one nav entry; loop-safe (reverses the ADR-0031 AREA-C redirect). **(D/E)** new read-only,
  ownership-scoped `GET /api/strategies/strategies/signal-copy/readiness/` (`_signal_copy_readiness`, reuses the
  EXACT arm gates incl. cohort + single-tenant, so the panel can never over-promise) + `SignalCopyReadiness`
  panel replacing the opaque "Not armed" with a ✓/✕ checklist + one customer-safe next action; the 7-state
  customer account status model + full backend→state map + acceptance journey documented in
  `docs/product/beta-journey-consolidation.md`. **(G)** five arm/toggle refusals that collapsed to a generic
  "try again" now map to their own customer-safe copy. **(I)** removed operator/backend terminology + raw slugs
  from the marketplace + accounts copy. `make check` green (backend **2633**, frontend **96** vitest, parity 42
  routes/49 components, build OK). Independent adversarial review found ONE readiness/arm divergence
  (single-tenant) which was fixed + test-pinned; no HIGH remaining. **Nothing deployed, no flag armed, no order
  path; legacy/Nuno behaviour byte-identical with flags OFF.**

- **2026-08-04 — WP6A Shared-Environment Operational Certification. Non-destructive; DARK; flags OFF. 🟢**
  Certified the engineered broker-connectivity capability BEHAVES CORRECTLY in the shared environment by
  EXECUTING the merged test suite (no destructive testing / no failure injection / no concurrency / no live
  accounts / no flag enablement, per the WP6A boundary): **387 backend tests across 19 modules + 46 frontend
  Operations-UI tests = 433, all OK**; full `make check` green. New `docs/operations/broker-connectivity/`:
  `wp6a-certification.md` (WS A–I record, per-module counts), `wp6a-certification.json` (machine-readable;
  module counts sum-checked), `wp6a-pilot-recommendation.md`. Validation test
  `backend/operational_events/tests_wp6a_certification.py` (10 checks). **Verdict = GO WITH CONDITIONS** for a
  tightly-controlled Internal Pilot (≤5–10 users, demo-only, manual, execution gate MAY stay OFF) — primary
  condition = broker-login HOST certification (first live demo VALIDATE_LOGIN failed at an ACL gap; ADR-0027
  Phase 2 not host-certified) + verified DB backup + operator confirmation of HOST-VERIFIED items. **WP6B
  (multi-tenant isolation / concurrency / load / capacity / failure injection / recovery) remains
  OUTSTANDING — NOT claimed complete.** Does NOT authorise Trusted Beta / arming / invitations.


- **2026-08-04 — WP6 Multi-Tenant Certification PLAN. Planning + governance + tests only. Flags OFF, DARK. 🟢**
  Certification programme **design** (no execution — that needs the disposable environment + Sponsor-gated
  runs; no arming/deploy/CZ/production/live accounts). New `docs/operations/broker-connectivity/wp6-*`: README
  + 12 area docs (environment/isolation/concurrency/execution-safety/health/operational-events/operator-workflow/
  failure-injection/recovery/rollback-rehearsal/capacity/release-recommendation) + machine-readable
  `wp6-test-matrix.json` (every case PLANNED), `wp6-evidence.json`, `wp6-release-gate.json` (GO/GO-WITH-CONDITIONS/
  NO-GO decision matrix, `recommendation=null`). Execution-safety (area D) covers **every exposure-opening
  route** in `execution_entrypoints.json` (15 routes, cross-checked); operator-workflow (area G) covers all
  **17** support-playbook workflows; capacity (area K) is entirely `TO BE MEASURED` (no invented thresholds).
  Validation test `backend/operational_events/tests_wp6_certification.py` (16 checks) enforces area/route/
  workflow/gate completeness, no premature PASS/recommendation, schema validity, no-secrets, DARK invariant.
  ADR-0029/0030/0032 gained a WP6-gate note. `make check` green. **WP6 execution NOT run; nothing armed.**

- **2026-08-04 — WP5.4 Trusted-Beta Operations Readiness & Arming Runbook. Docs + governance only. Flags OFF. 🟢**
  Repository documentation + operational controls + validation tests only — **nothing armed, nothing
  deployed, Customer Zero + production untouched.** New package `docs/operations/broker-connectivity/`:
  README, feature-flags(.md/.json), arming-runbook, rollback-matrix, incident-response, support-playbook,
  monitoring-spec, trusted-beta-readiness, evidence-pack, readiness-checklist.json. Defines the definitive
  6-flag inventory (all default OFF), the only permitted arming order (observe → onboard → converge → enforce
  → invite, execution gate gated on WP6 PASS), rollback for every partial-arming state, SEV-1/2/3 incident
  model, 17 support workflows, monitoring signal specs (all thresholds `TO BE BASELINED DURING WP6`), and the
  WP6 / Trusted-Beta entry-exit + capacity framework. Validation test
  `backend/operational_events/tests_wp54_readiness.py` (17 checks) enforces flag coverage/defaults, checklist
  fields, arming-step rollbacks, partial-state coverage, doc existence, no-secrets, and that WP6 stays
  not-authorised. ADR-0029/0030/0032 amended with the operational arming contract. `make check` green.

- **2026-08-04 — WP1B/WP2 Workstream E — Execution-Safety Closure. Flags OFF, DARK, additive. 🟢**
  The final WP1B/WP2 increment: definitive authoritative-route inventory + refusal-handling parity +
  runtime-lifecycle classification, unblocking release certification. All new enforcement behind
  `BROKER_CONNECTIVITY_EXECUTION_GATE` (+ `_HEALTH_ENABLED`), default OFF, transparent when OFF.
  **Inventory:** `backend/execution/execution_entrypoints.json` classifies every create/dispatch/retry/
  start/resume/recover/activate route (no UNKNOWN, no FIX_REQUIRED) + a **drift CI guard**
  (`tests_execution_entrypoints.py`) that fails on a new un-inventoried `ExecutionJob` creation site or a
  job-type mismatch. **Central fix — `next_job` claim-boundary dispatch gate:** the final-dispatch gate
  lived only in the ingest worker, so a direct host-bridge poller bypassed it; WSE enforces
  `evaluate_dispatch_gate` at the authoritative `next_job` claim boundary (an ineligible exposure-opening
  job is FAILED under the row lock, audited in autocommit, 204) → **no claimer/transport** (ingest worker,
  `mt5_signal_bridge`, `mt5_demo_bridge`, or any future executor) can dispatch an ineligible order.
  **Parity:** h4 scheduler brought to h1/m5 refusal handling (durable `EXECUTION_GATE_REFUSED` re-emit +
  projection, `bar_close_iso`-deduped; h1/m5 now dedup too); demo test-order (`PLACE_TEST_ORDER`, opens
  REAL exposure) now uses the enforcing `require_execution_gate` + `require_not_broker_paused` (durable
  audit + clean 503); promotion `_validate` gained an `is_broker_paused` pre-check (voids the plan → no
  PLANNED-slot leak). **Runtime-start = broker-INDEPENDENT / opens no exposure → DOCUMENTED exempt** (a
  runtime-start gate is deliberately NOT added — would break the CZ RUNNING journey; the authoritative gate
  is `ExecutionJob.save` + dispatch). **Resume-proof hardened** (allowlist + positive control + behavioural
  pause-never-clears + split-string scan; still no automatic caller). **Reason codes** consolidated on the
  `SR_*` canonical set (credential verb → `credential_replaced`; no new codes). **Concurrency** verified
  (deadlock-free in shipped autocommit config; zero reads when OFF; stale resume can't clear a newer
  pause). 21 new WSE tests. ADR-0029 amended. CZ / production UNTOUCHED.

- **2026-08-04 — WP5.2 Operational Event Source Wiring — repository engineering, DARK, additive. 🟢**
  Connected the existing authoritative broker-connectivity emit points to the WP5.1 operational-event
  projection through ONE central `operational_events/broker_projection.py` (owns category/severity/
  customer-visibility/summary/metadata-allow-list/dedup/source). **Projection only** — no new business
  event, no logic moved, no runtime/validation/execution/pause/resume/credential behaviour change; nothing
  reads OperationalEvent. **Safety core:** every projection is `transaction.on_commit(record_event)` at the
  DURABLE emission point — NEVER inline in an authoritative `atomic()` (verified Postgres hazard: a caught
  INSERT error still aborts the surrounding tx). on_commit is discarded on rollback (no phantom) and runs
  immediately in autocommit (durable); each `project_*` early-returns when `OPERATIONS_EVENTS_ENABLED` is
  OFF (zero extra work), wraps registration fail-open, and binds pre-computed scalars. Audit stays
  authoritative — the recorder never writes `core.audit`. **Wired (each at its durable point):** validation
  (`run_broker_validation`), health net-transition + credential-invalidation (`broker_health`), credential
  replacement + disconnect (`broker_connectivity`), pause/resume (`runtime_pause._audit`/`_resume_audit`
  choke points), execution creation refusal (`broker_gate._audit_refusal` + h1/m5 scheduler re-emit —
  mutually exclusive → one event per logical refusal), dispatch refusal (`_audit_dispatch_refusal`),
  broker-gate promotion rejection (`signal_promotion`). DISCONNECTED→ERROR (not inflated to CRITICAL);
  EXECUTION/operator-only; VALIDATION/HEALTH/CREDENTIAL-replaced/CONNECTIVITY/paused/resumed customer-visible.
  **Documented out-of-scope** (no existing durable audit to mirror): h4 scheduler (= WP1B/WP2-E h4 parity),
  PLACE_TEST_ORDER, view-pause-block. 30 new projection tests (per-source + dedup + DARK + rollback-no-
  phantom + autocommit fail-open via TransactionTestCase); source-coupling resume guard kept strict (renamed
  event-type literals, not weakened). ADR-0032 amended. CZ / production UNTOUCHED.

- **2026-08-04 — WP5.1 Operational Event Model — repository engineering, DARK, additive. 🟢**
  Sprint-4 pivot to operational readiness (WP5). Built the authoritative operational-event foundation the
  future dashboards / support tooling / monitoring will consume — **no UI, no scheduler, no notifications,
  no background jobs, no deployment, no source wiring, no runtime/validation/execution change**. New
  `operational_events` Django app (ADR-0032; named to avoid colliding with reliability's admin
  `operations-summary`) behind `OPERATIONS_EVENTS_ENABLED` (default OFF, read-live tolerant parser). As-built:
  `OperationalEvent` model = a query-optimised, NON-SECRET, owner-scoped, **derived/rebuildable projection**
  (a cache per data.md — explicitly NOT a second immutable audit; `core.audit` stays authoritative) with 7
  categories (VALIDATION/HEALTH/EXECUTION/RUNTIME/CREDENTIAL/CONNECTIVITY/SYSTEM), 4 severities
  (INFO/WARNING/ERROR/CRITICAL; `normalize_severity` reconciles upstream WARN/DEBUG), composite indexes, and a
  **partial-unique `dedup_key`** for idempotency. Single **recorder** `record_event` (DARK no-op, fail-open,
  idempotent via get_or_create, secret-safe metadata sanitiser) + `mark_resolved`; **query service**
  (timeline/recent/latest/open/customer_visible/operator_visible/summary — DTOs only, never ORM rows);
  **deterministic hybrid summary** (live authoritative state read READ-ONLY from WP1A/WP3/WP2 + timeline
  aggregates; fail-open); immutable **frozen-dataclass DTOs**; one read-only **owner-scoped API**
  `GET /api/operations/account-events/?account_id=` (IsAuthenticated, IDOR-safe 404, staff bypass, **404 while
  DARK**, returns `{summary, timeline}`; non-staff see customer-visible only). The existing broker emitters are
  NOT wired to record in this packet (documented as the next increment in ADR-0032). 38 focused tests
  (ordering/pagination/summary/severity/visibility/reason-codes/empty/multi-account/ownership/DTO-immutability/
  API/dedup-constraint/DARK-no-op). Footprint = new app + 2 registration lines (INSTALLED_APPS, root urls).
  Customer Zero / production / prod frontend UNTOUCHED.

- **2026-08-03 — Validation-runner task-trigger PERMISSION FIX → TASK_TRIGGER_PASS (credential-free). 🟢**
  BOUNDED engineering + host remediation. Prior packet's DACL run-grant (#259, `0x1200a9` on GvfxValidationRunner) was applied+verified yet the agent STILL could not trigger the runner (`validation_runner_unavailable`, ~0.2s, `LastResult=0x00041303 SCHED_S_TASK_HAS_NOT_RUN`). **Diagnosed on host (read-only first):** ruled out stale COM handle (agent restart — no change) and `RunLevel=Highest` elevation guard (flip Highest↔Limited — no change; both trigger once staging works). **TRUE ROOT CAUSE = handoff-dir ACL.** `TaskLaunchLoginValidator.validate()` stages the sealed request via `handoff.write_request()` BEFORE `task.Run()` (validate_login.py:338→343); the handoff dir `C:\GuvFX\beta\agent-state\validation-handoff` granted **SYSTEM + Administrators only**, and the least-priv `NT SERVICE\GuvFXBetaAgent` is neither → write failed closed → `validation_runner_unavailable` at line 341, trigger never reached (dir was empty, no `_handoff_hmac.key` — agent had NEVER written there). The #259 task-DACL grant was necessary but not sufficient. **FIX (PR #260, branch fix/validation-runner-handoff-acl, commit 030d0b2):** `install_validation_runner.ps1` computes the agent service SID once and grants it `(OI)(CI)(M)` on the handoff dir alongside SYSTEM/Administrators FC, with a read-back assertion; task principal UNCHANGED (SYSTEM/Highest/ServiceAccount — GUI-proven & trigger-proven); 2 comment em-dashes → ASCII (RULE 9). Sealed ciphertext only (agent already holds it in the op payload) → no new plaintext; all other users denied. **HOST-CERT (credential-free, installer re-run from repo, parse-gate OK, NO ad-hoc ACL, NO agent restart):** synthetic garbage-envelope VALIDATE_LOGIN → `TRIGGER_WORKED=True`, runner returned `credential_unsealable` (expected — fails before terminal launch), task ACTUALLY RAN (`LastRunTime=08/03 06:35:48`, `LastResult=0x00000000`); `handoff_residual=0`, `stray_validation_terminal=0`, no `accounts.dat`. Full chain proven: Agent→write_request→win.run_task→GvfxValidationRunner→runner→result→cleanup. **NO real creds, no broker login, no trade.** CZ pid316 alive, agent Running as NT SERVICE\GuvFXBetaAgent, all 9 prod containers Up/healthy — CZ/slots/prod UNTOUCHED. `make check` GREEN. **VERDICT: TASK_TRIGGER_PASS.** NEXT (separately authorised) = the one credentialed VALIDATE_LOGIN (acct#12) through the now-triggerable task-launched runner.


- **2026-08-03 — Final live VALIDATE_LOGIN via task-launched runner → UNAVAILABLE / validation_runner_unavailable (task-trigger PERMISSION gap; NOT credential/GUI). 🟠**
  One credentialed VALIDATE_LOGIN (acct#12) through the deployed task-launch path. Pre-flight clean (CZ RUNNING/13ev/blv=False; acct 1302575/IS6Technologies-Demo/is_demo=True; backup pre-finalvalidate-20260803T055546Z sha 98411df2). **Task-launch VERIFIED credential-free:** agent WinSW env has BETA_AGENT_VALIDATION_TASK_NAME=GvfxValidationRunner → agent builds TaskLaunchLoginValidator (not legacy); runner SYSTEM-task config resolves login_timeout_ms=120000 + terminal_dir + matching handoff dir; baseline 37DA8444; handoff empty. Timeouts aligned (runner 120s / agent read 120s / transport 130s). **RESULT: UNAVAILABLE / validation_runner_unavailable, 0.37s, is_demo=null.** Credential accessed+audited but **the agent could NOT trigger the runner task** (`win.run_task`→False): GvfxValidationRunner LastTaskResult=0x00041303 SCHED_S_TASK_HAS_NOT_RUN, no custom SDDL → default task security grants run-rights to Administrators+SYSTEM but NOT the least-priv NT SERVICE\GuvFXBetaAgent. So the runner NEVER launched → no MT5, no broker contact, no accounts.dat, handoff empty. **This is a DEPLOYMENT task-permission gap, not a code/GUI/credential fault; the GUI fix is unexercised because the task never started.** blv stays False. CLEANUP: baseline 37DA8444 (untouched), monitor/instrumentation removed, THIS packet's 120s WinSW+machine env reverted (task-name+handoff env kept), GvfxValidationRunner task kept, agent healthy. CZ+prod IDENTICAL (Trade 440/378, ExecJob 20991/20973, no drift; no trade/order). VERDICT BROKER_LOGIN_VALIDATION_UNAVAILABLE. **Smallest remediation = grant NT SERVICE\GuvFXBetaAgent Read+Execute (run) on the GvfxValidationRunner task security descriptor — mirror the slot-runtime tasks (GuvFXBetaRuntime-1..4) which the agent already triggers — in install_validation_runner.ps1; then re-run the single credentialed validation.**


- **2026-08-03 — Task-launched validation runner IMPLEMENTED + REVIEWED + CI + MERGED + DEPLOYED + GUI HOST-CERTIFIED (credential-free). 🟢**
  Engineering remediation of the login-validation GUI failure (root cause: MT5 GUI/MDI creation fails when launched IN-PROCESS by the WinSW service; succeeds via a scheduled task). **Impl (PR #258 merged, main bc086d7):** validation_handoff.py (ACL-restricted, HMAC-authenticated, single-use, expiry, auto-delete local handoff — SEALED ciphertext only, no secret via task cmd/args/env/registry/logs); validation_runner.py (task entrypoint: claim→in-process probe (GUI-capable)→envelope-open at point-of-use→secret-safe {ok,reason_code,is_demo}→scrub accounts.dat+logs); validate_login.TaskLaunchLoginValidator (delegates, one-at-a-time) + build_inprocess_handler; agent._build_login_validator delegates when task configured, triggers ONLY via win.run_task (no subprocess in agent.py); config +2 keys; manifest +2 pinned modules; taxonomy validation_runner_unavailable/_timeout→UNAVAILABLE; install_validation_runner.ps1 (single-instance, allow-listed, no-args task + ACL'd handoff). **30 new tests; make check GREEN; CI GREEN; 12-lens adversarial review (fixed cleanup-§6 credential-artefact-removal gap).** **DEPLOY:** agent bundle re-staged (integrity_ok 19 modules, RULE-9 parse-gate PASS, manifest 2026-08-02.2); GvfxValidationRunner task registered (Ready, IgnoreNew, fixed no-args cmd, SYSTEM); handoff dir ACL SYSTEM+Administrators; agent config via WinSW env (task_name+handoff_dir); agent restarted; NEGOTIATE confirms new bundle live (VALIDATE_LOGIN advertised, manifest 2026-08-02.2). **HOST-CERT (credential-free): SYSTEM task (GvfxValidationRunner identity, Session 0) launched the validation terminal → mdi_create_failed=0 + main window created → THE TASK-LAUNCH CONTEXT BUILDS THE MT5 GUI (fix proven on-host).** CLEANUP: validation terminal restored 37DA8444 (no accounts.dat); artefacts removed; handoff empty. CZ UNCHANGED (acct#12 RUNNING, blv=False, 13ev/1VR); Trade/ExecJob +1/+5 = normal live acct#1 production (NOT this deploy — credential-free, no order, no CZ touch). WinSW .bak-preTaskLaunch kept for rollback. NEXT = separately-authorised single credentialed VALIDATE_LOGIN via the deployed task-launched runner.


- **2026-08-02 — GUI-init investigation: ROOT_CAUSE_CONFIRMED = the agent's in-process WinSW-SERVICE execution context (non-interactive service window station), NOT Session 0. 🟢**
  Read-only + credential-free (NO VALIDATE_LOGIN/creds) on disposable copies. Backup pre-guiinvest-20260802T223117Z.sql.gz (sha 596dda77). **Exp A (config/file audit) REFUTES missing-files:** the failing precompiled terminal HAS all CHART01-04.CHR (Profiles\Charts\Default present) — identical to CZ's WORKING slot-2 terminal; golden lacks them (first-run generated). resource-131/MDI = terminal64.exe INTERNAL window templates, not files. **Exp E (discriminating control) is decisive:** the SAME disposable copy, credential-free — **(S0) SYSTEM scheduled task in Session 0 → GUI BUILDS FINE (mdi_create_failed=0, main window `MetaTrader 5 - Netting - EURUSD,H1` present); (INT) Administrator interactive task → GUI fine** — whereas the last packet's **agent WinSW-service in-process launch → MDI create failed.** ⇒ **SESSION 0 REFUTED as the cause** (a Session-0 SYSTEM task creates the GUI). The GUI/MDI failure is specific to MT5 launched **in-process by the WinSW SERVICE (NT SERVICE\GuvFXBetaAgent)** — a non-interactive service window station where MT5 window creation fails. Corroboration: the beta SLOT runtimes are launched via scheduled tasks and have WORKING GUIs (incl. CZ pid316). Agent-identity-via-task test inconclusive (0x1, service-window-station/ACL), doesn't change the finding. **VERDICT ROOT_CAUSE_CONFIRMED** (positive control = same copy builds GUI via SYSTEM/interactive task; correcting the launch context eliminates the MDI errors). **SMALLEST REMEDIATION (no VM, no new session arch): change ADR-0027 to LAUNCH the isolated validation MT5 via the existing task-based runtime-launch mechanism (as slot runtimes are — scheduled task → usable window station), NOT in-process from the WinSW service; then re-run the credentialed validation.** CLEANUP: _expE + tasks + temp removed; original validation terminal UNTOUCHED (fp 37DA8444, no accounts.dat, ACL intact); no stray. CZ+prod IDENTICAL (Trade 430/368, ExecJob 20666/20648, all pids, 8788/8791). No order/trade/credential/secret-print.


- **2026-08-02 — Broker login timeout LOCALISED = FAILED_TERMINAL_UI (Session-0 MT5 GUI/MDI creation failure, directly journalled). Verdict UNAVAILABLE. 🟠→🟢(localised).**
  One instrumented credentialed VALIDATE_LOGIN (acct#12 IS6Technologies-Demo/***575) via the ADR-0027 path, precompiled baseline (fp 37DA8444) + agent 120s (WinSW env, reverted after) + live SYSTEM Session-0 monitor + shared-read journal capture. Backup pre-locattempt-20260802T221407Z.sql.gz (sha bff5d4bf). **RESULT: UNAVAILABLE / login_timeout, 130.24s, is_demo=null** (same outcome, now EXPLAINED). **EVIDENCE (secret-safe): the login got FAR** — monitor t=4.1s: validation terminal launched (pid5696 Session-0), **IPC pipe MT5.Terminal.B609FC22 CREATED**, **accounts.dat written** (4635B), **broker TCP ESTABLISHED 194.164.179.28:443**. **Journal (25 lines, all within ~1s of start) is decisive:** `MetaTrader 5 build 6073 started` → `Window MDI unhook/create failed` → `Document create frame from resource 131 failed` → `create new frame CHART01-04.CHR failed` → **STOPS** (no connect/authoriz/login line ever). ⇒ **the MT5 terminal cannot create its GUI (MDI chart windows) in the agent's Session 0 (no interactive desktop); it never reaches a functional logged-in state, so initialize() times out.** STAGE-LOCALISATION VERDICT = **FAILED_TERMINAL_UI**. Last proven stages: launch✓ IPC-pipe✓ accounts.dat/server✓ broker-TCP-established✓; first failing = **UI/MDI initialization** (Session-0); login authorization + account_info never reached. **This DIRECTLY EVIDENCES the Session-0 GUI limitation (upgrades prior 'indicated' to journalled fact); credential validity remains INDETERMINATE (never authorised).** blv stays durably False (probe doesn't persist). accounts.dat credential artefact written → cleanup REMOVED it. CLEANUP: stray pid5696 killed; terminal restored to 37DA8444 (accounts.dat gone); agent env reverted+restarted to found 30s; _loc/monitor/task/temp removed. CZ+prod IDENTICAL (Trade 430/368, ExecJob 20666/20648, all pids, 8788/8791, NEGOTIATE VALIDATE_LOGIN advertised). No order/trade/secret-print. Next (smallest remediation) = run the validation MT5 in an INTERACTIVE session (Option F dedicated VM or interactive host session) where GUI/MDI initialises, then re-run the same validation.


- **2026-08-02 — First live credentialed VALIDATE_LOGIN reattempt (precompiled baseline + 120s): BROKER_LOGIN_VALIDATION_UNAVAILABLE / login_timeout. 🟠**
  Authorised RED single live VALIDATE_LOGIN via the deployed ADR-0027 path against Customer Zero's stored encrypted demo credential (acct#12 IS6Technologies-Demo/***575). Never saw/decrypted/printed the password/ciphertext/keys. Pre-flight clean (CZ acct#12 RUNNING/13ev/1VR blv=False/no active jobs; account non-secret verified: number 1302575, is_demo=True, password_enc present, plaintext empty, resolver→IS6Technologies-Demo; backend pubkey-only/no privkey; keyring lives in guvfx-beta-provisioner). **Fresh verified PG backup** pre-firstlogin-reattempt-20260802T214758Z.sql.gz (5245626B, sha 5e11ae10…, gzip OK). **Precompiled validation baseline** built in place (dismiss wizard→finish 129s recompile→stop→strip logs), fp 37DA8444, 131 .ex5, NO accounts.dat, agent Modify ACL intact, restore copy at _precompiled. **Agent 120s** via WinSW `<env>` BETA_AGENT_LOGIN_TIMEOUT_MS=120000 + Restart-Service (clean; CZ pid316 unchanged); NEGOTIATE confirmed VALIDATE_LOGIN advertised. **Invocation** = BrokerLoginValidator().validate(acct12) inside guvfx-backend (has code+pubkey+Fernet) with keyring/base_url injected from provisioner via never-echoed shell vars + BETA_AGENT_OP_TIMEOUTS={"VALIDATE_LOGIN":130}. **RESULT: UNAVAILABLE / login_timeout, retryable, is_demo=null, server=IS6Technologies-Demo, login_masked=***575, DURATION 130.22s** (ran the full 120s → 120s config took effect). Credential accessed+audited (CREDENTIAL_ACCESSED acct12) + sealed + sent, but the MT5 login DID NOT COMPLETE in 120s → **credentials NEVER validated (no broker accept/reject)**. Confounds eliminated: recompile (precompiled) + 30s→120s timeout; login still times out ⇒ remaining candidate = the agent's Session-0 in-process MT5 execution context (INDICATED, not proven — per evidence boundary I do NOT claim MT5 IPC requires a login). **PERSISTENCE:** blv stays durably False (probe does not persist; correct); the attempt WROTE a credential artefact config\accounts.dat which cleanup REMOVED. **CLEANUP:** stray Session-0 validation terminal (pid14992) path-verified-killed; terminal restored to precompiled baseline fp 37DA8444 (accounts.dat gone); agent WinSW env reverted + restarted to found 30s (CZ unchanged); _rib/_orig585/temp/tasks removed (_precompiled kept). **CZ + prod IDENTICAL** (RUNNING, 13ev/1VR blv=False, Trade 430/368, ExecJob 20666/20648, all pids alive, 8788/8791 up); no order/trade. VERDICT **BROKER_LOGIN_VALIDATION_UNAVAILABLE**. Next = smallest platform remediation: localise where the 120s login is spent (IPC-establish vs server-connect vs authorize vs wizard) via a journal-instrumented single credentialed attempt.


- **2026-08-02 — Run-in validation baseline: RUN_IN_BASELINE_FAIL. Recompile IS eliminable; the first-run wizard is NOT (intrinsic to an account-free MT5). 🟡**
  Authorised bounded credential-free experiment on COPIES of the validation terminal (original `terminal` never touched; NO login/creds/server; NO CZ/prod/bridge/golden change). Pre-flight clean (orig fp D29C2E7E; CZ acct#12 RUNNING/13ev/1VR blv=False/no active jobs; Trade≤430; ExecJob 20666). **Phases:** copy→run-in(dismiss "Open an Account" via WM_CLOSE, no account)→let ~90–130s recompile finish→graceful close→classify delta→strip logs→fingerprint→fresh-copy launch→restore. **RESULTS: (1) RECOMPILE ELIMINATED** — a fresh copy of the run-in baseline launches with **+1 file only** (just the log), **no MQL5 re-extraction, no recompilation**, main window at **~5–7s** (vs ~90–130s clean first-run). **(2) WIZARD NOT ELIMINABLE** — the "Open an Account" modal REAPPEARS on every fresh launch even after a GRACEFUL state-saving exit; `terminal.ini` carries no suppression key. It is intrinsic to an account-free terminal (dismissable at runtime via WM_CLOSE, not pre-bakeable). **(3) NON-DETERMINISTIC bake-to-bake** — two independent run-ins gave different fingerprints (91E45689 vs C455C93B); copy-deterministic but not reproducible-from-scratch (embedded timestamps/caches vary). Delta classification (636 added): 131 .ex5 (compiled), 396 .mq5/.mqh + 85 other = standard MetaQuotes MQL5 library, 8 .hcc market-data, 9 Bases caches, 7 Config (agents/dnsperf/servers + 4 .ini), **0 logs, 0 credential artefacts (NO accounts.dat)**. NOTE (corrects prior claim): `dnsperf.dat` WAS created credential-free this run → it is a DNS/server-list cache, NOT proof of login (Sponsor was right to flag this). Broker-login requirement remains UNPROVEN and is NOT claimed. **VERDICT: RUN_IN_BASELINE_FAIL** (wizard not absent + non-reproducible). Original terminal restored/untouched (585, fp D29C2E7E, ACL unchanged); _rib + all tasks/scripts removed; CZ + prod IDENTICAL (Trade/ExecJob unchanged); no trade/credential/login.


- **2026-08-02 — Wizard dismissal + credential-free IPC: POSITIVE_CONTROL_NOT_OBTAINED. Wizard is necessary-not-sufficient; MT5 IPC needs a broker connection. 🟢**
  Authorised bounded credential-free experiment on the ISOLATED validation terminal (no login/creds/server; NO CZ/prod/bridge/golden change). Pre-flight clean (CZ acct#12 RUNNING/13ev/1VR blv=False/no active jobs, Trade≤430, ExecJob≤20663, val fp D29C2E7E). **Dismissed the first-run "Open an Account" modal via `WM_CLOSE`** (pure Close — NO account/creds/server; `accounts.dat` never created). Result: modal gone, main window present, **new pipe `MT5.Terminal.B609FC22…` created** (wizard WAS blocking pipe creation). BUT credential-free `initialize(path,portable)` STILL `-10005`: (v1) attach during recompile → fail; **(v2) past-wizard + fully recompiled (1210 files) + pipe present, 3 attach attempts t=160/187/214s → all -10005**; (v3) package-launch + background dismisser + 110s timeout → -10005. **Filesystem delta (run-in vs 585 baseline): +625 files (131 .ex5, Bases/, run-in Config/, logs), 1 changed (servers.dat), NO accounts.dat, and crucially NO `dnsperf.dat`** — CZ HAS dnsperf.dat (created only on broker-server CONNECT); my offline terminal never made it. ⟹ **MT5 Python-IPC readiness requires a broker-server connection (login), not merely clearing the wizard; a purely credential-free positive control is unobtainable.** **VERDICT: POSITIVE_CONTROL_NOT_OBTAINED.** **Architecture consequence:** ADR-0027 (credentialed `initialize(login,password,server)`) is the CORRECT mechanism (matches the working bridge→logged-in-IS6 pattern). NEW actionable insight: the earlier live `login_timeout` was likely the **~90s first-run recompile** vs the probe's 30s login timeout — fix = re-bake golden as "RUN-IN" (past-wizard + 131 .ex5 precompiled, NO accounts.dat/dnsperf) so fresh copies start fast, and/or raise login timeout. Session architecture stays PAUSED. CLEANUP: validation terminal restored EXACT (585, fp D29C2E7E matches, ACL unchanged); tasks/_probe/temp removed; CZ+prod IDENTICAL to pre-flight (Trade/ExecJob unchanged); no trade/credential/login.


- **2026-08-02 — MT5 Python-IPC positive-control investigation: ROOT CAUSE = first-run "Open an Account" modal on the pristine golden terminal. 🟢 (session/co-tenancy conclusions WITHDRAWN).**
  Bounded credential-free investigation (read-only + isolated validation terminal only; NO login/creds/server/VALIDATE_LOGIN/CZ/bridge/order). Pre-flight clean (CZ acct#12 RUNNING, 13 ev/1 VR blv=False/no active jobs, Trade≤430, ExecJob≤20663; validation terminal 585 files fp D29C2E7E). **Experiment (pre-launch terminal `/portable`, observe, then 6 bounded attach attempts 15→120s):** ALL `-10005`. **Window enumeration caught the blocker: a VISIBLE persistent modal `#32770 "Open an Account"`** over the main window the whole 120s — the fresh credential-free golden terminal launches into MT5's first-run account wizard and never reaches IPC-ready. Journal confirms: build 6073 started, 91s recompile, then NO network/connect/login/IPC line ever. **Named-pipe enum decisive:** Python IPC = per-instance pipe `MT5.Terminal.<64hex>` created only after ready-state → pristine terminal (stuck on wizard) never creates it → `initialize()` times out. **H-results:** H1 sequencing REJECTED (pre-launch+wait+repeat didn't help — wizard persists); **H2 first-run modal SUPPORTED (direct evidence)**; H3 build-mismatch REJECTED (6073 starts fine); H4 co-tenancy REJECTED (pipes per-instance, IS6+CZ coexist); **H5 22346 MCP INCIDENTAL/REJECTED** (bridge does Python IPC vs IS6 build-5833 which has NO 22346; IS6/bridge hold 0 loopback conns; 22346=AI-assistant HTTP held only by CZ); H6 session — the SESSION-0/4/1 conclusions are WITHDRAWN (wizard blocks in every session). CZ (credential-free, no accounts.dat) IS ready because it was RUN-IN past the wizard (has dnsperf.dat + rich terminal.ini). **VERDICT: POSITIVE_CONTROL_NOT_OBTAINED** (pristine terminal wizard-blocked) — but blocker + mechanism now known. **Smallest next step (needs separate authorisation per the prompt rule): dismiss the "Open an Account" wizard ONCE on the isolated validation terminal (no account/creds/server) to reach run-in state (like CZ), then re-run credential-free `initialize()` → expect True + non-null terminal_info = the RULE-11 positive control; then re-bake golden as run-in.** CLEANUP: validation terminal restored to EXACT baseline (585, fp D29C2E7E MATCHES, ACL unchanged); tasks/_probe/temp scripts removed. CZ + prod IDENTICAL to pre-flight (all pids alive, 8788/8791 up, Trade/ExecJob unchanged). No trade/credential/login.


- **2026-08-02 — Session-4 (guvfx-rdp) validation-host experiment: FAIL, but host-wide (not Session-4-specific). 🟡 RULE-11.**
  Single bounded empirical experiment (read-only + credential-free MT5 IPC only; NO broker login/VALIDATE_LOGIN/CZ/bridge touch). Ran the identical credential-free probe
  (agent-venv `MetaTrader5 5.0.6090` + isolated validation terminal build 6073, `initialize(path,portable=True,timeout)` — no creds) **in Session 4** (session_id=4 proven) →
  `initialize=False, (-10005 IPC timeout)`. **RULE-11 positive control:** SAME probe in **active Session 1** (where the prod bridge does MT5 IPC right now) → **also** `-10005`.
  ⇒ failure is NOT Session-4-specific. Terminal journal: its local MCP listener could not bind `127.0.0.1:22346` (10048 in-use); owner = **Customer Zero PID 316** (golden-derived, Session 0).
  Prod bridge works via older IS6 build-5833 mechanism (no loopback port). **No positive control obtainable** without stopping CZ (out of scope) ⇒ per RULE-11 no per-session verdict is authoritative;
  Session-4 standalone suitability UNPROVEN (confounded by CZ co-tenancy). Strongly indicates the earlier "SESSION0_IPC_LIMITATION_CONFIRMED" was the same co-tenancy confound (inference, not re-proven).
  **Cleanup:** all launched validation terminals path-verified-killed; validation terminal restored to exact 585-file golden baseline (ACL unchanged); tasks + `_probe` scaffolding removed.
  **All 6 prod processes ALIVE & unchanged** (bridge 14604, IS6 4336/8748, CZ 316, agents 10768/6656); 8788/8791 listening. No trade, no credential, no order. Smallest next step: obtain ONE probe positive
  (golden terminal with no other golden terminal co-resident) before trusting any session verdict.


- **2026-08-02 — Option F selected; drafted the governed provisioning packet (credential-free). 🟢**
  Sponsor selected **Option F (dedicated Windows validation VM)**; do NOT repurpose guvfx-rdp or use either
  Administrator session. Authored `docs/PACKET_VALIDATION_ENV_PROVISIONING.md` — the DRAFT governed packet for
  *Customer Zero – Dedicated Validation Environment Provisioning and Interactive IPC Proof*: min VM spec
  (2 vCPU/4 GB/60 GB, Win11 Pro or Server, **no RDS/CALs — single console session**), Tailscale node + ACL'd
  worker port (:8792), low-priv `guvfx_validation` + auto-logon interactive session + startup task, clean
  Golden-copy isolated terminal, `validation_worker.py` (reuses envelope/probe/isolation/HMAC), **worker owns
  the envelope private key on the VM** (backend seal-only; Session-0 agent removed from the credential path),
  separate backend↔worker HMAC scope, no-trade surface, cleanup-to-baseline, monitoring, reboot recovery,
  rollback + Session-0 decommission, **cost ~US$20–45/mo**. Execution = P0 provision → P1 credential-free MT5
  IPC proof → P2 worker + synthetic sealed-payload + exact baseline restore → P3 reboot recovery → **STOP**
  (first live broker validation separately authorised). Credential-free; no host mutation this turn; CZ/prod
  unchanged; repo/CI clean (#219/#58 open).


- **2026-08-02 — Interactive-session architecture reassessment (read-only): dedicated-session path BLOCKED by concurrent-session limit; recommend a dedicated validation environment. 🟠**
  Sponsor created `guvfx_validation` (enabled, Remote Desktop Users) but Windows refused a 3rd interactive
  session — **"too many users signed in."** Evidence: **Windows Server 2025 Datacenter, RDS role NOT
  installed → 2 concurrent admin sessions**; Session 1 (console Admin) + Session 3 (RDP Admin) + Session 4
  (guvfx-rdp RDP disc) already at the cap, so a dedicated `guvfx_validation` RDP session cannot be added
  without disconnecting a session (forbidden) or RDS licensing (forbidden). **Options re-scored:** A
  (dedicated session) BLOCKED; B/E (worker in Session 1/3, which run production MT5) UNSAFE — the MetaTrader5
  package's "connect to a running terminal" behaviour risks the validation init grabbing/breaking the LIVE
  bridge's IS6 connection (pid 14604/8748, Session 1), and it **cannot be tested without risking production**;
  C (agent session-broker) BLOCKED — the agent runs as `NT SERVICE\GuvFXBetaAgent` (virtual account, no
  SeTcbPrivilege for WTSQueryUserToken/CreateProcessAsUser); G rejected. **Key facts:** Session 4 (guvfx-rdp,
  disconnected) is the ONLY existing interactive session that is **MT5-free**; the bridge starts via the
  `GuvFX_SignalBridge` scheduled task in the Admin console session. **Recommendation = Option F: a dedicated
  low-cost Windows validation host/VM** (own interactive session, own isolated terminal + worker + envelope
  key, connects to the same demo broker) — the only option that removes the session limit, the MT5
  coexistence risk AND all production blast radius, and doubles as the SAFE env to prove interactive IPC +
  coexistence without touching the live bridge. No-procurement fallback = repurpose Session 4 (governance +
  guvfx-rdp identity + disconnected-session IPC caveats). **Genuine Sponsor decision = procurement.**
  Read-only only; no host mutation, no credential, CZ/prod/validation-terminal unchanged; guvfx_validation
  left as-is (not deleted). Next = dedicated validation environment provisioning + credential-free interactive-IPC proof.


- **2026-08-02 — Session-architecture investigation: `SESSION0_IPC_LIMITATION_CONFIRMED` (read-only, no credential). 🟢**
  Root cause of the login_timeout is proven, without any broker login. **Session-0 synthetic** MT5
  `initialize(portable=True)` (as `NT SERVICE\GuvFXBetaAgent`, no login) = `false, (-10005 IPC timeout)`
  (4th confirmation). **Interactive-session natural experiment:** the PRODUCTION bridge
  `mt5_signal_bridge.py` (pid 14604) runs in **interactive Session 1** as Administrator and drives MT5 (IS6
  terminal pid 8748, Session 1) live/daily — same machine, same MetaTrader5 package, differing ONLY in
  session. → MetaTrader5 Python IPC requires an interactive desktop/window-station; the Session-0 service
  cannot establish it. Sessions: 1=console/Admin autologon, 3=RDP/Admin, 4=guvfx-rdp(disconnected,no MT5),
  0=services. I did **not** launch a synthetic MT5 in the live bridge's Session 1 (concurrent-connection
  risk to live production returned to Sponsor, not tested). **Recommended design = Option A: dedicated
  low-priv validation identity + auto-logon interactive session + isolated validation terminal + a
  validation worker that holds the envelope private key and runs the probe; the Session-0 agent relays the
  SEALED envelope over a local authenticated channel (never decrypts).** Rejected: B/E (reuse Nuno's
  session/bridge — production risk), D (separate host — disproportionate for Trusted Beta now). Validation
  terminal restored to exact clean baseline (585==golden, no accounts.dat); CZ pid316 + live bridge
  unaffected; provisioner DARK; credentials still untested. Next = Interactive Broker Validation Worker
  Implementation (Phase 1A interactive-IPC proof first).


- **2026-08-02 — Writable validation terminal granted; VALIDATE_LOGIN retry → FAIL again `login_timeout`; ROOT CAUSE now isolated = MT5 Python IPC does not work in Session 0. 🟠**
  Backup `pre-writableacl-20260802T165940Z.sql.gz` (sha256 `a0d404c1…`). Granted **`NT SERVICE\GuvFXBetaAgent:
  (OI)(CI)(M)` on the validation-terminal subtree ONLY** (read-back M+RX; no Full/ChangePermissions/
  TakeOwnership; original ACL saved). **Service-identity probe (as `GuvFXBetaAgent`): `write_inside=OK`,
  `write_golden=DENIED`, isolation code rejects golden/slot2/`..`** — but **MT5 smoke-start FAILED
  `mt5_smoke_init=false, last_error=(-10005, 'IPC timeout')`** even with write. (`write_slot2=ALLOWED` is the
  agent's PRE-EXISTING slot-manager permission — it owns slot2's terminal — NOT granted here; the primitive's
  code independently rejects slot paths.) Restored terminal to clean baseline (585 files == golden). **Final
  single VALIDATE_LOGIN retry (30s, clean, writable): `UNAVAILABLE / login_timeout` again (34.97s,
  is_demo=None).** **ROOT CAUSE ISOLATED: MetaTrader5 Python IPC (`initialize`) does not establish when the
  agent service launches the terminal in Session 0 (services session) — IPC timeout at both the 15s smoke and
  the 30s probe.** The write-ACL was NECESSARY (prior RX-only failed at MT5's data writes) but NOT sufficient;
  the deeper blocker is Session-0 IPC (matches known Session-0 MT5 GUI/desktop limitations). Credentials STILL
  untested (no authentication attempted — IPC never came up). Stray probe terminals (pids 13668/7244) killed;
  **terminal restored to exact clean baseline (585, no accounts.dat, no logs)**; M ACL retained for the
  investigation. **Customer Zero UNCHANGED** (RUNNING, updated 06:18:55, blv False, events 13/VR 1/jobs 2, pid
  316 sess0 started 06:18:54, slot 2 gen 5); watermarks unchanged (trade_max 430, 0 exec); provisioner DARK;
  prod IS6 4336/8748 alive. **VERDICT: BROKER_LOGIN_VALIDATION_FAIL (platform: Session-0 MT5 IPC).**
  **Finding for Failure Investigation:** run the isolated MT5 login probe in an INTERACTIVE session (e.g.
  Session 1 Admin autologon) or connect to a pre-launched interactive validation terminal (as the production
  bridge does) — the agent launching MT5 in Session 0 cannot establish the Python IPC. Next = Broker Login
  Failure Investigation.

- **2026-08-02 — Validation-terminal ACL remediated + service-identity certified; VALIDATE_LOGIN retry → FAIL `login_timeout` (RX-only denies MT5 portable writes). 🟠**
  Backup `pre-aclremediation-20260802T164759Z.sql.gz` (sha256 `af75b024…`). Granted **minimum
  `NT SERVICE\GuvFXBetaAgent:(OI)(CI)(RX)`** on `C:\GuvFX\beta\validation` (original ACL saved to
  `validation_acl_backup.txt`, restorable via `icacls /restore`; read-back confirms RX only, no
  write/modify/delete/full). **Service-identity effective probe (ran AS `GuvFXBetaAgent` via a temp
  scheduled task):** `isfile_exe=true`, `read_exe=OK`, **`write=DENIED`**, `iso_accept=OK`, negative
  controls reject golden/slot2/`..`/missing → `SERVICE_IDENTITY_VALIDATION_TERMINAL_ACCESS_PASS` (closes the
  earlier RULE-11 admin-only gap). Terminal re-certified clean (no accounts.dat, 0 EAs). **Single
  VALIDATE_LOGIN retry: now PASSES the isolation gate and REACHES the MT5 probe (35.19s) → `UNAVAILABLE /
  login_timeout`, retryable, is_demo=None.** **Root cause (read-only confirmed): MT5 in portable mode
  (`portable=True`) must write its data dir; the RX-only ACL denies write, so MT5 launched (stray pid 6684)
  but wrote NOTHING (no accounts.dat, no logs\, 0 files) → login hung → 30s timeout.** Platform failure, NOT
  a credential result; credentials still untested. Stray pid 6684 (path-verified validation terminal)
  terminated; RX ACL left in place (correct/minimal-read); **Customer Zero UNCHANGED** (RUNNING, updated
  06:18:55, blv False, events 13/VR 1/jobs 2, pid 316, slot 2 gen 5); watermarks unchanged (trade_max 430,
  0 exec); provisioner DARK; no order/trade/lifecycle. **VERDICT: BROKER_LOGIN_VALIDATION_FAIL (platform).**
  **Finding for Failure Investigation:** a FUNCTIONING probe needs the agent service account to have WRITE
  to the MT5 data area (config/logs/bases) — the packet's RX-only minimum is insufficient for portable MT5.
  Options: grant Modify on the isolated validation terminal (still disjoint from golden/slot/prod), or a
  per-run writable copy, or redirect MT5 data. Next = Broker Login Failure Investigation.

- **2026-08-02 — First live VALIDATE_LOGIN → FAIL (fail-closed at the isolation gate; RULE-11 service-account ACL gap). 🟠**
  Executed exactly ONE live VALIDATE_LOGIN against Customer Zero's stored demo credentials via the production
  code path (`BrokerLoginValidator.validate`; agent HMAC keyring moved container-to-container, never through
  logs; injected 90s transport). Backup `pre-firstlogin-20260802T162842Z.sql.gz` (sha256 `3d014595…`).
  **Outcome: `UNAVAILABLE / isolation_check_failed`, retryable, is_demo=None, 0.19s** — the agent failed
  CLOSED at `assert_isolated_validation_terminal` **before any MT5 login**. **Root cause (read-only
  confirmed):** the validation terminal `C:\GuvFX\beta\validation` was created by robocopy (as
  administrator) with default ACLs granting only `NT AUTHORITY\SYSTEM` + `BUILTIN\Administrators`; the agent
  runs as **`NT SERVICE\GuvFXBetaAgent`**, which is **absent** from that ACL (whereas `C:\GuvFX\beta\slots`
  *includes* it), so `os.path.isfile(terminal64.exe)` returns False under the service identity →
  `validation_terminal_missing` → `isolation_check_failed`. As administrator the check passes — a **RULE-11
  gap: host certification proved isolation under `administrator`, never under the service identity that
  actually runs the probe.** This is a PLATFORM/ACL failure, NOT a credential result; credentials were never
  tested. **Customer Zero UNCHANGED** (state RUNNING, updated `06:18:55`, blv False, events 13, VR 1, jobs 2,
  0 queued/running); **provisioner DARK**; no MT5 login, no order, no trade, no lifecycle op; validation
  terminal still clean (no accounts.dat — login never happened); temp keyring files removed (return to DARK).
  **VERDICT: BROKER_LOGIN_VALIDATION_FAIL.** Not fixed / not retried (out of scope). Fix belongs to the
  Failure Investigation packet: grant `NT SERVICE\GuvFXBetaAgent` read+execute on the validation terminal
  (the ADR-0016 intrinsic-ACL / B3P-2 slot-grant pattern), then re-run the single validation.

- **2026-08-02 — Broker Login Validation Primitive: HOST CERTIFICATION COMPLETE — fully deployed & production-ready (DARK). 🟢**
  Completes the deploy started in the entry below. **Agent host (`WIN-RD8VDS93DK7`):** deployed the `8fa3748`
  bundle to `C:\GuvFX\beta\agent` (backup `agent.bak-preLoginPrimitive`), **on-host integrity_ok=True**,
  `manifest_version 2026-08-02.1`; installed deps in the agent venv (**cryptography 50.0.0, MetaTrader5
  5.0.6090**); **RULE-9 parse gate 0 failures**. **Isolated validation terminal** `C:\GuvFX\beta\validation\
  terminal` created from the CLEAN golden (no `accounts.dat`); **ISOLATION_OK** via the deployed ADR-0027
  code with negative controls (rejects golden + slot 2 → `validation_terminal_not_isolated`). **Envelope
  keys** (`key_id beta-cred-v1`): X25519 pair generated **on-host**, private key written to the host secret
  store (machine env via `winreg`) — **never printed / never left the host**; public key installed on the
  backend (`beta.env`, `BROKER_CRED_ENC_PUBKEYS`+`KEY_ID`). Agent restarted (WinSW service mechanism), clean
  start, `:8791` pid 6656; **AGENT_CAN_DECRYPT=True** (synthetic non-credential round-trip); **BACKEND_CANNOT_
  DECRYPT=True** (`backend_has_private_keys=False`, seals but cannot open). **NEGOTIATE ok** → advertises
  `VALIDATE_LOGIN`; **replay → `nonce_replayed`**; HMAC channel unchanged; old-image provisioner ↔ new agent
  backward-compatible. **Customer Zero pid 316 unchanged** (Session 0); prod IS6 terminals 4336/8748 healthy;
  **provisioner DARK** (`BETA_RUNTIMES_ENABLED=0`); `PROVISIONING_REQUIRE_BROKER_LOGIN` unset; prod API 200.
  **No broker login performed, VALIDATE_LOGIN never invoked, no order, no strategy, CZ untouched.** Backup
  `pre-hostcert-20260802T154902Z.sql.gz` (sha256 `d164b0a6…`). Governance reconciled: workstream branches
  deleted, PRs #254/#255/#257 merged + #256 closed, ADR-0026/0027 on main, CI green. **Least-privilege
  note:** the envelope private key + the HMAC keyring are machine-scoped env vars — recommend future move to
  a service-scoped secret store. Next = First Live Broker Login Validation (separately gated).

- **2026-08-02 — Broker Login Validation Primitive: BACKEND deployed to production DARK; host certification STOPPED at the envelope-keypair + clean-MT5 boundary. 🟠 (SUPERSEDED by the entry above — host certification now complete.)**
  Controlled deploy of the ADR-0027 primitive (source `8fa3748`). **Backend half DONE + verified, fully
  reversible.** Verified backup `pre-loginprimitive-deploy-20260802T153047Z.sql.gz` (sha256 `8ae1b131…`,
  gzip-OK); rollback tag `guvfx-prod-guvfx-backend:rollback-preLoginPrimitive` → prior `160e3bc6`.
  `BUILD_TREE_PARITY_PASS_8FA3748` (649 `*.py` byte-identical, 0 deltas); new image **`cf8e3b1801fd`**
  (throwaway-verified: `VALIDATE_LOGIN` in `SUPPORTED_OPERATIONS`, seal-only guard present, no
  `unseal_as_sender`, no `BROKER_CRED_ENC_*` baked). Recreated **only** `guvfx-backend` (`--no-deps`,
  migration-free); post-verify: running `cf8e3b18`, `VALIDATE_LOGIN` advertised, **backend cannot decrypt**
  (`backend_has_private_keys=False`, `backend_enc_configured=False`, no envelope key in env), `migrate
  --check` 0, system-check clean, public API 200. **Provisioner UNTOUCHED** (`160e3bc6`, DARK
  `BETA_RUNTIMES_ENABLED=0`) — it is a standalone (non-compose) container whose config lives only
  in-container, so its recreate needs its secret-config source (deferred). **Customer Zero pid 316 ALIVE
  unchanged** (Session 0); prod IS6 terminals 4336/8748 healthy; `GuvFXBetaAgent` :8791 (pid 13228)
  Running; all containers healthy. **STOPPED before** the Windows-host + agent-key steps: (1) the envelope
  **private key** (#13) is a customer-credential decryption secret — Sponsor-inserted, never handled by me;
  (2) the isolated **validation terminal** (#14) needs a CLEAN MT5 source (RULE-10, never a used/production
  terminal) that I do not hold; (3) the agent-bundle + provisioner restart is a live management-plane
  mutation awaiting explicit go. `PRODUCTION_UNAFFECTED`. Security finding (pre-existing, redacted): prod
  `docker-compose.yml` inlines secrets in `environment:` — recommend migration to a secret store + rotation.

- **2026-08-02 — Repository hygiene & governance closure. 🟢**
  Read-only inspection + governance reconciliation, no implementation. **main clean** (@ `c2da273`,
  0 ahead/0 behind origin); no stale background jobs (no `manage.py test` / `make check` / `gh` watchers /
  orphaned loops running); all recent CI `completed/success`. The reported **"main +77 -0"** was the two
  untracked backlog docs (42+35 = 77 added lines, 0 tracked-file changes) — recorded in a prior packet,
  now committed here. **PR reconciliation:** **#255 MERGED** (ADR-0026 broker-connectivity capability —
  the accepted governance baseline for the capability; clean, additive, adds only
  `docs/ADRs/0026-…md`); **#256 CLOSED** (ADR-0027 design — superseded by merged #257, which carries
  ADR-0027 with a §9 As-built on main); **#219 kept OPEN** (Hosted-MVP Phases 0-1 baseline — stale +
  CONFLICTING, has unique docs but would regress live handoff docs → flagged for a PM rebase-or-close
  decision); **#58 kept OPEN** (Blueprint-06 PROPOSED live↔packet reconciliation — a genuine pending
  governance proposal for the deferred E3 live-execution gate). **REPOSITORY_HEALTH_PASS.** No production /
  Customer Zero / provisioner / broker / Windows-agent / runtime / slot / strategy change.

- **2026-08-02 — Broker-login validation primitive MERGED to main (ADR-0027 Phase 1, engineering-only). 🟢**
  PR **#257** merged (`main` `bffba3b`→`3f58e56`, merge `8fa3748`). The single non-destructive,
  runtime-independent broker-login validation mechanism: backend **envelope-encrypts** the customer's MT5
  password to the agent's public key (backend can encrypt, **cannot** decrypt — ephemeral-static ECIES,
  X25519+HKDF-SHA256+AES-256-GCM, AAD binds op/runtime/correlation/nonce, keys a distinct scope from the
  HMAC keyring); the agent opens it at point of use and probes login against a **dedicated ISOLATED
  validation terminal** (contained-under-dedicated-root AND disjoint from every slot/golden/accounts/
  beta_root; rejects `..` traversal + bare-drive root; single-flight lock; **always shutdown()**; NO
  order/symbol API). New signed protocol op `VALIDATE_LOGIN` (credential bound to the signature via a signed
  `payload_digest`; lifecycle ops sign a byte-identical body — backward compatible); `mgmt_protocol.py` +
  `mgmt_agent_core.py` mirrored byte-identical; `manifest.json` regenerated (17 modules). Single backend
  entry `BrokerLoginValidator.validate(account)` → secret-safe `ValidationOutcome`
  (HEALTHY/NEEDS_ATTENTION/UNAVAILABLE); **backend seal-only invariant code-enforced**. Tests: envelope (9)
  + validator (12) + agent handler/isolation/taxonomy/no-leak/parity/wiring (40); full `make check` green;
  3-agent adversarial review (protocol clean; all crypto/isolation MED-and-lower findings fixed). **Opt-in
  & fail-closed** (absent a configured isolated terminal + envelope key → `validation_unconfigured`).
  **NOT deployed; no live login; no host/key/Customer-Zero change; `PROVISIONING_REQUIRE_BROKER_LOGIN`
  OFF.** Host staging, `mt5.last_error()`→reason calibration (RULE-11), and the `cryptography`+`MetaTrader5`
  host deps (RULE-9 parse-gated install) are deferred to host certification. Next = Broker Login Validation
  Primitive Production Deployment (separately gated).

- **2026-08-02 — PR #254 broker-server resolver DEPLOYED to production (DARK, no broker login). 🟢**
  Controlled deploy of the ADR-0025 fix while Customer Zero stayed broker-independent RUNNING. Backup
  `pre-pr254-deploy-20260802T085724Z.sql.gz` (sha256 `7a304ed8…`); rollback tag `rollback-prePR254-…` →
  prior `4c975abf`. Source synced from a pristine `3fa48bd` worktree; **BUILD_TREE_PARITY_PASS_3FA48BD**
  (636 `*.py`, aggregate `368e1eaf…`, byte-identical, 0 deletions). New image **`160e3bc6190c`**
  (system-check clean, `migrate --check` 0, resolver behaves — free-text→`IS6Technologies-Demo`,
  FK-wins→`Demo-Srv`, both-absent→`broker_server_missing` — PR#252 lease-guard + PR#253 commands intact, no
  keyring/`PROVISIONING_REQUIRE_BROKER_LOGIN` baked). Recreated `guvfx-backend` + `guvfx-beta-provisioner`
  (`--no-deps`, DARK); no `--remove-orphans`, no migrate. **Read-only resolver validation against real
  `TradingAccount pk 12`:** `resolve_broker_server` → `("IS6Technologies-Demo", None)`,
  `_expected_login_server` → `("1302575", "IS6Technologies-Demo")` — password never decrypted, login path
  never invoked. `PR254_BACKEND_RESOLVER_READY` · `PR254_PROVISIONER_DARK_READY` (armed 0, `require_login`
  False both containers) · `CUSTOMER_ZERO_SERVER_RESOLUTION_PASS` · `CUSTOMER_ZERO_RUNNING_UNCHANGED` (RUNNING,
  Job#2 DONE, VR `broker_login_verified=False`, events 13, slot 2 gen 5, **MT5 pid 316 same start 06:18:54 —
  not restarted**) · `PRODUCTION_UNAFFECTED` (terminals 4336/8748, bridge 401, watermarks 430/20647, Nuno
  acct #1 368, only backend+provisioner recreated). Next = Broker Connectivity Execution Design (separate gate).

- **2026-08-02 — Automated broker-server resolution fix: engineering-complete (ADR-0025). 🟠**
  The provisioning login path read only the normalised `broker_server` FK and ignored the customer-entered
  free-text `broker_name` (where the frontend "Broker server name" lands), so a beta account like Customer
  Zero (`broker_name="IS6Technologies-Demo"`, FK null) could not broker-login without manual normalisation.
  New `resolve_broker_server()` with deterministic precedence: **normalised FK wins → else trimmed
  `broker_name` → else fail closed (`broker_server_missing`)**. Wired into `_expected_login_server` +
  `_start_and_verify`. **FK wins unconditionally** (no fail-closed on FK↔broker_name disagreement) because
  `broker_name` is dual-use free text (often a broker *display* name on a normalised account) — so the change
  is strictly additive, no normalised account's resolution changes. Tests: new
  `tests_broker_server_resolution.py` (10 directive scenarios: FK-present, free-text-only, both-absent,
  whitespace, both-equal, both-differ→FK-wins, Customer-Zero shape resolves `IS6Technologies-Demo`, no
  credential logged, no plaintext password, production account unchanged) + updated `tests_broker_login.py`.
  Full `terminal_provisioning`+`trading` (1016) green. **NOT deployed; no production mutation; no broker login
  performed; provisioner DARK; PROVISIONING_REQUIRE_BROKER_LOGIN still OFF.** Next = deploy under governance,
  then the (separately-gated) broker-login execution.

- **2026-08-02 — Customer Zero runtime stability VERIFIED (read-only, ~16.5 min). 🟢**
  Read-only observation gate: 16 polls (~1/min) + t0 baseline. **Every poll identical and healthy** — state
  RUNNING, Job #2 DONE, VR 1 (no new), events 13 (no new), read-only VERIFY `running=true / slot 2 / gen 5 /
  pid 316 / session 0`, provisioner DARK + **0 restarts**, slot 2 gen 5 quarantine/alloc clear, **MT5 pid 316
  unchanged across the whole window** (started 06:18:54 → ~32 min continuous uptime by window end), prod
  terminals 4336/8748, bridge 401, watermarks 430/20647, CZ trades 0, Nuno acct #1 368. `STABILITY_DEMONSTRATED`;
  zero failure conditions. **Conclusively refutes the historical "Session-0 beta MT5 exits after 10–30 s"
  concern.** Next = Customer Zero – Broker Connectivity (separate gate).

- **2026-08-02 — Customer Zero CONTROLLED PROVISIONING SUCCEEDED → broker-independent RUNNING. 🟢**
  The culmination: the original MATERIALISE timeout failure is fully resolved. Sponsor-approved. Fresh backup
  `pre-cz-provision-attempt-20260802T061701Z.sql.gz` (sha256 `ee0efca9…`); Golden STOP-check BEFORE
  (585 files / 396,694,220 B / aggregate `0af1fd48…`). Armed **only** the provisioner-scoped
  `BETA_RUNTIMES_ENABLED` `0→1` (in-place, keyring never read) → recreate provisioner → worker claimed **Job #2**
  → **MATERIALISE (300 s timeout, no false-timeout, attempt 1, ~25 s) → START → VERIFY → RUNNING** →
  **disarmed** back to DARK (armed window ~90 s). Result: `AccountRuntime pk1 = RUNNING`; Job #2 PROVISION/DONE;
  Job #1 preserved; **ProvisioningVerificationReport broker_login_verified=False** (broker-independent).
  Host: slot 2 gen 5 occupied; **MT5 pid 316, Session 0, `C:\GuvFX\beta\slots\2\terminal\terminal64.exe`**
  (task `GuvFXBetaRuntime-2` lastResult=0). `GOLDEN_BYTE_IDENTICAL_PASS` (AFTER `0af1fd48…` == BEFORE).
  `PRODUCTION_UNAFFECTED` (prod terminals 4336/8748 on separate IS6 path; bridge 401; watermarks 430/20647;
  CZ trades 0; **Nuno acct #1 trades 368 unchanged**; API/frontend 200). No broker login/order/trade.
  Next = Customer Zero – Broker Connectivity (separate gate).

- **2026-08-02 — Customer Zero backend recovery APPLIED (REMOVED → NOT_PROVISIONED + inert job). 🟢**
  Sponsor-gated `recover_beta_runtime --apply` (in the DARK provisioner container — its `assert_dark_or_allow`
  guard reads that container's arm flag, and only the provisioner is authoritatively `BETA_RUNTIMES_ENABLED=0`;
  the backend API container inherits the base `=1`). Fresh backup `pre-recovery-apply-20260802T060814Z.sql.gz`
  (sha256 `24c855c7…`). Result: `AccountRuntime pk1` **REMOVED → NOT_PROVISIONED**; one new `RECOVER/recover_reset`
  RuntimeEvent (id 8); **Job #1 preserved** (FAILED/attempt 3); **new Job #2 PROVISION/QUEUED/lease None (inert)**;
  exactly one active PROVISION job; 0 VR. `BACKEND_RECOVERY_APPLY_PASS` · `CUSTOMER_ZERO_NOT_PROVISIONED` ·
  `NEW_PROVISION_JOB_CREATED` · `FAILED_JOB_PRESERVED` · `PROVISIONER_REMAINS_DARK` · `NO_AGENT_OPERATION` ·
  `SLOT2_GEN5_UNCHANGED` · `PRODUCTION_UNAFFECTED`. Job #2 stays inert until a **separate Phase-3 arming**.
  Next = Controlled Provisioning Attempt.

- **2026-08-02 — Customer Zero orphaned-slot cleanup APPLIED (first signed mutating agent op). 🟢**
  Sponsor-gated `reclaim_beta_runtime --expect-slot 2 --expect-generation 4 --apply` (in the DARK provisioner,
  stable `job_id=1`). Fresh backup `pre-cleanup-apply-20260802T054646Z.sql.gz` (sha256 `c0908c8b…`). Drove
  signed **STOP → TOMBSTONE → RELEASE** (~29s, rc=0). **Agent:** slot 2 → `[2, NULL, 5, NULL]` = **Available at
  generation 5**; `slot_generations (2,5,release)`; `slot_audit (10,slot_released,4)`; gen-4 teardown evidence
  complete (`confirm_terminated`/`tombstone`/`verify_cleanup` COMPLETED); tombstone `379ff98c4149a4b5` created
  (gen-4 tree preserved). **Backend (exact reviewed marker):** `AccountRuntime pk1` FAILED→**REMOVED** + one
  `RECLAIM/slot_reclaimed` RuntimeEvent (id 7); **Job #1 unchanged** (FAILED/attempt 3); 0 new/active jobs, 0
  VR. `STOP_ABSENT_PASS` · `TOMBSTONE_GEN4_PASS` · `RELEASE_SLOT2_GEN5_PASS` · `CUSTOMER_ZERO_NOT_RETRIED` ·
  `PROVISIONER_REMAINS_DARK` · `PRODUCTION_UNAFFECTED` (slot 1 gen 7 + slots 3/4 unchanged; terminals
  4336/8748; bridge 401; API/frontend 200). **`CLEANUP_APPLY_PASS`.** Backend recovery (`recover_beta_runtime`)
  NOT run — separate authorised phase. Next = Backend Recovery dry-run.

- **2026-08-01 — PR #253 reclaim/recovery tooling DEPLOYED to production (DARK). 🟢**
  Controlled deployment of the ADR-0024 tooling into the running backend image while Customer Zero stayed
  `FAILED`, slot 2 unchanged, provisioner DARK. Backup `pre-pr253-deploy-20260801T182104Z.sql.gz` (sha256
  `0eb2ebf6…`, 103 tables, gzip OK); rollback tag `rollback-prePR253-20260801T182127Z` → prior image
  `d06b13e81078`. Source synced from a pristine `677da61` worktree; **BUILD_TREE_PARITY_PASS_677DA61** (635
  `*.py`, aggregate sha256 `9123daa8…`, byte-identical, zero deletions). New image **`4c975abf97ad`**
  (system-check clean, `migrate --check` 0 unapplied, both commands registered + dry-run default, imports have
  no side effect, PR#252 lease-guard passes, keyring NOT baked). Recreated `guvfx-backend` (default files) +
  `guvfx-beta-provisioner` (its 2 files, `--no-deps`); no `--remove-orphans`, no migrate. Post-deploy:
  backend restarts 0 / API 200 / keyring-clean; provisioner armed=0 / keyring present / worker DARK, 0 agent
  ops; **CUSTOMER_ZERO_FAILED_UNCHANGED** (Job#1 FAILED attempt 3, 6 events, 0 reports/active); **SLOT2_GEN4
  _UNCHANGED** (stage_copy COMPLETED only, quarantine clear); **PRODUCTION_UNAFFECTED** (terminals 4336/8748,
  bridge 401, watermarks 430/20647, all other containers untouched). **Commands available, none executed.**
  Next = separately-authorised Orphaned Slot Cleanup dry-run.

- **2026-08-01 — CZ orphan-reclaim + failed-runtime recovery tooling: engineering-complete (ADR-0024). 🟠**
  Governed, backend-only tooling to reclaim Customer Zero's orphaned agent slot (slot 2/gen 4) and prepare a
  retry — the RELEASE-driver + reclaim-command gap flagged after the MATERIALISE incident. Branch
  `feat/cz-orphan-reclaim-recovery`: `mgmt_client.release()` (→ agent `op_release`) + read-only
  `probe_occupancy()`; `recovery.py` (guards, STABLE-job_id, `_step`-driven STOP→TOMBSTONE→RELEASE reusing
  PR#252 reconcile, `recover_to_provisionable` exactly-one-job); two SEPARATE operator-gated commands
  `reclaim_beta_runtime` (Phase 1) + `recover_beta_runtime` (Phase 2), **dry-run by default**. Fail-closed
  (BETA-only, DARK-preserved, quarantine-not-REMOVED on failure); no migration; no `deploy/beta-agent` change.
  ADR-0024 + `docs/CZ_RECLAIM_RECOVERY_RUNBOOK.md` (operator runbook + non-executed cleanup/recovery dry-run
  plans + evidence matrix). 12-lens adversarial review run; **all 6 confirmed findings resolved** (`ff33b65`):
  probe now uses a fresh single-use job_id (never a memoised VERIFY); a failed reclaim writes an immutable
  FAILURE RuntimeEvent (never REMOVED); `--force-from-failed` refuses a live/HELD runtime; recover has a DARK
  guard; a lock-free RELEASE resend (`runtime_not_assigned`) is idempotent success; fake/real RELEASE
  generation aligned. `make check` green (889 backend + frontend).
  **NOT deployed, NOT executed; provisioner DARK; slot 2 untouched.** Phase 3 (arm + retry) = separate gate.

- **2026-08-01 — Customer Zero controlled provisioning → FAILED at MATERIALISE; remediation engineering-complete. 🟠**
  First genuine CZ provisioning drove Job #1 / AccountRuntime pk1 to `FAILED` at MATERIALISE though the golden
  copy **completed on the host** (control-plane false-negative). Root cause (verified): a single 20s transport
  timeout on the ~380 MB / ~41s MATERIALISE copy → `op_ambiguous_timeout`, then blind re-POSTs mis-classified
  the agent's `runtime_busy` (lock held during copy) as `materialise_failed`, burning `MAX_ATTEMPTS=3` in ~0.3s.
  **Remediation (branch `fix/cz-materialise-timeout-idempotency`, client-side only, NO migration, NO agent
  change, deployable via backend recreate):** per-op `(connect,read)` timeout map (MATERIALISE 300s, clamp 600);
  in-attempt reconcile (poll-not-repost) that treats `runtime_busy`/timeout as "still running" and quarantines
  only on a bounded budget exhaustion; fail-closed quarantine on a proven-partial; `LEASE_TTL` 300→1500 with an
  honest startup+CI coupling guard; heartbeat TTL default 120→900. Passed an 8-lens adversarial review (no
  blocking; all confirmed multi-user-latent findings resolved). ADR-0023; incident +
  orphaned-slot + recovery/retry plans in `docs/POST_INCIDENT_CZ_MATERIALISE_TIMEOUT.md`. `make check` green
  (858 backend + frontend). **Not deployed; provisioner DARK; slot 2 orphan left untouched (Sponsor-gated
  cleanup).** Deferred: agent `put`-inside-lock hardening (benign window, needs re-stage) + a backend RELEASE
  driver/reclaim command (recovery tooling).

- **2026-07-31 — LiveUpdate containment (Variant A, ADR-0022): SHIPPED + host-validated. 🟢**
  MT5 LiveUpdate relocated `terminal64.exe` into the slot's roaming profile and relaunched outside the slot,
  breaking `is_beneath` VERIFY and the exact-path STOP task (both host-proven). The launch wrapper
  (`slot_launch.ps1`, runs as the slot identity) now denies that identity WRITE on its **own**
  `%APPDATA%\MetaQuotes` update-staging **before** launching, so MT5 cannot relocate and always runs from the
  canonical slot exe. Preserves VERIFY/STOP unchanged; no new privilege; idempotent; SID read-back; fail-closed.
  A reversible host probe proved MT5 fails-closed (continues on the in-slot build) when staging is denied; the
  deployed wrapper (sha256 `d870dcf8`) then made the lifecycle reach ABSENT for a would-relocate build.
  **Merged PR #249 (main `21b4e08`); 30 focused tests + `make check` green; RULE 9 parse-gate passed; staged to
  host; production untouched.** This removes the execution-plane prerequisite that gated Phase B.

- **2026-07-25 — B3P-2 on-demand task model (ADR 0017): the task-enablement blocker is RESOLVED. 🟢 decided + implemented + reviewed, 🟠 host apply + final lifecycle next.**
  **Decision (Nuno):** the eight beta tasks are **ENABLED but TRIGGERLESS** at rest — on-demand execution
  capabilities, not scheduled jobs. No per-invocation enable/disable; no task-modification right for the service.
  **Implemented:** installer registers the tasks enabled (removed the two `Disable-ScheduledTask` calls); VERIFY
  now asserts Enabled + zero triggers; a credential-free `install_pool.ps1 -EnableTasksOnly` migrates an
  already-provisioned pool (Enable-ScheduledTask, no password, refuses non-beta/wrong-principal/triggered tasks,
  read-back). Runtime: `query_task` reads the trigger count, `inspect_task` carries it (unread ⇒ incomplete/fail
  closed), `assert_task_matches_approved` rejects any trigger. **Security hardening beyond the ask (RULE 11):**
  both the VERIFY and enable paths now assert **no non-service principal can Run a task** (full-DACL scan for the
  FILE_EXECUTE bit, with a positive-control self-test). Host-measured on all 8 tasks: the slot identity holds
  read-only `0x120089` (no run bit), beta service `0x1200a9`, no `Authenticated Users` ACE — so the boundary
  ("slot identity gets no run right") already holds and is now self-checked. Adversarial review (5 lenses →
  6 confirmed doc/test regressions fixed, 2 DACL findings refuted by host measurement). ADR 0017 written.
  `make check` green. **Supersedes the 2026-07-25 "second blocker" entry below** — that blocker is resolved.

- **2026-07-25 — B3P-2 TSV: DISCOVERY fix merged + APPLIED + PROVEN on host; native STOP uncovers a SECOND, separate blocker. 🟢 TSV complete, 🟠 lifecycle blocked by task-enablement gap.**
  **TSV discovery = DONE.** `#212` (exact-name `GetTask` + HRESULT classification incl. `excepinfo[5]`;
  `Grant-GuvfxServiceTaskAccess` least-privilege `0x1200a9`) merged; `#213`/`#214` added a credential-free
  `-GrantTaskAccessOnly` install mode (admin-only SD change, no slot password, decoupled from golden validation).
  **Applied on host** (`install_pool.ps1 -GrantTaskAccessOnly`, no passwords): 8 beta tasks now carry the service
  ACE `mask=0x1200a9`; root task folder still holds **no** service ACE (least-privilege, exact-name lookup only).
  **Proven under `NT SERVICE\GuvFXBetaAgent`** (temp service-context task driving the real `win_slot_ops`): beta
  tasks `FOUND`, production `GuvFX_SignalBridge` `DENIED`, nonexistent `ABSENT`; RULE-11 raw controls show the real
  `com_error` arrives **wrapped** (`DISP_E_EXCEPTION 0x80020009`) with the true SCODE only at `excepinfo[5]`
  (`0x80070002`/`0x80070005`) — the shipped `excepinfo[5]` scan is load-bearing, host-confirmed.
  **BUT the native lifecycle still cannot complete.** Signed `STOP` no longer returns `task_absent`; it now returns
  `task_definition_drift`. Root cause (a **second, pre-existing blocker outside TSV scope**): the per-slot launch/stop
  tasks are registered then `Disable-ScheduledTask`d ("install-only", asserted Disabled), `approved_tasks.json`
  records `enabled:true`, and **no agent code path ever enables a task** — so installed `enabled:false` is the sole
  differing identity field and `assert_task_matches_approved` (and `run_task`) reject a disabled task before any
  trigger. This blocks the whole task-trigger path (START-via-task and STOP), and enabling an armed `Stop-Process
  -Force` terminate task is security-sensitive → needs a scoped decision, not an in-passing edit. STOP was denied at
  **precheck before any mutation**: slot 1 unchanged (gen 1, `running:false`, occ `19738ae6d1cfc9c4`); MT5 4336 +
  bridge 13292 + 5 estate tasks untouched. **Recommendation: ACCEPT WITH RECORDED CONSTRAINTS** — TSV done; do not
  declare B3P COMPLETE until the task-enablement gap is decided.

- **2026-07-25 — B3P-2 TSV: Task Scheduler visibility remediation (the final lifecycle blocker). 🟢 code + review complete, 🟠 host proof + merge pending.**
  **What.** Native STOP returned `task_absent` because the least-privilege service `NT SERVICE\GuvFXBetaAgent`
  could not discover its per-slot scheduled tasks. **Root cause (host-measured, service-context authoritative):**
  the root Task Scheduler folder grants Authenticated Users (the service among them) only `FW` write, not
  read/list — so the agent's `GetTasks(0)` enumeration returned nothing; and the individual tasks carry no
  service ACE. `GetTask(exact)` returns `0x80070005` (access-denied), distinguishable from `0x80070002`
  (not-found). **Fix (two parts, both least-privilege):** (1) `win_slot_ops._registered_task` — exact-name
  `GetTask` + HRESULT classification (`0x80070002`→absent, `0x80070005`→`PermissionError`/UNAVAILABLE never
  absent, else→re-raise), scanning every COM surface (winerror/hresult/args[0]/excepinfo[5]) via a shared
  `_com_error_codes`; (2) `install_pool.ps1` `Grant-GuvfxServiceTaskAccess` grants the service exactly
  `0x1200a9` (read+execute) on the 8 beta tasks only, idempotently (RawSecurityDescriptor), scoped-refusal
  guards, per-grant + VERIFY-section read-backs (both `-Apply` and `-VerifyOnly`), plus a root-folder-DACL
  VERIFY asserting the service has NO folder-level ACE. `uninstall` deletes the tasks (ACE goes with them).
  **Phase A** temporarily granted the service read on one task and proved `GetTask`+read succeed with no
  modify/delete, then reverted cleanly. **Adversarial review (4 lenses, each finding independently verified)
  — 5 confirmed, all fixed:** MEDIUM (the test fake set winerror/hresult but a real `com_error` carries the
  HRESULT only in args[0], so the tests never exercised the production path — fixed by modelling all four COM
  surfaces) + 4 LOW (excepinfo scanning, idempotency order test, SID-scope test slice, folder-grant verb-scope
  + runtime folder-DACL VERIFY). **Mutation-tested:** all fail-opens killed (incl. the args[0]/excepinfo drop
  the old fake missed). 720 `terminal_provisioning` tests + full `make check` green (1703 backend, 0 lint).
  Stale contract/research docs corrected to the host-measured HRESULTs. **Not yet done:** merge, re-stage,
  service-context task-discovery proof, then native NEGOTIATE→VERIFY→START→PRESENT→STOP→ABSENT→TOMBSTONE→
  RELEASE→Available. ADR-0016 process-ACL mechanism untouched. Production MT5 4336 + bridge 13292 untouched.

- **2026-07-25 — B3P-2 ADR-0016 Option A: launch-time process-ACL grant + observe revert. 🟢 code + review complete, 🟠 host proof pending.**
  **What.** Completes unprivileged PRESENT attribution (the 4th and final least-privilege blocker: the service
  cannot open a cross-account slot process). A thin, admin-only, hash-pinned launch **wrapper**
  (`deploy/beta-agent/slot_launch.ps1`), run AS `guvfx_b_slot<n>` by the launch task, creates the slot's
  `terminal64.exe` **suspended** (`/portable`), adds ONE DACL ACE granting the service
  `PROCESS_QUERY_LIMITED_INFORMATION | READ_CONTROL` (0x21000, **read-modify-write**, EQUALITY-verified),
  resumes it, and on any failure `TerminateProcess`-es the child by handle and exits non-zero — nothing ever
  runs un-observably. The ACE is intrinsic to the process object: it dies with the process, no revocation
  (ADR-0016 refinement). Observe (`win_slot_ops.py`) now opens at `PQLI | READ_CONTROL` and reads the process
  **object owner** SID via ctypes `advapi32.GetSecurityInfo(SE_KERNEL_OBJECT)`; token-based `_user_sid` removed.
  The F3 launch gate (`win_primitives.py`) moved to the terminate-style arg validation (powershell + fixed
  wrapper via `-File` + this slot's terminal64 + service-SID shape + `/portable` + no inline command).
  `install_pool.ps1` stages the wrapper into `C:\GuvFX\beta\launcher` (inheritance broken, slots RX-only),
  rewires the launch task, and — after review — reads the launcher ACL back and re-hashes the wrapper in the
  VERIFY block (both `-Apply` and `-VerifyOnly`).
  **Design workflow (5 lenses)** locked the Windows-security invariants before implementation.
  **Adversarial review (5 lenses, each finding independently verified) — 8 confirmed, all fixed:** the HIGH
  (VerifyOnly never inspected the launcher ACL/hash) + MEDIUM (0x21000 not pinned as *exactly* that — now a
  single `GRANT_MASK` const with equality read-back) + 6 LOW (exception-safety child leak; `exit 2` dead code;
  unvalidated `$WorkingDirectory`; inline-switch abbreviations; ADR/text accuracy; install-vs-runtime list
  divergence). **Mutation-tested:** every fail-open (owner-match inverted, dropped fail-closed raise, wrong-slot
  terminal, non-powershell exe, lost `/portable`, inline command, dropped `READ_CONTROL`, weakened mask
  equality) is killed by a test; one source-invariant test was strengthened after a mutation survived on a
  docstring match.
  **Verified.** 707 `terminal_provisioning` tests + full `make check` green (1690 backend, frontend build,
  0 lint errors). ADR-0016 finalised to **Accepted**. **Merged** main `23f38d8` (#209). **Re-staged to the
  host** byte-identical (manifest INTEGRITY_OK); **RULE 9** `[Parser]::ParseFile` under Windows PowerShell
  5.1.26100 caught a parse defect CI can't see — `$LauncherDir:` read as a scope qualifier — **fixed +
  re-merged** main `fd716b8` (#210) + a CI lint added; all 5 install/wrapper scripts now parse **0 errors**
  (negative control 4). Host baseline untouched throughout (prod MT5 pid 4336 only terminal64 + bridge pid
  13292 running, beta service Running, all 8 tasks Disabled, launcher dir correctly absent).
  **Only remaining step — GATED on Nuno's credentialed `install_pool.ps1 -Apply`** (re-registers the 4 launch
  tasks with the wrapper action + 4 `TASK_LOGON_PASSWORD`s; creates + ACLs `C:\GuvFX\beta\launcher`; stages
  `slot_launch.ps1`). After that, autonomously: CLM check as `guvfx_b_slot1`, object-owner==slot-SID positive
  control, additive-ACE STOP-still-works, PRESENT proof, then slot-1 VERIFY→STOP→TOMBSTONE→RELEASE→Available.

- **2026-07-24 — B3P-2 RELEASE operation shipped to PR #200 (ADR 0014 Accepted). 🟢 code complete, 🟠 host slot-1 proof pending.**
  **What.** `op_release` — the RELEASE protocol operation that transitions a beta slot Released → Available.
  It advances the durable per-slot generation by exactly one and frees the slot after TOMBSTONE, sourcing its
  two live proofs from a fresh `observe_process → ABSENT` (never a fabricated "stopped"; a live process **or**
  an unreadable host blocks it). This is what lets a runtime launched out-of-band (no `confirm_launch`) — the
  preserved slot 1 — complete its lifecycle. Runs OUTSIDE the per-runtime mutation lock; touches no filesystem.
  Added to `PROVISIONING_OPERATIONS` + `manifest.supported_operations` (NEGOTIATE advertises it; per-op
  integrity manifest gains `op_release`); a drift-guard test pins the two lists equal.
  **Adversarial review (5 lenses, each finding independently verified) — 12 confirmed, 5 behavioural fixes:**
  quarantine + generation-monotonicity gate enforced at the single mutation point (`release_after_tombstone`,
  fail-closed, both release paths); audit written only AFTER the commit as `slot_released` (was a false
  pre-gate `tombstone_completed`) + mandated `before_release` checkpoint; reports the released occupancy's own
  generation; `path_containment_verified=False` for the tombstoned-away dir. **Deferred + recorded:** the
  backend does not yet SEND RELEASE (CVM-Inc-5; no live impact, `BETA_RUNTIMES_ENABLED` off); crash-window
  idempotency is safe/fail-closed; `assert_compatible` makes RELEASE a fail-closed co-deployed capability.
  **Verified.** 639 `terminal_provisioning` tests + full `make check` green (1622 backend, frontend build,
  EVIDENCE-LINT PASS); real `build_agent` E2E (`enforce_integrity=True`, signed protocol):
  NEGOTIATE→MATERIALISE(gen 1)→TOMBSTONE(release_pending)→RELEASE(released, available, **gen 1→2**)→slot freed,
  audit `[slot_released]`, no manual step. **Not yet done:** host re-stage + slot-1 native-lifecycle proof.
  Production MT5 pid 4336 + bridge pid 13292 untouched (no host mutation in this change).

- **2026-07-23 — B3P-2 Phase 2: golden image approved, PLAN run, baseline finding retracted. 🟠 waiting at the APPLY gate.**
  **Golden image.** The previously staged `C:\GuvFX\golden\mt5\5.0.0.5833\` tree was **rejected**: a
  content-level provenance scan found 66 absolute paths rooted at `C:\GuvFX\terminals\account_001\instance\`
  inside `MQL5\experts.dat`, proving it was copied out of a live per-account runtime (RULE 10 violation).
  Every filename-based check had passed it. A dedicated clean install at `C:\GuvFX\golden\newMT5\` was
  commissioned by Nuno, validated read-only, and **promoted**: build `5.0.0.6036`, 584 files, tree digest
  `3a7fa6638e9eb9a0989edcaaff5b0c9ec93b15a6c62b9ee9b5f5f420d6313f10`. Promotion wrote exactly two marker
  files. The validator itself was corrected three times — it had rejected genuine MetaQuotes installer
  output (`MQL5\` is absent in a non-portable install; `bases\` ships populated with 537 files; sample EAs
  and `Profiles` ship). **`install_pool.ps1` PLAN ran clean** (4 identities, 4 rights, 8 disabled tasks,
  approval file) with the **LSA interop self-test passing against the live policy**; post-PLAN state
  unchanged — 0 identities, 0 tasks, no service, production MT5 pid 4336 and bridge pid 13292 untouched.
  **Retraction.** The 2026-07-22 baseline's `SeBatchLogonRight = <ABSENT FROM POLICY>` was a **capture
  defect**, not host state: the export path came from `GetTempFileName()`, which creates the file, and
  `secedit /export` writing into an existing path emits UTF-16 **with no BOM**, so `Get-Content` read it as
  ANSI and *no* line matched. Reproduced both ways on the host. The right holds the three Windows defaults
  and always did (`secedit.sdb` last written 2026-03-19; `scesrv.log` absent ⇒ no `/configure` has ever
  run). **No STOP condition met**; the LSA-over-`secedit` decision is unaffected and better supported.
  PR #181, `make check` RC=0, 1514 backend tests. **Nothing installed, granted or started.**

- **2026-07-21 — GFX-BETA-HEADLESS Increment 4: broker-INDEPENDENT provisioning slice + provider-driven broker-validation abstraction. 🟢**
  Architecture note (supersedes the 07-20 RDS/RemoteApp plan): the beta target is now **non-interactive
  headless co-hosting on the EXISTING box** (per-account portable MT5 runtime in the Administrator autologon
  Session 1, per-runtime process/NTFS/bridge/credential isolation; **no RDS/RemoteApp/customer terminal**),
  Nuno-approved, executed as a **vertical slice** (prove one full customer journey before scaling to five).
  Increments 1–3 (shipped previously) built the durable `terminal_provisioning` machinery: `AccountRuntime`
  state machine + cohort exclusion, atomic 5-global/1-per-user `BetaCapacityLock`, the enqueue-only
  provisioning driver, and the immutable **Provisioning Verification Report** (with a real single-runtime box
  proof, pid 13020 in Session 1, Nuno's terminal 4336 + bridge :8788 untouched). **Increment 4 (this):**
  (a) **decoupled provisioning from broker login** — new `PROVISIONING_REQUIRE_BROKER_LOGIN` flag (**default
  OFF** = broker-independent): a runtime reaches RUNNING on *process* verification alone and its report records
  `broker_login_verified=False`; the strict control-8 identity/login fail-closed checks return only when the
  flag is ON (the later broker-login stage). Report fields now record the runtime's OWN assigned binding, never
  the box's self-report. (b) **broker-validation abstraction** (`trading/brokers/`) — provider-driven registry
  + fail-closed fallback, **MT5 as the first provider** consuming it (format-only, no connectivity); the beta
  reservation path consumes it fail-closed inside the capacity lock. So the **broker-independent journey is
  complete through**: register → login → broker record → ProvisioningJob → runtime alloc → verified RUNNING +
  durable Verification Report — with **no broker connectivity**. **PR #163, `main` 091585f, image 7967c786,
  rollback `rollback-preBetaBrokerIndep` (→16b6b609), no migration.** 996 tests; two adversarial-review rounds
  (all MUST_FIX/SHOULD_FIX resolved). Deployed backend-only with **all gates OFF**; verified beta dormant
  (0 runtimes/reports/jobs), Django check clean, bridge :8788 alive, live execution flowed through the deploy
  (single restart-blip on the ingest worker, recovered). **Onboarding stays CLOSED.** Next: broker-login
  verification stage (needs a **separate disposable demo broker account** from Nuno — not prod/existing demo)
  + the remaining slice wiring (strategy assignment → 0.01 per-assignment sizing → AUTO_DEMO-ready → dashboard).

- **2026-07-20 — GFX-PKT-BETA-ONBOARDING-V1: readiness = NOT READY; Option A approved; Phase 0 security shipped. 🟡**
  Read-only investigation (workflow wf_e3b038d9-1e7, 8 agents) + prod census: the platform is **single-tenant**
  and cannot onboard external beta users — **21 Critical + 14 High** blockers. **⚠️ Onboarding must stay CLOSED
  until the Phase-4 isolation gates pass** (it is safe today only because email-verification never sends a code —
  do NOT unblock it before per-user isolation exists). App-DATA isolation is solid (querysets scope to
  request.user; creds Fernet-encrypted; no DB IDOR); the gap is the MT5-terminal / execution / routing / sizing
  layer. **Architecture decision (Nuno):** Option A = **Windows-native RDS/RemoteApp host pool** (17-point design
  + BoM in `docs/BETA_ONBOARDING_V1_ARCHITECTURE_OPTION_A.md`; no procurement until Nuno approves the cost model).
  **Phase 0 (additive, onboarding stays closed) — first increment SHIPPED (PR #148, `main` f169418, image
  5ec61598, rollback `rollback-preBetaPhase0`, no migration):** fail-close `_get_user_mt5_instance` (C2/C17/C19 —
  a no-lease user can never bind to Nuno's box; verified live) + tenant-scoped alerts/recommendations/trading-health
  and admin-only global ops endpoints incl. the circuit-reset **mutation** (C15/C14 + IDOR). 875 tests; review 0
  MUST_FIX. Nuno's account/strategies/AUTO_DEMO untouched (verified: ti/wayond configs, drawdown $2000, watcher,
  silent_loss=0). Evidence: Notion GFX-EVD-BETA-ONBOARDING-V1. Remaining Phase 0: per-account lot override, beta
  entitlement behind a closed gate, marketplace foundations, Account Status panel, provisioning-state records,
  user-scoped admin, raw-error correction, max-10 accounts.

- **2026-07-16 — GFX-PKT-TI-SIGNAL-EXECUTION-GAP-AND-TP-PROTECTION-FINAL-HARDENING: investigated, no defect; ops rollup added. 🟢**
  **Forensics (evidence): 11 TI signals today — 6 EXECUTED, 5 REJECTED (`daily_drawdown_hit`), 0 silent
  loss.** The "1 of 5" was an early snapshot: plan 27 closed 00:07 UTC realizing −502.80 → tripped the
  OLD $100 drawdown → plans 28–32 (01:03–06:02) durably `PROMOTION_REJECTED`; after the **$2,000** raise
  (confirmed in the **listener** runtime, authoritative for promotion) signals resumed (33–37 executed).
  Replay: $100/$500 reject, $1000/$2000 execute. **TP protection revalidated live:** plan 36 = a 2nd
  TP2_LOCKED broker proof (#494 SL 4038.7→4042.61, leg 3 closed at the TP2 price +156.40, band cleared
  ~8s); breakeven on plan 34; plans 35/37 (losses) INITIAL/no-protection-due (correct). Watcher healthy,
  ti_signals-only. Priority MODIFY>PLACE>CLOSE>SYNC verified. **No blocker to remove.** Added a
  `/operations.signal_execution` per-source rollup (today executed/rejected+reasons/pending, execution%,
  all_accounted). 845+ tests green; no migration.

- **2026-07-16 — GFX-PKT-MT5-BRIDGE-STALL-ROOT-CAUSE-AND-RESILIENCE: root cause found + fixed, review/deploy in progress. 🟡**
  The intermittent "5–6 min MT5 bridge/worker stall" is **NOT** MT5, a hung bridge, or worker death.
  **Root cause (evidence):** the ingest worker made **5 `jobs/next/` calls per loop (~150/min)** vs the
  backend `GuvFXUserRateThrottle` **100/min** → chronic **HTTP 429**; `claim_next_job` raised on 429 →
  the blanket `except: print; sleep(2)` left the **claimed job RUNNING** (reclaimed a lease later as a
  false *"orphaned: worker gone"*) and tight-retried → a self-sustaining storm that intermittently
  blocked *all* claims incl. protection MODIFYs. Proof: **410 `loop_error`=429 in ~1h**, 89 orphaned
  SYNCs/7d, worker RestartCount=0, no OOM, **interleaved successes** (worker alive), bridge reachable.
  **Fix (minimum safe):** `next_job` takes a priority-ordered `job_types` CSV → **one prioritized claim
  per loop (~30/min ≪ 100)**; worker maps 429→`RateLimited`→**exponential backoff**; on a per-job
  processing error, `complete_job(FAILED)` for **idempotent** SYNC/MODIFY/CLOSE (PLACE_ORDER left for
  the reconciler). Deduped `worker_throttle_storm` alert. 845 backend tests green; no migration.

- **2026-07-16 — GFX-PKT-TP-PROTECTION-OPTIMISATION-AND-RELIABILITY-FINALISATION: implemented, review+deploy in progress. 🟡**
  Finalisation of the (already-correct, already-fast) TI protection subsystem — instrumentation, not
  redesign. Reverified prod first: ladder correct, **TP2_LOCKED broker-proven (job #405)**, armed state
  intact, Wayond unchanged, provider disabled.
  - **A — durable latency instrumentation:** `Trade.close_ingested_at` (the worker stamps the
    authoritative UTC ingestion instant on the None→closed transition, idempotent) +
    `execution/protection_latency.py` computing per-plan/leg transition timestamps + segments A–H from
    durable data. Missing datapoint = **UNKNOWN**, never a fabricated zero. Broker→UTC conversion is
    explicit + tested (`BROKER_UTC_OFFSET_HOURS` default 3, flagged **unverified**); the system also
    reports the offset-independent ingestion→verified latency.
  - **E — broker floor quantified** (`protection_floor_stats`): soft-deferral windows by stage+direction
    (empirical: TP2_LOCKED 243 s / BREAKEVEN 42 s) — the **irreducible** floor; `sl_within_stops_level`
    stays a soft retryable deferral.
  - **F/G — `/operations` `tp_protection` block** (per-leg latency + segments, broker floor, SLA status,
    source-aware, honest UNKNOWN); SLA breach → overall WARNING. Existing deduped auto-resolving alerts
    cover the rest (no redundant paging added).
  - **D — benchmark (prod dry-run, no writes):** disabled 8 q/25 ms; **armed idle 12 q/42 ms per 30 s
    tick (~0.4 qps — negligible)**; active 1 s only inside a live window; no bridge calls from the watcher.
  - **H — soak: SOAK-IN-PROGRESS** — durable instrumentation installed; before/after latency accrues on
    natural trades (no trade forced). Migrations `trading` 0010 (index) + 0011 (close_ingested_at).

- **2026-07-16 — GFX-PKT-TP-PROTECTION-LATENCY-AND-FAST-WATCHER: implemented, review+deploy in progress. 🟡**
  Authoritative reconstruction of plan 33 (ti_signals SELL) proved the incremental ladder is **CORRECT**,
  not defective: **job #405 verified the first live TP2_LOCKED** (leg 3 SL 4028.92→4025.30 at 07:26:06),
  and leg 3 then closed at 4025.30 (the TP2 level, +$144.80). The screenshot Nuno saw (SL=entry) was a
  **mid-deferral snapshot**. Latency was the real issue (~10 min close→verified): **~5 min hung-SYNC
  ingestion stall** (the ingest worker recycled mid-call, stranding SYNC #392 RUNNING for its full 300s
  lease while `_ensure_position_sync` refused a new sync → ingestion blind) + ~1 min monitor cadence +
  **~4 min broker stops/freeze band** (irreducible). Fix (Nuno approved): a dedicated **adaptive
  ti_signals TP-protection watcher** (`run_tp_protection_watcher`, idle 30s / pre-TP 3s / active 1s,
  single-flight advisory lock, self-healing reclaim, enqueue-only, minute-chain fallback, Wayond
  untouched) + **protection-sync short lease** (`EXECUTION_SYNC_LEASE_TTL_SECONDS=60`) so a stranded
  sync frees ingestion in ~1 min not ~6. Ops: `/operations` protection_watcher block + deduped alerts;
  soak watcher line. Deploy artefacts under `deploy/tp-protection-watcher/` (managed compose service,
  dark by default). 827 backend tests green.

- **2026-07-16 — GFX-PKT-POST-DEPLOY-STABILISATION-AND-EXECUTION-CORRECTIONS: implemented, review+deploy in progress. 🟡**
  New incidents investigated independently from production evidence (no assumptions):
  - **A (TP2 protection):** plan 24's leg 3 closed at breakeven (4038.01) not TP2 (4034.50). Root cause:
    plan 24 ran at 17:16 UTC 07-15, **before** the incremental ladder (PR #131) merged at 18:27 UTC —
    its MODIFY jobs 179/180 carry `stage=None` (old breakeven-only code). The new ladder had therefore
    **never produced a TP2_LOCKED job in prod**. Fix: proved the ladder with deterministic tests +
    added `_supersede_pending_breakeven` (TP2-always-wins) so a stale PENDING breakeven can't land an
    entry SL after TP2 closes. Bridge refuse-widen backstop confirmed present (guarantees steady state).
  - **B/C (non-executing signals):** plans 28–31 were each `PROMOTION_REJECTED: daily_drawdown_hit`.
    Root cause: `RISK_MAX_DAILY_DRAWDOWN_ABS` unset → **$100 default**, but one 1.20-lot ti_signals
    stop-out ≈ $500 (plan 27 realised −$502.80 today), so the breaker halts after the FIRST losing
    signal each day (the $100 was sized for the old 0.06-lot scale). **Nuno approved re-scaling to
    $2,000**; set durably in `telegram.env` (backend/worker) + `wayond-listener.env` (promotion). The
    breaker is unchanged — only the threshold. 28–31 are NOT replayed (stale).
  - **D (branding):** already correct — acct#1 `public_display_name='IS6FX'`, `public_label()='IS6FX'`,
    and a fresh render in prod shows `account_label='IS6FX'`. The stakeholder saw a pre-fix card. Locked
    with a regression test.
  - **E (notifications):** reconciles clean (WIN 14 = candidates 14 = SENT 14 = transmitted 14,
    exactly-once). Added a persistent per-source reconciliation block + WARNING on mismatch.
  - **F/G:** operations_summary gained notification-reconciliation + risk_state (drawdown) blocks;
    soak gained pipeline-latency (promotion/execution/notification) + protection-by-stage metrics.

- **2026-07-16 — GFX-PKT-POST-INCIDENT-EXECUTION-AND-NOTIFICATION-STABILISATION: DONE + DEPLOYED (PR #136). 🟢**
  Fresh evidence-led investigation of TI non-executions AFTER the listener-parity fix (packet warned
  not to assume that incident explained any later failure). **Findings (both pre-migration, unrelated
  to protection_stage):** plan 19 (msg 29) = a **legitimate** `account_exposure_exceeded` promotion
  rejection, durably recorded (exposure calc verified correct — excludes closed trades, a new signal
  is allowed); plan 22 leg 3 (msg 32) = PLACE_ORDER job #118 stuck `RUNNING` ~8h while **all three
  orders had actually landed** (tickets 224366/367/368) — a bookkeeping orphan that `execution_health`
  neither reclaimed nor alerted (it handled only SYNC/MODIFY, and alerted only PENDING). **Fix:**
  `reconcile_orphaned_place_orders` — a place-order is NOT idempotent (re-run = duplicate), so it is
  **never re-enqueued**; a lease-expired RUNNING place-order is reconciled against the broker (leg
  Trade exists → mark SUCCESS with the ticket; else deduped WARN, operator-only), with an independent
  resolve pass keyed on the **broker trade** (a FAILED/missing order keeps the alert open).
  operations_summary gains an `execution_jobs.place_order` block + folds `PROMOTION_REJECTED` reasons
  into per-source rejection_reasons. **Verified in prod:** job #118 auto-reconciled to SUCCESS/ticket
  224368 (`po_reconciled=1`); `silent_loss_total=0`; armed state intact (AUTO/DEMO/kill=False, ti
  0.40×3 cap0 incremental=True, **Wayond unchanged**, BREAKEVEN=1, provider engine disabled);
  notifications exactly-once (14 WIN=14 SENT=14 transmitted, real transport); IS6FX label correct;
  all containers restart=unless-stopped; **no signal replay**. Also fixed the hourly **soak cron**
  (its log dir was root-owned so the ubuntu cron's redirect failed → no snapshots); 24h soak: TI 12
  signals/1 exposure-rejected/10 promoted/W-L-BE 14-15-1/PnL +313.48/14 cards.


- **2026-07-15 — GFX-PKT-TI-SIGNALS-NON-EXECUTION-INCIDENT: root-caused, fixed, DEPLOYED. 🟢**
  Two valid XAUUSD BUY TI signals (approval #45 msg 35 @19:03, #46 msg 36 @20:03 UTC) were acquired,
  parsed, and APPROVED but created **no plan/job/order** — lost silently. **Root cause = deployment/
  runtime parity:** the incremental-TP deploy applied migration 0022 at 18:29 UTC (protection_stage
  NOT NULL; Django drops the DB default after back-fill), but the **guvfx-wayond-listener** — which
  does synchronous auto-routing/planning/leg-creation — was NOT rebuilt, so its pre-0022 model
  omitted the column → leg INSERT NOT-NULL IntegrityError → the plan+legs transaction rolled back →
  the `except IntegrityError` handler mislabelled it **`duplicate_plan`** (it assumed a unique(approval)
  race) → auto_router only logged it. **Fixes (PR #133 `0b9d79e` + #134 `9abf7ca`, deployed):**
  migration **0024** restores DB-level defaults on protection_stage/incremental_protection_enabled
  (schema tolerant of migrate-before-rebuild); `signal_planning` raises a distinct **`plan_integrity_error`**
  (never masquerades as duplicate); `auto_router` records a durable **AUTO_ROUTE_DEFERRED** on every
  rejection; `execution_health` adds a deduped auto-resolving **unplanned-tradeable-signal** alert;
  `operations_summary` gains a **signal_dispositions** block (planned/deferred/in_flight/
  unplanned_no_reason/silent_loss_total). **Deploy:** backend+worker recreated; mig 0024 applied
  (protection_stage default now `'INITIAL'` — proven); **listener rebuilt** (deploy/wayond-listener/
  Dockerfile → telethon + new code) + recreated. Both expired signals **NOT replayed** (watermark past
  msg 36); durable dispositions backfilled for #45/#46 + 3 pre-incident historical losses (#23/#28/#29)
  → **silent_loss_total = 0**. Armed state restored: AUTO=True/DEMO/kill=False, ti 0.40×3 cap0
  incremental=True, **Wayond unchanged**, BREAKEVEN_ENABLED=1; all gates green (symbol accepted,
  concurrent 1/10, daily unlimited) → future signals execute.


- **2026-07-15 — GFX-PKT-INCREMENTAL-TP-PROTECTION-AND-BREAKEVEN-REPAIR: DEPLOYED + ARMED (PR #131). 🟢**
  **Deploy (WS-I):** backend image rebuilt (rollback tag `rollback-preIncrementalTP`), migs 0022/0023
  applied, backend + trade-ingest-worker recreated, Windows bridge swapped + restarted (backup
  `mt5_signal_bridge.preIncrementalTP.py`; new PID; dry-validated bogus-ticket
  `/mt5/modify-position` → `position_not_found`). `incremental_protection_enabled=True` for
  ti_signals, **Wayond unchanged (False)**. Post-deploy armed: AUTO=True, KILL=False,
  BREAKEVEN_ENABLED=1; every-minute `run_monitor_chain` cron intact; steady-state `failures=none`.
  **Incident confirmed from prod (WS-A):** plan #24 (ti_signals) jobs 179 (FAILED `position_not_found`
  — the wasted MODIFY on the already-closed TP2 leg) + 180 (SUCCESS `verified_sl=4038.01` — TP3 SL at
  entry/breakeven, with NO state-2 job) — exactly the missing TP2-lock the ladder now adds.
  **WS-J evidence:** DEPLOYED AND ARMED — natural two-stage broker evidence PENDING (no forced trade).
  Root cause of the XAUUSD SELL "late breakeven" incident: NOT a detection delay — TP1 and TP2 closed
  14s apart inside one 60s monitor cycle, so the old sweep only ever applied state-1 (breakeven→entry);
  the real defects were (1) no state-2, so TP3 was left at breakeven instead of the TP2 price, and (2)
  a fill→ingest lag wasted a MODIFY on an already-closed leg. Repaired by generalising auto-breakeven
  into a **monotonic per-source incremental TP-protection ladder** (`INITIAL → BREAKEVEN →
  TP2_LOCKED`): TP1 profit-close → each open leg's SL → its OWN filled entry; TP2 profit-close → TP3's
  SL → the planned TP2 price; TP1+TP2 in one cycle → TP3 goes DIRECTLY to TP2 (skips breakeven). State
  persisted per leg (`protection_stage`, migs 0022/0023), profit-gated, risk-reducing only, preserves
  a more-protective manual stop (bridge `would_increase_risk`). **Per-source: ON for `ti_signals`,
  OFF for Wayond (unchanged).** Adversarial multi-lens review (7 lenses → verify pass, 0 MUST-FIX)
  drove 7 hardening fixes: broker stops/freeze-band deferral (retryable, no false CRITICAL),
  position_not_found no-op no longer marks an open leg protected, orphaned-MODIFY reclaim in
  execution_health (worker-recycle self-heal), overdue-WARN gating, stage-aware provider baseline,
  close-during-modify downgrade, result self-consistency. Ops: `/operations` protection block +
  monitor-chain counters (`tp2_locked/deferred/noop_closed/overdue/reclaimed_modify`).

- **2026-07-15 — GFX-PKT-PRODUCTION-HARDENING-PHASE-2-AND-FULL-SIGNAL-COPY: 8 workstreams shipped (7 deployed, 1 deploy-dark). 🟢**
  Investigation ran as a 4-agent parallel workflow; the provider-command engine passed 2 rounds of
  6-lens adversarial review (3+1 MUST-FIX all fixed + re-verified). PRs #124–#129 (+#128).
  **H — card branding/privacy (#124, deployed):** WIN-only fail-closed renderer + `_safe_account_label`
  (redact account number) + no-raw-slug subtitle; regression suite locks branding/precision/no-leak.
  **C — hidden-limit audit + durable rejections (#125, deployed):** full 60+ gate table (evidence);
  fixed 4 silent paths — auto-router MANUAL reason now persisted (`AUTO_ROUTE_DEFERRED`, mig 0008);
  NEW always-on `execution_health` monitor step (reclaims orphaned SYNC + alerts on order-opening jobs
  stuck PENDING = the R1 "promoted but never placed" defect); shared-budget attribution. TI cap=0
  (explicit unlimited) confirmed.
  **B4/B2 — notification health + msg-id (#126, deployed):** always-on `notify_health` rollup alert
  (auto-resolving) + undelivered-WIN auto-resolve + Telegram `provider_message_id` capture (mig 0020).
  **D — /operations completion (#127, deployed):** per-source D1 rows (provider vs assignment split,
  caps, promoted/closed/delivered, rejection reasons), D2 infra block (honest UNKNOWN + core flag),
  D3 incident ids, D4 staff actions (admin-only ack + narrow source-bound assignment pause/enable).
  **G — soak instrumentation (#129, deployed):** `soak_report` command + durable `SoakSnapshot`
  (mig 0003) + hourly VPS cron. Baseline captured (TI 24h: 13 signals / 14 fills / 27 closes /
  12W·15L / +94.68 PnL / 12 cards). Meaningful full-soak = ≥24–72h continuous armed operation.
  **E — TI provider trade-management commands (#128, DEPLOYED-DARK, arm=Nuno/Red):** classify TI
  follow-ups (move-SL / close / cancel) → gated, enqueue-only, source-isolated engine (reply-only
  correlation, 4-layer TI↔Wayond isolation, defer-on-unresolved-fill, freshness bound). Recording is
  always-on; acting is OFF (`PROVIDER_COMMANDS_ENABLED` + per-source `command_engine_enabled`, both
  false). Bridge `/mt5/close-position` reused (no Windows change). See [[project_ti_provider_commands]].
  **F — restart/autonomy:** F1 controlled backend restart verified (control unchanged, no replay/dup,
  armed, chain resumed); worker/listener/frontend/bridge restart evidence from this session's deploys;
  all containers `restart=unless-stopped`, cron VPS-side → no Claude/laptop dependency.
  **Breakeven broker proof (A1):** still awaiting a natural TP1 close (0 breakeven fires; instrumentation
  armed). **Both strategies armed throughout** (auto=True DEMO kill=False, breakeven=True, asn#7 wayond +
  asn#8 ti_signals LIVE). Safety: pg_dump `~/backups/pre*-*`, image `:rollback-*` tags, batched deploys.

- **2026-07-15 — GFX-PKT-PRODUCTION-STABILISATION-AND-RELIABILITY: exposure fix + auto-breakeven + notification exactly-once DEPLOYED. 🟢**
  **A — trade-execution reliability (PR #120, `main` 7840ee4; DEPLOYED + verified).** Root-caused why the latest TI
  signal never executed: the exposure gate **double-counted** a PROMOTED-and-filled plan (its legs counted as BOTH open
  positions AND in-flight signal lots → 2.40 vs the 2.40 cap → `account_exposure_exceeded`). `_active_signal_lots` now
  dedups legs already reflected as an open Trade (via the `WAY{plan}L{leg}` comment). The **TI daily cap was proven NOT
  the blocker** (count_today 6/24) but removed per directive: per-SOURCE `SignalSourceConfig.daily_group_cap` (mig 0018),
  `ti_signals=0` (unlimited); Wayond keeps 24. All other gates (duplicate/expiry/concurrency/exposure/broker/margin) intact.
  Verified on prod: the stuck plan#19 exposure eval now returns `None` (margin-guard 10320% ≥ 300%). 
  **B — automatic breakeven (PR #121, `main` e955755; DEPLOYED + ARMED).** When a plan's TP1 leg closes, each remaining
  OPEN leg's SL is moved to its entry (breakeven). Enqueue-only `sweep_breakeven` monitor-chain step (backend holds no
  bridge creds) → worker claims `MODIFY_POSITION` → bridge `POST /mt5/modify-position` (`TRADE_ACTION_SLTP`, re-reads to
  VERIFY, refuses any risk-increasing move). Idempotent (`ProposedOrderLeg.breakeven_applied_at`, mig 0019), retry to
  3 then one deduped CRITICAL alert, fail-safe (only when risk-reducing vs the plan SL). Also enqueues a periodic
  SYNC so closes ingest promptly (no periodic SYNC existed before). Bridge deployed to Windows (`GuvFX_SignalBridge`
  task recycled). Armed via `BREAKEVEN_ENABLED=1`; monitor log `breakeven[enabled=True …]`. Broker evidence auto-captured
  on the first natural TP1 close (`result.verified_sl`). See [[project_auto_breakeven]].
  **C — notification exactly-once (PR #122, `main` 87b5dfc; DEPLOYED + verified).** Investigation confirmed the pipeline
  is already exactly-once in prod (7 WINs → 7 transmitted, 0 gaps). Added the missing reconciler `reconcile_notifications`
  (monitor-chain step): mark-delivered (revives the dead `TradeOutcomeRecord.delivered` flag), backfill a missing
  candidate, revive a dry-run-"SENT"-trapped candidate (real transport only, dup-safe by construction), and a deduped
  WARN for any WIN undelivered > 20 min. Verified: all 7 WINs stamped `delivered=True`.
  **D — IS6FX branding:** verified live (acct#1 `public_label`="IS6FX", source labels "TI Signals"/"Wayond").
  **E — operations dashboard:** backend `GET /api/reliability/operations-summary/` live; `/operations` frontend page
  DEPLOYED to the (diverged) prod frontend (build-gated, HTTP 200; no nav link yet — reachable by URL, staff-only).
  **F — autonomous:** all containers `restart=unless-stopped`, monitor-chain cron VPS-side → zero laptop dependency;
  both strategies armed throughout (auto=True DEMO kill=False, asn#7 wayond + asn#8 ti_signals LIVE).
  **G — safety:** pg_dump `~/backups/preBreakeven-*` , image `:rollback-preBreakeven`/`:rollback-preReconcile`/`:rollback-preOps`,
  bridge `.bak-*`, kill-switch windowed deploys, armed-state capture/restore. Cleared 6 orphaned RUNNING SYNC jobs →
  EXECUTION_PIPELINE now OK. Real live orders (E3) still RED (timezone + Blueprint-06 + Nuno sign-off).

- **2026-07-15 — Stakeholder card branding (IS6FX) DEPLOYED + operational observability (health API + /operations page). 🟢**
  **Workstream A — DONE + deployed + verified (PR #117, main `8d0516c`).** Presentation-only (no trading/routing/auth
  change). `TradingAccount.public_display_name` + `public_show_account_number` (migration 0009); account #1 → **IS6FX**
  (number hidden; falls back to internal name). Reusable `intelligence.display_labels.source_display_label`
  (`ti_signals`→"TI Signals") — card header now "TI Signals Trade Result" (was "TI_SIGNALS TRADE RESULT"). `_price()`
  is now genuinely instrument-aware (was a no-op → raw 5dp): XAUUSD 2dp, JPY 3dp, crypto 2dp, FX 5dp. Restrained polish
  (taller banner, spacing, contrast). Card verified visually (IS6FX + 2dp + human labels); one clearly-labelled DEMO
  preview sent (Telegram msg 61, real candidate, no fabrication, single send). Internal slugs/identity unchanged.
  **Workstream B — health VISIBILITY delivered (PR #118, main `1273075`).** Read-only, extends the existing
  `reliability` app (no parallel subsystem). New `operations_summary.build_operations_summary()` + `GET
  /api/reliability/operations-summary/` (staff-only) aggregates: control (auto/mode/kill), component health + heartbeat
  freshness, **source-aware** strategy metrics (signals/accepted/rejected/wins/losses/breakevens/realised-PnL/cards per
  source, never combined), open positions/plans/candidates, dispatch, best-effort broker metrics (balance/equity/free-
  margin/margin-level via a fail-safe order_check — **no order placed**), and open alerts. Verified on prod: ti_signals
  per_leg=0.40 / wayond 0.02, account "IS6FX", broker reachable, all heartbeats HEALTHY, no secrets. Frontend
  `/operations` page built + CI-validated (deploy gated by the prod-frontend divergence — see [[project_frontend_divergence]]).
  **Partial/follow-up:** B3 alerting reuses the existing `reliability_tick` reconcile+Telegram+dedup+recovery (verified
  producing 2 open alerts: a WARN for the 4 stuck SYNC jobs + a stale 07-07 circuit-breaker CRITICAL) — additional
  conditions (listener/monitor/margin-guard) + the ops-route `RELIABILITY_TELEGRAM_*` config are a scoped next step; B7
  reporting deferred. +20 tests (663→669 backend). Both strategies remain continuously armed; no execution change. E3 live still RED.
- **2026-07-15 — TI missing-WIN-cards REPAIRED + RECOVERED, and ti_signals now sizes 0.40/leg (1.20/signal) LIVE. 🟢**
  **Workstream A (missing cards).** Genuine TI XAUUSD winners closed in MT5 but produced no cards in the WIMs
  Stakeholder Review channel. Root cause: (1) the live ingest worker ran a **stale host-mounted copy**
  (`/srv/guvfx/worker_scripts/…`, never updated by PR #112) → `close_price=None` → skipped; fixed by pointing the
  worker command at the durable image copy `/app/mt5_trade_ingest_worker.py`. (2) `resolve_leg_evidence` picked a
  stale price-less deal row over the authoritative position row → **"+$0.00"** cards (PR #114, +2 tests). **Recovered**
  the 3 real winners (plan 15: 224287 +$4.10 / 224288 +$8.36 / 224289 +$13.74) → cards **msg 57/58/59**, progressive,
  correct name, zero duplicates.
  **Workstream B (0.40/leg, TI-only; ADR-0012, PR #115).** Every lot/exposure cap was global source-blind; now
  source-scoped at every gate via `SignalSourceConfig.max_lot_per_leg`/`max_total_lot` (migration 0017, ti → 0.40/1.20;
  wayond stays 0.02/0.06). Order payload carries `signal_source`+`max_lot`; worker + all 4 bridge sites admit up to the
  payload cap, fail-closed to 0.02, bounded by an **independent per-source ceiling** (env `TI_SOURCE_MAX_LOT`). Exposure
  0.50 → **2.40**. **NEW free-margin guard** (projected margin level via bridge `order_check`, floor 300%, **fail-CLOSED**,
  logs) — the listener was given bridge access (`100.79.101.19:8788`) so it verifies (else it would block all TI orders).
  Adversarial review NO-GO → 4 must-fixes fixed → re-verify GO, 0 regressions. **Deployed** in a kill-switch window;
  **preflight** order_check XAUUSD 0.40 = retcode 0 (no order_send), projected 1.20-signal margin_level ~10,336% (floor
  300%). Deployed split: TI [0.40,0.40,0.40], wayond [0.01,0.01,0.01]. Both strategies armed; monitor heartbeat fresh;
  **prod == git** (main `04bb4e1`). Full backend suite green (649). Notion: GFX-EVD-TI-WINNER-RECOVERY-AND-0.40-SIZING.
  Rollback: `:rollback-preTiSizing` images + `~/backups/preTiSizing-*.sql.gz` + bridge `.bak-preTiSizing`; ti caps
  revertible in the DB; exposure/guard env-overridable. E3 live (real-money) still RED.
- **2026-07-15 — CI reconciled truthful-green + DAILY GROUP CAP now PER-SOURCE (24/day). 🟢**
  **CI finding (evidence-first):** the packet's premise of "58 red backend CI failures" did **not** reproduce — CI
  runs the full suite (`python manage.py test`) and it is **green** on PR #112's HEAD (`Ran 626 tests … OK`,
  run 29399551558). The 58 failures I had measured were an **environment artifact** of running the suite inside the
  **prod backend container**: prod sets `SECURE_SSL_REDIRECT=True` (301s every Django test-client request → all
  view/API tests fail assertions — the exact reason `ci.yml` sets `DJANGO_SSL_REDIRECT=False`) and carries live
  bridge/agent env vars (symbol/order tests reach real services → ERROR). **Classification: 0 genuinely broken,
  0 obsolete** — every one is env-induced and passes clean in CI. **No CI restructuring performed** (adding
  integration markers / moving tests would be an unrequested refactor of an already-green suite). **PR #112 merged**
  → main `d570d40`; prod==git verified (5 runtime files + running container sha256-match PR HEAD; Windows bridge
  functionally identical — only comment-encoding cosmetic drift).
  **Daily-cap change (this PR):** `PLAN_MAX_GROUPS_PER_DAY` was `10` and account+symbol-wide **and** counted only
  currently-`PLANNED` plans (which promote/close within seconds → the cap was effectively a no-op and both providers
  shared one budget). Now **env-tunable default 24, per-account+symbol+SOURCE**, counting **acted-on** groups
  (PLANNED/PROMOTED/CLOSED; VOIDED/HELD/SUPERSEDED excluded) across the calendar day. Each provider (`wayond`,
  `ti_signals`) gets an independent 24/day budget — one source can never consume another's. **Unchanged:**
  concurrency cap 10 groups (per account+symbol, across sources), 20 open positions, 20 positions/symbol, 0.50 lot
  exposure, lot sizing, mandatory SL/TP, expiry/duplicate/broker-symbol gates. **+7 tests** (per-source isolation,
  acceptance up to cap, fail-closed past cap counting acted-on groups, concurrency enforced independently, other
  risk caps unchanged, calendar-rollover reset, both sources live + no open position altered). No migration (pure
  constant + classmethod). Branch `fix/daily-cap-per-source`. E3 live (real-money) still RED.
- **2026-07-15 — CLOSE-PRICE → OUTCOME → TELEGRAM CARD pipeline REPAIRED + overlap fixes productionised (PR #112). 🟢**
  The ingest worker built one Trade per raw MT5 *deal*, reading `open_price`/`close_price` (fields a deal lacks) →
  every Trade had `open_price=0, close_price=None` → `TradeResultProducer` skipped them → zero outcomes / candidates
  / Telegram cards. **Fixed:** `mt5_trade_ingest_worker.build_positions_from_deals()` groups deals by `position_id`
  (open = entry `DEAL_ENTRY_IN` deal, close = exit `OUT`/`OUT_BY` deals — authoritative, never inferred; fail-closed
  on partials), one Trade per position, idempotent; the bridge deals snapshot now returns the deal `entry` type.
  Also landed the overlap fixes (`resolve_completed_plans` PROMOTED→`CLOSED` frees the concurrency slot; caps raised
  to 20 open / 0.50 lot / 10 groups) and repaired the **monitor-chain cron** (had never run — log `Permission
  denied`; now provisioned + reliability heartbeat + logrotate). **Deployed + PROVEN:** WAY8 3-leg SELL re-synced to
  real open/close/profit → 3 LOSS outcomes (internal-only); a controlled WIN (rolled back, no order/send) flowed
  trade → WIN outcome (`+$16.10`) → NotificationCandidate → **rendered card** "🏆 Wayond WIM Strategy +$16.10 WIN".
  10 new ingestion tests + 6 re-pinned risk/concurrency tests pass; 7-lens adversarial review = 0 defects; the 58
  pre-existing env-dependent suite failures are unchanged (delta 0). Both strategies stay continuously armed
  AUTO_DEMO. Branch `fix/continuous-plan-resolution-and-trade-outcomes`; rollback `:rollback-preCloseFix` images +
  pg_dump `~/backups/postCloseFix-*.sql.gz`. **PR #112 open for Nuno to merge** (PM owns lifecycle). E3 live still RED.
- **2026-07-14 — WAYOND-WIM-STRATEGY: MERGED + DEPLOYED + CONTINUOUS AUTO_DEMO ARMED (Nuno-authorised). 🟢**
  PRs #109 (feature) + #110 (source-aware card name) merged → main `d5ae9b9`; deployed backend `78c9fe8ab61f`
  + listener `f417fd68` + frontend rebuilt (mp-010 card live). Migration strategies/0012 applied in a kill-switch
  window (released after verify). **"Wayond WIM Strategy"** now runs continuously in DEMO alongside Wayond Auto
  Demo (both live). TI Signals chat `-1004480146594` (listener account NunoRAmaral already a member; parser 6/6);
  Wayond asn#7 bound `wayond`, WIM Strategy#8/asn#8 bound `ti_signals` on demo acct#1 (SignalSourceConfig 0.06,
  cert MEDIUM); watermark=14 → catch-up processed=0 (no replay); 有効期限 expiry enforced fail-closed; source
  routing verified independent (`effective_mode → AUTO_DEMO armed`: wayond→#7, ti→#8); order_check dry-run retcode
  0 / no order_send; 3 legs shared-SL/unique-TP proven. Global auto=True mode=DEMO kill=False; VPS-side, no
  Claude/laptop dependency. Rollback: `:rollback-preWIM` images + `~/backups/preWIM-*.sql.gz`; TI-only disarm =
  pause asn#8. Notion evidence: GFX-EVD-WAYOND-WIM-STRATEGY-DEPLOY-ARM. E3 live (real-money) still RED.
  _(Prior Phase-A code-only entry superseded by this deployment.)_
- **2026-07-14 — WAYOND-WIM-STRATEGY (Phase A): a second Telegram signal-copy strategy — CODE ONLY, repo branch, NOT deployed/armed. 🧩**
  New "Wayond WIM Strategy" sourced from the **TI Signals** Telegram channel (provider slug `ti_signals`),
  mirroring the Wayond auto-demo, with a marketplace card + a manual enable/disable. Additive backend +
  frontend on branch `feat/wayond-wim-strategy`. **(1)** New `ti_signals_v1` parser
  (`intelligence/ti_signals_source.py`, registered in `signal_intake/parsers.py`) for the
  `🔔 XAUUSD BUY (M15) / Entry: a-b (mid m) / SL: / TP1..3` format — line-anchored, real-timeframe-gated,
  quarantines ambiguous entries, update-markers-first. **(2)** Source-scoped auto-routing
  (**ADR-0011**, Amber): new `StrategyAssignment.signal_source` (migration `0012`, additive) + a fail-closed,
  back-compatible `_resolve_target(mode, source)` so Wayond→Wayond and TI→WIM route independently; the live
  single-Wayond path is unchanged while `signal_source` is unset. Claim-detection is unscoped (disabling by
  is_active/stage/mode/account never re-routes), and routing now also requires `account__is_active`.
  **(3)** Marketplace card `mp-010` (frontend `MARKETPLACE_SEED` + backend `MARKETPLACE_STRATEGIES`) with a
  dedicated `signal-copy/status` + `signal-copy/toggle` that PAUSE/RESUME an already-armed AUTO_DEMO
  assignment (never arm; disable is a reliable kill; owner+account authz; generic Assign rejects signal-copy
  templates). **Adversarially reviewed** (20-agent sweep → 11 confirmed defects, all fixed + regression
  tested; focused re-verify → 2 residual unbound-fallback holes, closed by a multi-source fail-closed
  guard + a Phase-B "bind Wayond first" requirement). Tests: `501` green (execution+strategies+signal_intake);
  frontend lint 0 errors + build OK. **Phase B (RED, Nuno's gate, NOT done):** bind Wayond assignment to
  signal_source="wayond" FIRST; TI channel numeric chat-id + listener join;
  create prod provider/source-config/AUTO_DEMO-assignment rows; deploy the router change in a kill-switch
  window; arm + verify. E3 real-order posture unchanged (still RED).
- **2026-07-06 — E3-DEPLOY-AND-PREFLIGHT: production brought up to current main (`e69c144`) WITHOUT enabling E3. 🚀**
  Deployed the full undeployed stack (prod backend was 36 commits behind at `cb2108c`/#56 → now `e69c144`/#93:
  auto-shadow, close-monitor, outcome-router, notification dispatcher, dry-run transport, E3 demo-promotion,
  AUTO_DEMO router, monitor-chain). **Kill-switch window** protected live strategy trading: tagged rollback
  image `:rollback-preE3`, took a verified pre-migration `pg_dump` (1.28 MB), engaged kill switch + paused
  scheduler crons + waited for in-flight to settle, applied **9 additive migrations** (trading 0008; execution
  0009–0014; signal_intake 0007; strategies 0011 — `migrate --plan` confirmed additive-only, no destructive
  op), rebuilt + force-recreated `guvfx-backend` + `guvfx-mt5-trade-ingest-worker` + `guvfx-mt5-shadow-worker`
  on the new shared image (`8a55b0cacf45`), restored crons, released kill switch. Installed the monitor-chain
  cron. **All 14 VERIFY points PASS; every default stayed safe:** provider `wayond` ONBOARDING/un-armed,
  `auto_execution_enabled=False`, `signal_execution_mode=SHADOW`, 0 active AUTO_DEMO assignments, dispatch
  OFF, 0 Wayond ExecutionJobs, 0 `order_send`; existing shadow dry-run still PASS (job #68, no order); listener
  (separate image) + frontend + validate-worker untouched; API 200. **Nothing armed/enabled — E3 remains RED.**
  Rollback = retag `:rollback-preE3`→`:latest` + recreate (+ restore `pg_dump` if a migration must be undone).
  Evidence `GFX-EVD-E3-DEPLOY-AND-PREFLIGHT.json` (PASS 14/14).
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

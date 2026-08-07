# NEXT — Priorities (keep this list short)

## ▶ Hosted Persistent MT5 Workspace — Increment 3 shipped (2026-08-07, DARK); awaiting Sponsor
ADR-0033 accepted-with-conditions. PR #301 (foundation) + #302 (readiness abstraction + hardened opening
gate) MERGED. Increment 3 (branch `feat/adr0033-inc3-pilot-plumbing`) = **complete trade-operation
identity safety**: CLOSE + MODIFY now carry the same identity invariant as PLACE (pre-send
`verify_mutation_identity` before every `order_send`; all 4 mutation sites gated). **Bounded next action:**
the remaining pilot-plumbing (durable routing wiring + server-side producer pin-derivation, observer
pause/resume, host attach probe, read-only API, staff observability) is a SEPARATE follow-up increment —
the repository is NOT yet full-pilot-ready. Then Sponsor approval to open the disposable-host pilot (16
checks) + RULE-11 NTFS-ACL cert + the RDS/licensing decision. No host mutation, no execution enablement.

## ▶ (superseded) Hosted Persistent MT5 Workspace — Increment 2 shipped (2026-08-07, DARK); awaiting Sponsor
ADR-0033 accepted-with-conditions. Increment 1 (PR #301, foundation) MERGED. Increment 2 (branch
`feat/adr0033-inc2-readiness-abstraction`) done DARK: two-provider readiness abstraction + hardened
order-time gate (mandatory identity pin + TOCTOU narrowing), Provider A regression-identical, migration
`trading 0015`. **Bounded next action:** obtain Sponsor approval to open the disposable-host pilot (16
checks) — the single gate that unblocks everything downstream: RULE-11 per-user NTFS-ACL certification,
EXP-1 manual-login attach → IPC, wrong-account order-time rejection, reboot auto-reconnect — plus the
commercial RDS/licensing decision. Repository follow-ups (additive, non-safety, deferred): observer
pause/resume wiring, read-only workspace API, observability projection, routing implementation + producer
pin-plumbing (Tension 2), the host attach probe. No host mutation, no execution enablement until then.

## ▶ Supervised installer — engineering-complete (2026-08-06); NOT deployed; awaiting Sponsor gate
Branch `feat/supervised-installer` (base `main` `be7f215`). Resolves the 2026-08-06 host-deploy blocker:
`install_service.ps1` now takes a mandatory `-InstallProfile Dark|Supervised` and is the **single sanctioned
install mechanism** — it does the post-install `sc config obj=` virtual-account assignment + `SeServiceLogonRight`
grant, verifies `SERVICE_START_NAME == NT SERVICE\GuvFXBetaAgent` (rejecting LocalSystem), uninstalls-first on
re-install (WinSW v2.12 has no in-place update), and **auto-rolls-back (verified)** on any failure. A 7-lens
adversarial review folded in 6 fixes (verified uninstall/removal, refuse-when-baseline-XML-unknown, `$RunAsUser`
pin, exit-code checks). `make check` green; 26 installer tests + contract JSON + ADR-0013 addendum. **Bounded
next action:** obtain Sponsor authorisation to re-attempt the Windows-host-only supervised deploy using the
installer (PLAN then `-Apply`), per `docs/operations/validation-agent/deployment-min-hardening.md §4`. No
host mutation until then; `:8788`/#12/#1 untouched.


## ▶ Validation Agent MINIMUM Production Hardening — engineering-complete (2026-08-06); NOT deployed; awaiting Sponsor gate
Branch `feat/validation-agent-min-hardening` (base `main` `f5d8389`). Implements RR-1/2/3/4/11 as repository
engineering: supervised WinSW target profile, signed-NEGOTIATE readiness probe, durable lifecycle logging,
single-instance + launch enforcement (`agent_supervised`), monitoring + named alert delivery, read-only Ops
surface. A 6-lens adversarial review folded in an **exclusive OS bind** (`allow_reuse_address=False` +
`SO_EXCLUSIVEADDRUSE` — SO_REUSEADDR let a 2nd process hijack `:8791` on Windows), an **advisory lock** (never
vetoes a start), **crash detection** (`AGENT_CRASHED` + non-zero exit so WinSW restarts), and a real
`agent_crash_loop` alert. `make check` green; no DB migration. **Bounded next action:** obtain Sponsor
authorisation for the separately gated Windows-host + backend deployment (`docs/operations/validation-agent/
deployment-min-hardening.md`) — install the supervised profile + provision the readiness probe/alert owner.
Do NOT apply any host/service/firewall change before that authorisation; `:8788`, Customer Zero #12 and live
account #1 stay untouched.


## ▶ Validation-UX packet — engineering-complete (2026-08-05); PR #288 OPEN; NOT deployed
Branch `fix/validation-ux-timeout` (base `main`) turns broker validation into a modal interaction with a
contextual next action, customer-safe errors on every path, a duplicate-click guard, graceful reconnect after
a dropped connection, and the backend gunicorn timeout raised (120→190s) above the 175s VALIDATE_LOGIN budget.
Full timeout chain audited in `docs/VALIDATION_TIMEOUT_CHAIN.md` (reverse proxy needs no change). `make check`
green (backend 2638 OK / frontend 123 / lint 0-err / build). **Bounded next action:** obtain Sponsor
authorisation to deploy — a **backend image rebuild** (for gunicorn 190) **+ frontend rebuild** — after which
Nuno re-runs the browser Test connection on disposable account #13. Do NOT deploy before that authorisation;
Customer Zero #12 and live account #1 stay untouched.

## ▶ Customer Journey Consolidation & Telegram Readiness — engineering-complete (2026-08-05); DARK; awaiting Sponsor gate
Branch `feat/ipr-journey-consolidation` (base `dcea807`) makes `/accounts` the single canonical broker-account
page, adds the read-only signal-copy readiness endpoint + `SignalCopyReadiness` panel (replacing "Not armed"),
maps every arm refusal to customer-safe copy, and removes operator/backend terminology from customer copy —
all behind default-OFF flags. `make check` green; adversarial review no-HIGH. **Bounded next action:** obtain
the Sponsor decision to **merge this DARK** (on CI green) — nothing else. Browser acceptance with a disposable
user is a SEPARATE, Sponsor-authorised packet and additionally depends on the still-outstanding environment
gates (WP6B multi-tenant execution, `BETA_RUNTIMES_ENABLED` on a certified host + broker-login ACL,
`BETA_SELF_SERVE_ARM_ENABLED` + `INTERNAL_PILOT_ARM_APPROVED_EMAILS`). Do NOT arm any flag or deploy here.

## ▶ Broker Connectivity Trusted Beta — WP5.4 ops package + WP6 certification PLAN COMPLETE (2026-08-04); DARK
The full engineering plane (WP1A→WP5.3) is merged DARK; the WP5.4 operations-readiness package and the **WP6
multi-tenant certification PLAN** are authored: `docs/operations/broker-connectivity/` (WP5.4 arming/rollback/
incident/support/monitoring + WP6 `wp6-*` certification matrix/evidence/release-gate), validation tests
`tests_wp54_readiness.py` + `tests_wp6_certification.py`.

**WP6A (shared-environment non-destructive certification) DONE (2026-08-04):** 433 tests executed all-pass;
verdict **GO WITH CONDITIONS** for a tightly-controlled Internal Pilot (docs/operations/broker-connectivity/
wp6a-*). **Bounded next action:** none in the repository — **WP6B (isolation/concurrency/load/failure/capacity)
remains OUTSTANDING and needs the disposable environment** (a Nuno-provided disposable demo account + the Windows host; task #108) and is
Sponsor-gated. Do **not** arm any flag, deploy, or invite beta users. When Nuno authorises WP6 execution, run
the `wp6-test-matrix.json` cases in the disposable env, capture evidence per `wp6-evidence.json`, complete
`wp6-release-gate.json`, and only then does the Sponsor decide GO / GO-WITH-CONDITIONS / NO-GO. Execution-gate
arming (arming stage 6) + invitation (stage 7) require **WP6 PASS + explicit Sponsor approval**.

## ▶ Customer Zero MATERIALISE remediation — engineering-complete (2026-08-01); awaiting merge + Sponsor gate
Remediation of the CZ MATERIALISE timeout/idempotency/retry defect is engineering-complete on branch
`fix/cz-materialise-timeout-idempotency` (client-side only, NO migration, NO agent change; `make check` green;
ADR-0023; `docs/POST_INCIDENT_CZ_MATERIALISE_TIMEOUT.md`). Provisioner is DARK; slot-2 orphan left untouched.

**Bounded next action:** the remediation is deployed (PR #252, image `d06b13e`), and the governed cleanup +
recovery tooling is engineering-complete (ADR-0024, branch `feat/cz-orphan-reclaim-recovery`: `reclaim_beta_runtime`
+ `recover_beta_runtime`, dry-run by default). Merge the tooling through normal governance, then request the
single Sponsor decision **"Customer Zero – Orphaned Slot Cleanup Dry-Run"** (`reclaim_beta_runtime … --probe-agent`
read-only, no `--apply`). Do **not** apply cleanup, recover, arm, or retry without that authorisation.

## ▶ Customer Zero + Trusted Beta — Phase A COMPLETE (2026-07-31); Phase B next
Programme directive widened Customer Zero to a full **trade-execution → ingestion → analytics** journey and
made the golden the permanent baseline. Full phased plan (A golden re-stage → B Phase-4 keyring → C execution
plane → D trading validation → E Trusted-Beta readiness): **`docs/TRUSTED_BETA_CZ_IMPLEMENTATION_PLAN.md`**.

**✅ Phase A DONE (2026-07-31): golden re-staged + promoted.** Build **5.0.0.6073** is the active beta golden at
`C:\GuvFX\golden\newMT5` (585 files, tree digest **`db54d94a…`**); prior golden retired-but-retained
(`newMT5.retired-20260731T072529Z`); Machine env re-pinned `BETA_AGENT_GOLDEN_DIGEST` + `BETA_AGENT_GOLDEN_MANIFEST_VERSION=5.0.0.6073`
(both required — B2 correction); agent Running. Nuno estate untouched. Blocker A REMOVED. Installer mode
`-ApplyGoldenAclOnly` shipped via PR #246. Evidence: `evidence/beta-agent-phase3-cert/GOLDEN_PROMOTION_2026-07-31.md`.

**One bounded next action — authorise Phase B (signing-key/keyring infrastructure):** provision the matching
agent + provisioner keyrings so `guvfx-beta-provisioner` stops logging `unknown_key_id` and ProvisioningJob #1
advances QUEUED → NEGOTIATE → runtime RUNNING for account #12. This is now the SOLE remaining blocker to the
"Runtime Running" milestone. Key finding still stands: the beta **execution plane is unbuilt** (Phase C) — a beta
slot "RUNNING" is view-only/broker-independent; Session-0 `order_send` on a slot is **unproven**. Do NOT begin
Phase B without Sponsor authorisation; DECISION GATE C0 (per-slot-bridge vs per-VM) deliberated in parallel.

## B3P-2 — on-demand task model decided + implemented; final slot-1 lifecycle next (2026-07-25)
TSV task-discovery is complete (`#212`/`#213`/`#214`). The follow-on task-enablement blocker is **resolved by
[ADR 0017](ADRs/0017-beta-task-enabled-triggerless-on-demand.md)**: the eight beta tasks are ENABLED but
TRIGGERLESS at rest. Installer registers them enabled (no `Disable-ScheduledTask`); credential-free
`-EnableTasksOnly` migrates an already-provisioned pool; runtime gate adds a zero-triggers invariant; both VERIFY
and enable paths assert no non-service principal can Run a task (RULE 11, host-measured safe). One bounded step:
- [ ] **Apply on host + run the full native slot-1 lifecycle.** Re-stage `install_pool.ps1`, RULE-9 parse, run
  `-EnableTasksOnly` (credential-free) to enable the eight tasks, then the signed
  `NEGOTIATE→VERIFY ABSENT→START→VERIFY PRESENT→STOP→VERIFY ABSENT→TOMBSTONE→RELEASE→Available` on slot 1 (the
  ADR-0016 occupancy `1f1b4b83…` gen 1 releases first, gen → 2). Production MT5 (4336) + bridge (13292) untouched.

## B3P-2 ADR-0016 Option A — merged + re-staged, host proof gated on Nuno (2026-07-25)
The launch-time process-ACL grant that makes unprivileged PRESENT attribution work. **Merged** (main
`23f38d8` #209 + parse-fix `fd716b8` #210), **re-staged** to the host, all install/wrapper scripts parse
**0 errors** under PS 5.1. One credentialed step blocks the rest:
- [ ] **Nuno: run `install_pool.ps1 -Apply`** on the host (prompts for the 4 slot `TASK_LOGON_PASSWORD`s,
  which the model must never see). It re-registers the 4 launch tasks with the wrapper action, creates + ACLs
  `C:\GuvFX\beta\launcher`, stages the hash-pinned `slot_launch.ps1`, and runs the VERIFY read-backs.
  **Re-stage the bundle first is already done** (byte-identical to main; manifest INTEGRITY_OK).
- [ ] **Then (autonomous):** CLM check **as `guvfx_b_slot1`** (if CLM enforced → hash-pinned precompiled exe
  fallback); PRESENT proof under `NT SERVICE\GuvFXBetaAgent` (before-grant `OpenProcess(slot)` DENIED →
  after-grant ALLOWED at `PQLI|READ_CONTROL` yielding the slot path + `guvfx_b_slot1` object-owner SID ==
  account SID; production stays denied/session-excluded; STOP still terminates a granted runtime = ACE
  additive); then slot-1 VERIFY→STOP→TOMBSTONE→RELEASE→Available, gen +1. Production MT5 (4336) + bridge
  (13292) untouched.

## B3P-2 RELEASE operation — SHIPPED to PR, host proof pending (2026-07-24)
`op_release` (ADR 0014, PR #200) closes the two lifecycle gaps below: it is the RELEASE protocol op that
advances the per-slot generation and frees the slot after TOMBSTONE, sourcing its proofs from a live
`observe_process → ABSENT`. 639 tests + `make check` green; real `build_agent` E2E proven offline.
- [ ] **Re-stage the agent bundle to the host, then prove slot 1** through the native lifecycle:
  `NEGOTIATE → VERIFY → STOP (only if VERIFY finds it running) → TOMBSTONE → RELEASE → Available`, gen 1→2,
  complete audit chain, production MT5 (pid 4336) + bridge (pid 13292) untouched. No manual intervention.
- [ ] **Deploy ordering:** the agent bundle (RELEASE present) must re-stage before/with any backend that
  expects it — `assert_compatible` requires the full `PROVISIONING_OPERATIONS` set (fail-closed).
- [ ] **Deferred to CVM-Inc-5:** wire the backend to SEND RELEASE after TOMBSTONE (`_drive_deprovision`),
  else a backend-driven deprovision tombstones without freeing. No live impact (`BETA_RUNTIMES_ENABLED` off).

## B3P-2 Phase 2A — waiting at the APPLY gate (2026-07-23)
Golden image approved and pinned; `install_pool.ps1` PLAN is clean; nothing is installed.
- [ ] **Nuno: accept the PLAN, then run `-Apply` locally** — it prompts for four passwords, which the model
  must never see, request, log or store. Invoke with **`-GoldenDir C:\GuvFX\golden\newMT5`**; the built-in
  default `C:\GuvFX\beta\golden` does not exist and aborts.
- [ ] **Re-stage the bundle first.** PR #181 adds a comment-only correction to `install_pool.ps1`, so the
  host copy no longer matches Git. Merge, re-copy, re-verify the checksum, parse-validate (RULE 9).
- [ ] After APPLY: Phase 3 verification → Phase 4 service-start gate → Phase 5 observation probe →
  Phase 6 bounded MT5 viability trial (**the trial question — does a GUI MT5 run under a
  `TASK_LOGON_PASSWORD` task with no interactive session — is still unanswered**).
- [x] ~~`open_handles()` has no supported Windows implementation~~ — RESOLVED by WS-B (PR #199): Restart
  Manager probe, host-proven with positive/negative controls.
- [x] ~~`release()` implemented but unwired (pool exhausts after `pool_size` tombstones)~~ — RESOLVED by the
  RELEASE operation (ADR 0014, PR #200); see the RELEASE section at the top. Backend-SEND wiring is the
  only remaining piece (CVM-Inc-5).

## Beta Onboarding — headless co-hosted vertical slice (2026-07-21) — onboarding stays CLOSED
Architecture is now **non-interactive headless co-hosting on the existing box** (no RDS/RemoteApp — supersedes
the 07-20 Option A plan); execution is a **vertical slice**. Increments 1–4 shipped (runtime state machine +
capacity + provisioning driver + Verification Report + broker-independent decoupling + broker abstraction).
- [ ] **Broker-login verification stage** — the ONE deferred part of the first slice. Blocked on Nuno providing
  a **separate disposable demo broker account** (NOT prod / existing demo). When available: wire a real MT5
  `verify_login` on the broker abstraction, flip `PROVISIONING_REQUIRE_BROKER_LOGIN=1` for beta, prove a runtime
  reaches RUNNING with `broker_login_verified=True` + exact identity match (control 8).
- [ ] **Finish the broker-independent slice wiring:** strategy assignment → 0.01 per-assignment sizing →
  AUTO_DEMO-ready state → truthful Account Status + Dashboard for a beta runtime (no broker connectivity needed).
  Each: test + adversarial review + controlled deploy with gates OFF.
- [ ] **Do NOT enable onboarding** until Phase 4 isolation gates pass (see KNOWN_ISSUES). No procurement without Nuno's approval.


## TI execution-gap follow-ups (2026-07-16)
- [ ] **Watch the daily-drawdown behaviour across a full day** — today's cumulative TI realised PnL
  reached −772.80 (still < $2000). If a losing streak pushes past −$2000, `daily_drawdown_hit` will
  correctly halt for the rest of the UTC day; confirm that reads correctly on `/operations.risk_state`.
- [ ] **Broker-time/UTC boundary for the drawdown day** — plan 27 closing at broker-03:07 (UTC 00:07)
  counts in the correct UTC day here, but the ~3h broker offset means the drawdown "day" and the
  broker trading day differ; tied to the pending broker-timezone probe.

## Bridge-stall follow-ups (2026-07-16)
- [ ] **After deploy, confirm the 429 storm stops** — worker `loop_error`/`rate_limited` rate → ~0,
  orphaned-SYNC count → ~0. SOAK the claim rate under active trading (signal-time burst).
- [ ] **Consider a dedicated worker throttle scope** if, under heavy concurrent load, one prioritized
  claim/loop plus other internal clients still approaches 100/min (evidence did not warrant it yet).

## TP-protection finalisation follow-ups (2026-07-16)
- [ ] **Complete the 24/48/72h soak** — the durable latency instrumentation (`close_ingested_at` +
  `protection_latency`) is live; aggregate before/after latency + soft-deferral distribution from
  natural trades. SOAK-IN-PROGRESS; do not force a trade.
- [ ] **Verify the broker UTC offset** (`BROKER_UTC_OFFSET_HOURS`, currently assumed +3, unverified) —
  the two broker-anchored latency segments (A, H) depend on it; the offset-independent
  ingestion→verified segment does not. Tied to the pending broker-server-timezone probe.

## TP-protection latency follow-ups (2026-07-16 watcher packet)
- [ ] **After arming the watcher, capture before/after latency** on the next natural TI trade where
  TP1/TP2 close while TP3 runs (target: TP2-lock verified within seconds of ingestion, not ~1 min).
- [ ] **Diagnose the intermittent MT5 bridge SYNC/PLACE_ORDER ~6-min hang** if `protection_sync_stall`
  fires again — the short lease bounds the symptom but the bridge-side stall is the root.

## Post-deploy stabilisation follow-ups (2026-07-16 packet)
- [ ] **Capture the first natural TP2_LOCKED broker proof** now that the ladder is armed + hardened
  (leg 3 SL → the TP2 price on a signal where TP1 and TP2 both close while TP3 runs). Do not force.
- [ ] **Confirm the re-scaled drawdown admits the next post-loss signal** on the next day ti_signals
  takes an early loss then signals again (expect promotion, not `daily_drawdown_hit`).
- [ ] **Broker-server timezone probe** (still Red/Nuno) — also aligns the drawdown "day" boundary.

## Post-incident stabilisation follow-ups (2026-07-16 packet)
- [ ] **Capture the first natural incremental-TP-protection broker proof** on the next eligible
  ti_signals plan (TP1→remaining SL at entry; TP2→TP3 SL at the TP2 price). Auto-captured; still
  EVIDENCE-PENDING. Do not force a trade.
- [ ] **Operator (PM): the 2 stale OPEN CRITICAL alerts** — `RECOVERY_CIRCUIT:global` (2026-07-07)
  and `EXECUTION_PIPELINE:0:0` (2026-07-15 14:29, pre-dates the packet). Ack/clear; decide on
  enabling the dormant reliability core.
- [ ] **Confirm the soak cron now accumulates** hourly snapshots (log-perm fixed 2026-07-16); read
  48–72h trends once available.

## Current next action (single)
- [ ] **Broker-server timezone determination probe (Red, needs Nuno's approval):**
  verify the TradersWay-Demo server timezone before any normalisation or broad
  backfill. MT5 bar times are broker-server time, not guaranteed UTC; no offset may
  be hardcoded. This touches real data, so it is a Nuno-gated Red step.

> ✅ Done 2026-06-28: **S1** (approved `GuvFXData` storage root provisioned) and
> **GFX-PKT-006D-A2-P5** (first durable immutable raw object + provenance manifest,
> SHA-256-verified in GuvFXData; idempotent). This is the first real GuvFX
> market-data object.

> The synthetic 006C foundation arc is fully merged (PR #36, `main` `148437a`). The
> live frontier is the **006D** real-data acquisition workstream in the dedicated
> `guvfx-windows-history-agent` repo + governed VPS probes — see
> `docs/PROGRAMME_STATE.md`. Notion (*Current State v0.52*) is authoritative.

## Phase-2 hardening + signal-copy follow-ups (2026-07-15 packet — separate track)
- [ ] **Nuno decision (Red): arm the provider-command engine** — `PROVIDER_COMMANDS_ENABLED=1` +
  ti_signals `command_engine_enabled=True`, in a controlled window (see KNOWN_ISSUES). Until then it
  records commands but takes no action.
- [ ] **Capture the first natural incremental-TP-protection broker proof** — on the first eligible
  ti_signals plan, confirm a `MODIFY_POSITION` `result.verified_sl` for BOTH stages: TP1→remaining
  legs' SL at entry (BREAKEVEN) and TP2→TP3 SL at the TP2 price (TP2_LOCKED). Auto-captured, not
  forced. Until then the two headline claims read EVIDENCE-PENDING (see KNOWN_ISSUES).
- [ ] **Soak result becomes meaningful after ≥24–72h** continuous armed operation — read
  `SoakSnapshot` trends (hourly cron installed).
- [ ] **Operator (PM): reliability core + circuit breaker** — enabling `RELIABILITY_CORE_ENABLED` and
  resetting the stale `RECOVERY_CIRCUIT:global` breaker (carried over from the prior packet).

## Production-stabilisation follow-ups (2026-07-15 packet — separate track)
- [ ] **Capture auto-breakeven broker evidence** on the first natural TP1 close (`MODIFY_POSITION`
  job `result.verified_sl` + leg `breakeven_applied_at`) — the one pending WS-B verification.
- [ ] **Operator decisions (PM/Nuno):** reset the stale `RECOVERY_CIRCUIT:global` circuit breaker and
  decide whether to enable `RELIABILITY_CORE_ENABLED` (turns on automated recovery) — see KNOWN_ISSUES.
- [ ] **Optional:** add an `/operations` nav link (page is deployed, URL-only today).

## PM improvement backlog (in progress, Claude-as-PM)
Green/Amber items proceed autonomously; Red items are flagged for Nuno's approval.
- [x] **A — reconcile these stale handoff docs** to the true 006D/S1 state.
- [x] **B — `docs/PROGRAMME_STATE.md`** consolidated packet→repo→status→evidence index.
- [x] **C — `GUVFX_DATA_ROOT` preflight validator** — `scripts/check_data_root.py`
  + `tests/test_data_root.py`, wired into `make governance-check` + CI.
- [ ] **D — evidence-factuality linter** (file/test counts, clean-tree, checksums).
- [ ] **E — enforce read-only MT5 boundary** (verify/added CI AST guard).
- [ ] **F — broker-server timezone probe** — **NEEDS NUNO APPROVAL (Red, data)** — *next gate*.
- [ ] **G — live Trading path standing risk-watch** (kill-switch, failure modes).
- [ ] **H — ratify the Blueprint** (Proposed → Approved) — **NEEDS NUNO SIGN-OFF**.
- [ ] **I — reconcile role vocab + ADR-009 numbering collision**.
- [x] **J — backup & DR** — decided: Phase-1 NAS-local (RAID); offsite deferred.
- [x] **K — record PM governance state in Notion** (operating model + S1/P5 records).

## P0 (historical)
1. [x] Resolve local docs diffs cleanly: either (a) commit `docs/HANDOFF.md` + `docs/STATUS.md` on a small `docs/...` branch and open a PR to `main`, or (b) restore them if they are outdated. — done 2025-12-16
2. [x] Confirm repo health: run `make check` on `main` and on the active feature branch. — done 2025-12-16
3. [x] Broker autocomplete MVP: define acceptance criteria and implement debounced broker search + selection flow. — done 2025-12-16
4. [x] Add tests/guardrails for broker autocomplete (minimum: type-safe API response handling + basic UI state tests if available). — done 2025-12-16

## P1
1. [ ] Cleanup follow-ups: ensure `.trash_duplicates/` stays ignored and remove any remaining duplicate “(1)” / “ 2” files if they reappear.
2. [x] Switch login reason parsing to a lazy `useState` initializer so the client-only `window` lookup happens safely.
3. [x] Silence the remaining frontend ESLint warnings in `accounts`, `backtests`, and `profile` so `make check` stops failing because of lint.
4. [x] Track keyboard navigation edge cases (wrap, visibility, focus) as follow-up work before the next release; `fix/broker-autocomplete-edgecases` re-applied the debounce/keyboard nav/abort flow for broker suggestions and now needs verification on real data. — done 2025-12-16
5. [x] VPS deployment + domains + Traefik + Guacamole routing completed and serving production traffic (live 2025-12-16).
6. [ ] Verify MT5 handoff automation end-to-end (multiple accounts) using the shared `/srv/guvfx/mt5_handoff` configs.
7. [ ] Investigate/fix MT5 mouse input reliability through Guacamole (mouse clicks freeze until File menu is toggled).
8. [ ] Harden MT5 automation (secure password handling, per-account JSON, and optional `SUBMIT=1` gating for `apply-account-config`).
9. [ ] Bake the `apply-account-config` automation + Openbox autostart into the `mt5free-desktop` image once the workflow stabilizes.
10. [x] Decision: continue using host bind mounts for MT5 automation scripts rather than baking them into the container images. — done 2025-12-16

## Parking lot (later)
- Ideas/notes that are **not** committed work

## Backlog (documented, not scheduled)
- [ ] **Registration Flow Enhancement** — Multi-step registration with email verification, hosting selection, compliance acknowledgments, and 2FA. See [`docs/REGISTER_FLOW_TODO.md`](./REGISTER_FLOW_TODO.md) for full plan.

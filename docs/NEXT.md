# NEXT — Priorities (keep this list short)

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
- [ ] Still Nuno's calls, both blocking a *complete* lifecycle but not the trial: `open_handles()` has no
  supported Windows implementation (TOMBSTONE refuses before moving anything), and `release()` is
  implemented but unwired (the pool exhausts after `pool_size` tombstones).

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

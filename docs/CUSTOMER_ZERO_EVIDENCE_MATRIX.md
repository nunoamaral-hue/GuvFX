# Customer Zero — Evidence Matrix (authoritative programme dashboard)

**Rule:** no stage is marked complete without *observed production evidence*. Architecture suggesting it
should work does **not** count. Every implementation phase updates this file.

**Subject:** Customer Zero = prod user **#16** `beta.guvfx01@gmail.com`; account **#12** (`1302575`,
`IS6Technologies-Demo`). **Last observed:** 2026-07-30 (read-only prod: `guvfx-backend` shell,
`guvfx-beta-provisioner` logs/env). **Programme order:** A golden re-stage → B keyring → C hosted-MT5
validation → D BUG-001 → E execution plane.

Legend: ✅ proven · ⚠️ partial · ⛔ blocked · 🔨 future build.

| Stage | Current Status | Observed Evidence | Owner | Blocking Issue | Next Action | Acceptance Criteria |
|---|---|---|---|---|---|---|
| 1 Registration | ✅ proven | prod user #16 exists, `is_active=True` | Platform | none | — | account exists + can authenticate |
| 2 Email verification | ✅ proven | onboarding `email_verified=True` (beta = allowlist admission) | Platform | none | — | verified flag set |
| 3 Login | ✅ proven | authenticated cookie session serving #16 | Platform | none | — | authenticated requests succeed |
| 4 Onboarding | ✅ proven | `onboarding_completed=True`; email+plan+risk True | Platform | none | — | minimum steps complete |
| 5 Broker account creation | ✅ proven | account #12 created (ADR-0021; `mt5_instance=None` by design) | Platform | none | — | TradingAccount row exists |
| 6 Runtime assignment | ⚠️ partial (reserved, not advancing) | `AccountRuntime` pk1 `cohort=BETA state=QUEUED uuid 66972e0e…`; `ProvisioningJob #1 op=PROVISION status=QUEUED`; `BETA_RUNTIMES_ENABLED=1` | Sponsor (auth) / Claude (exec) | **Blocker B** (keyring) + **Blocker A** (golden) | Phase A then Phase B | job advances past QUEUED; `ProvisionerHeartbeat>0` |
| 7 Runtime healthy (RUNNING) | ⛔ blocked | runtime `QUEUED`; provisioner log `negotiation failed job=1: unknown_key_id`; `KEYRING/KEYID/BASE=EMPTY`; heartbeats=0 | Sponsor (auth) / Claude (exec) | Blocker A + Blocker B | Phase A→B→C; observe QUEUED→…→RUNNING | `state=RUNNING` + Verification Report |
| 8 MT5 instance attached | ⛔🔨 (BUG-001) | beta uses `AccountRuntime` not legacy `Mt5Instance`; #12 `mt5_instance=None`; error from legacy `trading/views.py` actions | Claude (Phase D) | legacy endpoints not beta-aware + upstream 7 | Phase D — see `docs/BUGS.md` BUG-001 | accounts page reflects AccountRuntime, no false "no mt5_instance" |
| 9 Broker login validated | ⛔🔨 | `PROVISIONING_REQUIRE_BROKER_LOGIN` OFF; `configure()` no-op; never attempted | Claude (Phase E) / Sponsor (demo acct) | broker-login stage unbuilt; no disposable demo account | Phase E-C2 | `broker_login_verified=True` (platform determination) |
| 10 Strategy assignment | ⛔ blocked (correct) | assign requires `account.is_active=True` (`views.py:557-559`); #12 `is_active=False` | Claude (Phase E) | upstream 7 (runtime→active) | after Stage 7 proven | StrategyAssignment row for #12 |
| 11 Strategy deployment | ⛔🔨 | dormant `SessionAssignment`/`bridge_identity` | Claude (Phase E) | per-slot routing unbuilt | Phase E routing | strategy targets #12's runtime |
| 12 Strategy activation (AUTO_DEMO) | ⛔🔨 | arming needs `is_demo AND is_active` (`:1079`) + `MULTI_ACCOUNT_ROUTING_ENABLED`/`BETA_SELF_SERVE_ARM_ENABLED` (unset) | Claude (Phase E) / Sponsor (levers) | Class-B levers + per-account sizing | Phase E sizing + staged enablement | `AUTO_DEMO`+`STAGE_LIVE`+`is_active` armed |
| 13 Trade execution | ⛔🔨 | no per-slot order path; agent protocol lifecycle-only; Session-0 `order_send` UNPROVEN | Claude (Phase E) / Sponsor (demo acct, order auth) | execution plane unbuilt + feasibility unproven | Phase E — C-spike first | demo order on #12's slot, isolated from Nuno |
| 14 Trade ingestion | ⛔🔨 | single-tenant ingest worker; #12 has no node | Claude (Phase E) | per-slot ingest unbuilt | Phase E routing | `Trade` rows for #12 |
| 15 Analytics | ⚠️🔨 (read layer ready) | `AccountPerformanceView`/`TradeHistoryView` generic + user-scoped | Platform (auto) | depends on 14 | none in read layer | analytics return #12's numbers |
| 16 Dashboard validation | ⚠️🔨 (read layer ready) | beta dashboard reuses analytics + truthful account-status | Platform (auto) | depends on 6–15; heartbeat/notif stubs | minor: real heartbeat/notif | end-to-end render for #12 |

## Two production blockers (observed)

**Blocker A — RESOLVED 2026-07-31 (golden re-staged + promoted).** The drifted golden was replaced by a pristine
dedicated build-5.0.0.6073 install. Promotion executed (7 steps, adversarially pre-reviewed): retired pid 5912 +
Strategy Tester firewall rule; `newMT5`→`newMT5.retired-20260731T072529Z` (retained); `staging`→`newMT5` (585 files);
`-ApplyGoldenAclOnly`; full `-VerifyOnly` tree digest = pinned **`db54d94a…`**; Machine env re-pinned
`BETA_AGENT_GOLDEN_DIGEST=db54d94a…` + `BETA_AGENT_GOLDEN_MANIFEST_VERSION=5.0.0.6073` (both required — verified),
agent restarted Running (positive/negative env-propagation control). Nuno estate untouched.
(Evidence: `evidence/beta-agent-phase3-cert/GOLDEN_PROMOTION_2026-07-31.md`; provenance `GOLDEN_PROVENANCE_2026-07-30.md`;
digest `GOLDEN_6073_DIGEST_2026-07-30.md`.) **Only Blocker B (keyring) now stands between #12 and Runtime Running.**

**Blocker B — provisioner authentication.** `guvfx-beta-provisioner` `BETA_AGENT_KEYRING`/`KEY_ID`/`BASE_URL`
**EMPTY** → `unknown_key_id` on every NEGOTIATE for job #1 → runtime stuck QUEUED. → **provision matching
keyrings (agent + provisioner).** This is the direct cause of Customer Zero not reaching Runtime Running.

## Runtime panel is correct (do not "fix")
`build_account_status(#12)` = `phase=provisioning_runtime`, steps `account_received=done →
provisioning_runtime=current → validated=pending`. Truthfully reflects `AccountRuntime=QUEUED`.

## Dependency graph
```
Blocker B (keyring EMPTY) ── AND ── Blocker A (golden) ✅ RESOLVED 2026-07-31 (build 6073 promoted, digest db54d94a…)
      ▼
ProvisioningJob #1 QUEUED → AccountRuntime QUEUED → account #12 is_active=False / mt5_instance=None
      ├─► "test connection" → legacy Mt5Instance endpoint → "no mt5_instance assigned"  [BUG-001, separate]
      ├─► account-status panel → "Provisioning runtime"  [truthful]
      ├─► marketplace assign → requires is_active=True → correctly prevented
      └─► Wayond WIM → future work (Runtime Running → assignment → execution plane)
```
Clearing Blockers A+B unblocks Stages 6–7 and (via account activation) Stage 10. Stages 8, 9, 11–14 need
additional build. Stages 15–16 light up automatically once per-account `Trade` rows exist.

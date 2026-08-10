# Beta Readiness Report — Post Customer Zero Certification

Compiled 2026-08-10 (main @ current). Method: a 10-agent parallel read-only gap-analysis across the 10 Beta
Readiness workstreams (WS1–WS10) plus this synthesis. **No architecture redesign, no execution enablement,
no Customer Zero mutation** — read-only analysis + plans only. Execution remains DARK
(`HOSTED_MT5_EXECUTION_ENABLED` unset; ASN #7/#8 AUTO_SHADOW).

> Two synthesis caveats are corrected against ground truth: (a) `CAPACITY_MODEL.md` **is merged to main**
> (PR #337); (b) execution-plane "no regression" is backed by a **green `make check` (3523 backend tests,
> run twice)**, not code-reading alone. Everything else stands as written.

The full per-workstream deliverables (Current State / Target / Gap / Implementation / Evidence / Risks /
Decisions for WS1–WS9) are archived in the workflow transcript; this report carries the executive summary,
the operational checklist (WS10), and the consolidated Sponsor/CA decisions.

---

BETA READINESS — EXECUTIVE SUMMARY

VERDICT: "First internal beta user can sign up unassisted" is NOT achievable today. The front half of onboarding (register → verify → plan → risk → workspace-request) is genuinely automated, but a self-registered user then STRANDS: node allocation has no caller (G2), the observation loop has no scheduler (G15) so the confirm/ready gate never resolves, all per-user host materialisation is manual (identity, golden MT5, RDP, RemoteApp publish, AppLocker) and one required artefact — the NTFS-ACL script (G5) — does not exist at all, and there is no customer UI to drive the hosted journey (G18). Execution remains correctly DARK throughout.

WHAT IS DONE (verified/certified):
- App onboarding: registration + auto beta entitlement, genuine-SMTP email verification, plan/risk completion — automated and tested; admission is two global DARK flags (not a per-user allowlist).
- Customer-Zero hosted workspace: broker login live inside a Guacamole RemoteApp (1302561 on IS6Technologies-Demo, 96 symbols), AppLocker Enforce SID-scoped with the HIGH#1 writable-path bypass fixed (PR #334), single-session enforced (PR #330), node authority bound, delivery-connect card deployed to prod.
- AutoTrading auto-config for the SLOT-POOL path (PR #336) — both-keys minimum empirically certified; digest-safe, idempotent, tested.
- Execution plane: multi-layer fail-closed DARK, no regression across merges #316/#317/#318/#335/#336 (by code reading); credential separation clean across all three Telegram surfaces; WIN-card path is exactly-once and executor-agnostic.
- Operational-readiness read-only layer (ADR-0035) and reliability health/alerting merged.

CRITICAL PATH to first unassisted internal beta user:
1. Repo-only, mergeable DARK now (Phase 1): a driver that auto-calls allocate_workspace_node after request (G2); persist rdp_host durably (G12); schedule run_hosted_observations (G15).
2. Repo-only, DARK, parallel (Phase 4): customer hosted-journey UI (request/journey/confirm) and make /accounts create the hosted intent account (G18).
3. Host-cert gated (Sponsor authorisation required): author the missing NTFS-ACL script (G5) and compose ONE idempotent per-user prepare_hosted_slot op (identity → ACL → golden → RDP → RemoteApp publish → AppLocker -HostedUser) under RULE 1/9/10/11; add a host-resident read-only observe bridge feeding the scheduler.
4. Once-per-host machine-wide steps (host-cert runbook): RDS role, SPLA/CAL licensing, single-session, AppLocker baseline.
5. Flip HOSTED_PERSISTENT_MT5_ENABLED + HOSTED_WORKSPACE_ONBOARDING_ENABLED (+ delivery flag). Execution stays DARK. Scaling lever = raise TerminalNode.max_accounts (do NOT route through the slot-pool cap of 5).

TOP 5 RISKS:
1. Orchestration dead-ends (G2 allocation + G15 observation have no callers) — the two highest-value unblocks; both are repo-only and shippable DARK now.
2. Host per-user provisioning is 100% manual and a required artefact (NTFS-ACL, G5) does not exist — no unassisted path without Sponsor host-cert AND new automation that expands what the host agent may do (create local users, edit ACLs, publish RemoteApps); an Amber security-posture expansion needing an ADR.
3. Multi-tenant Telegram uses a single global TELEGRAM_CHAT_ID — cross-tenant WIN-card data exposure the moment a 2nd tenant is armed; load-bearing private-beta blocker.
4. No automated DB/Guac backup + single-VPS SPOF (TECH_DEBT #1/#5) — autonomous provisioning writes more durable state (identities, node bindings) with no verified restore; blast radius scales with users.
5. Execution-cert subject = LIVE account #1/1302561 (HIGH#2) plus the Wayond cutover ordering-inversion — certifying/executing on a live-traded account, and any premature quiesce-reverse fires real orders on the legacy path; blocks the demo-trade/execution gate and sets beta account policy.

HONEST CAVEATS (no fake readiness): capacity figures (~15/~40) are from ONE idle workspace and CAPACITY_MODEL.md is not on main; host telemetry is absent so utilisation triggers are unenforceable; the execution test suite was NOT run (code-reading only); host topology and RDS licensing state (workgroup + 120-day grace, no CALs) are INFERRED, not confirmed.
---

WS10 — PRE-BETA OPERATIONAL CHECKLIST
Status key: [GREEN]=verified/certified/merged+tested · [AMBER]=partial, works-with-caveat, or design-only touching shared structure · [RED]=blocked on Sponsor/CA/host/irreversible action · [NOT-TESTED]=no evidence produced, unwired/no-caller, or unverifiable read-only.

1. PROVISIONING (in-app onboarding, certified hosted-workspace path)
- [GREEN] Account registration + auto beta entitlement — POST /api/auth/register/ auto-grants can_use_hosted_workspace; gated by REGISTRATION_ENABLED (default true); automated.
- [GREEN] Email verification — genuine SMTP, honest 502; allowlist bypass retired (ADR-0021).
- [GREEN] Plan + risk → onboarding_completed — REQUIRED_STEPS flips completion; automated.
- [AMBER] Broker-profile → workspace request (POST hosted-workspace/onboarding/request/) — creates intent account, but DARK behind 2 flags and API-only (no frontend).
- [AMBER] Admission gate — two global DARK flags + auto beta entitlement (NOT per-user allowlist); correct design, flags OFF.
- [RED/NOT-TESTED] Node allocation (allocate_workspace_node) — G2: NO non-test caller; a requested workspace never advances to WAITING_FOR_LOGIN. Single biggest orchestration gap.
- [AMBER] rdp_host durability — G12: set via Django admin, not a durable server-derived binding.
- [AMBER] Identity provisioning (services.provision → guvfx_u_<id>) — G3: reachable only via manage.py command; hosted flow does not call it.
- [RED] Windows materialisation (Provision-GuvfxAccount.ps1) — G4: hand-run over SSH.
- [RED/NOT-TESTED] NTFS ACL on runtime tree — G5: NO script exists at all (grep icacls/Set-Acl empty).
- [RED] Golden MT5 seed into runtime_root\terminal — G13: manual copy.
- [RED/NOT-TESTED] Observation scheduler (run_hosted_observations) — G15: NO scheduler caller; canonical CONNECTED/EXECUTION_READY never auto-derived → confirm gate cannot pass unassisted.
- [NOT-TESTED/AMBER] Frontend hosted-journey UI — G18: no customer UI calls request/journey/confirm; /accounts still creates a legacy account.

2. HOSTED MT5 GOLDEN TEMPLATE / AUTOTRADING
- [GREEN] Slot-pool AutoTrading auto-write (PR #336) — writes [Experts] AllowLiveTrading=1+Enabled=1 idempotently after stage post-check; digest-excluded; tested; bundle hashes match manifest; marker HOSTED_AUTOTRADING_CONFIGURATION_CERTIFIED.
- [GREEN] Empirical both-keys minimum — AllowLiveTrading=1 alone → trade_allowed=false; +Enabled=1 → true (CZ).
- [RED/AMBER] Auto-config on the path CZ/hosted actually uses — per-account C:\GuvFX\accounts\<id>\terminal is populated AllowLiveTrading=0; CZ needed a MANUAL flip; PR #336 does NOT reach it. Model-convergence decision required.
- [NOT-TESTED] Beta-agent bundle deployment to host — unverifiable read-only.

3. REMOTEAPP / TERMINAL ACCESS
- [GREEN] RemoteApp delivery card — auto-detects owned workspace; delivery-connect mints signed descriptor server-side; deployed prod.
- [GREEN] RemoteApp live on CZ — rdpshell/rdpinit/rdpclip in session 3.
- [GREEN] Guacamole on-the-fly token — no persistent DB record; origin-pinned clean auth.
- [GREEN] Server-side auto-resize ready — resize-method=display-update set, no fixed w/h.
- [AMBER/NOT-TESTED] Fullscreen / expand / responsive / size-memory — design only (WS6); current embed fixed 640px capped at 1100px.
- [RED/NOT-TESTED] REMOTEAPP_ISOLATION marker — withheld; needs behavioural 8004 escape evidence.

4. BROKER ONBOARDING
- [GREEN] Broker login inside RemoteApp (CZ) — 1302561 authorized on IS6Technologies-Demo, 96 symbols synced, trading enabled under AppLocker Enforce.
- [AMBER] Confirm-broker-account gate — needs canonical CONNECTED+match from the (unscheduled) observation loop.
- [RED] Execution-cert subject — CZ uses LIVE account #1/1302561 (HIGH#2); beta account policy undecided.

5. DASHBOARD / READY-TO-CONNECT
- [GREEN] Dashboard + ready-to-connect UI — delivery-state auto-detect + Open MT5 Terminal built and localised.
- [AMBER] Optimistic-readiness divergence (TECH_DEBT #11) — setup router advances on state==RUNNING while arming uses strict account_runtime_ready; hosted UI must read canonical or a user dead-ends.

6. TELEGRAM
- [GREEN] WIN-card (profit) path — exactly-once (OneToOne + atomic claim + durable pre-finalize + idempotency belt + reaper + reconcile), executor-agnostic; tested.
- [GREEN] Ops health alerts — reliability + validation-agent surfaces with SEPARATED, collision-guarded credentials (RULE 3/6).
- [GREEN/AMBER] DARK-by-default — NOTIFICATION_DISPATCH_ENABLED off + dry-run default transport; sends nothing today.
- [N/A/NOT-TESTED] Signal / trade-open customer notifications — do NOT exist; unconfirmed whether legacy CZ sent them.
- [RED] Per-tenant destination — single global TELEGRAM_CHAT_ID; cross-tenant WIN-card exposure at >1 user. Multi-tenant blocker.
- [NOT-TESTED] Customer-path delivery probe / behavioural equivalence — no first-class probe; real-trade parity deferred to M5.

7. PERSISTENCE
- [GREEN] Per-user persistent portable workspace (ADR-0033) — certified for CZ; node binding workspace_node=execution_node=terminal_node=1.
- [NOT-TESTED] MT5 panel/dock layout persistence host-side — assumed, not verified; frontend cannot store it.

8. RECOVERY / RESET / ROLLBACK
- [AMBER] Beta-user reset — design only (WS3); backup-first, soft/hard depths; no execution.
- [NOT-TESTED] Customer-Zero delete-guard — no tooling today; plan adds explicit id=1 assertions.
- [RED] Host teardown (identity/ACL/RemoteApp/Guac + reclaim_beta_runtime) — irreversible; Sponsor-gated.
- [AMBER] DB↔host decoupling — no delete signals; DB-only reset silently orphans host artefacts; both planes must be reconciled.
- [GREEN] Wayond-cutover rollback — legacy retained; rollback = swap the broker login back, never rebuild.

9. MONITORING
- [GREEN] Operational-readiness read-only layer (ADR-0035) — operational_health/preflight/rollback_planner/evidence; merged, read-only.
- [GREEN] Reliability ComponentHealth + alerting — exists, drives ops alerts.
- [RED/NOT-TESTED] Host resource telemetry — no RAM/CPU sampler; register_host_capacity_probe UNWIRED so host_has_capacity() is a no-op; admission gated only by the static count.
- [NOT-TESTED] Utilisation-% triggers — unmeasurable live until a sampler is wired.

10. CAPACITY
- [GREEN] Capacity primitives — TerminalNode.max_accounts / has_capacity; scaling lever = raise max_accounts (NOT slot-pool BETA_MAX_ACTIVE_RUNTIMES=5).
- [AMBER] Capacity model figures (~15 comfortable / ~40 hard) — measured from ONE idle signal-copy workspace; CAPACITY_MODEL.md on a branch NOT merged to main; must re-measure under active trading.
- [NOT-TESTED] Capacity+licensing thresholds doc — does not exist; WS8 is the plan to author it.
- [NOT-TESTED] Scale-out placement/affinity policy — none (one node today); needed at 2nd host.

11. SECURITY
- [GREEN] Execution DARK, multi-layer fail-closed — auto_router AND-gates, ExecutionJob.save central gate, kill switch, broker gate, identity pin, guarded-attach; no regression after #316/#317/#318/#335/#336 (code-reading).
- [GREEN] AppLocker Enforce SID-scoped (CZ) — Exe/Msi/Script enabled, denies scoped to guvfx_u_1, admin recovery intact; HIGH#1 writable-path bypass fixed (PR #334).
- [AMBER] AppLocker residuals — %WINDIR% LOLBINs (rundll32/regsvr32/msbuild) Everyone-allowed; escapes decision-proven, not behaviourally closed.
- [GREEN] Credential separation / no secrets in git/logs — three Telegram surfaces use distinct secrets; delivery password stays inside AES token only.
- [AMBER] Terminal-side margin removed — hosted slots now default AutoTrading ON (PR #336); backend flags are now the sole gate DARK→live.
- [NOT-TESTED] Execution test-suite PASS evidence — suites exist but were NOT run (read-only mandate); "no regression" is a code-reading result, not a green run.

12. DOCS
- [GREEN] Host-cert runbooks — WORKSPACE_DELIVERY_HOST_CERTIFICATION, EXECUTION_ENGINE_* exist.
- [AMBER] CAPACITY_MODEL.md — exists on a branch, not merged to main.
- [NOT-TESTED] CAPACITY_AND_LICENSING_THRESHOLDS.md — not written yet.
- [AMBER] Handoff docs — project convention requires STATUS/HANDOFF/NEXT/KNOWN_ISSUES updates each change (not re-verified this pass).

13. KNOWN LIMITATIONS
- Two provisioning models coexist (slot-pool vs per-account) — ADR-gated cutover (TECH_DEBT #7) not done; PR #336 auto-config reaches only the slot pool.
- i18n drift — nav JA / content EN; several hundred untranslated customer-facing strings; no CI guard (WS7).
- Prod/host state unverifiable read-only — live flag values, readiness_provider, execution_mode, bundle deployment, and host topology/licensing are all INFERRED, not confirmed.

14. OPEN RISKS (top-level)
- [RED] Single-VPS SPOF + no automated DB/Guac backup (TECH_DEBT #1/#5) — blast radius grows with users; autonomous provisioning writes durable state with no verified restore.
- [RED] Wayond-cutover ordering-inversion — reversing the quiesce before hosted routing is certified fires orders on the LEGACY path; the reverse must be the LAST step.
- [AMBER] Co-tenant coupling — readiness_provider is per-account, so account #1's cutover moves wayond (asn#7) + ti_signals (asn#8) together.
---

OUTSTANDING DECISIONS — Sponsor / CA only (not engineering). Consolidated and de-duplicated across WS1–WS9.

A. HOST CERTIFICATION & LICENSING (Sponsor / procurement)
1. Authorise the WORKSPACE_DELIVERY_HOST_CERTIFICATION packet (H1 RDS role, H2 RemoteApp publish, H3 SPLA/RDS-CAL licensing, H4 AppLocker, H5 NTFS-ACL) on a disposable cert host — prerequisite for all per-user host automation. Never on shared prod 100.79.101.19.
2. RDS licensing route: SPLA/rented per-authorised-user (elastic; ~$4.50 US / €7.90+VAT EU per user/mo, or AWS EC2 RDS SAL $10/user/mo) vs OWNED (Per-User CAL ~$110–160 + base WS User CAL ~$40, requires standing up own AD DS). Must be decided BEFORE admitting the 2nd named user. Licensing is a pure Sponsor/procurement call engineering cannot waive.
3. Per-User vs Per-Device CAL: engineering recommends Per-User (matches the guvfx_u_<id> model), which FORCES AD DS / domain-join — decide whether to domain-join the CZ host or stand up a separate infra host.
4. Confirm four host-state facts (operator, read-only): workgroup vs domain; RDSH role install date (120-day grace-clock start); any existing licensing server/CALs; Windows Server licensing basis (bundled/SPLA vs BYOL per-core). Every downstream capacity/licensing number depends on these.
5. Scale-up vs scale-out preference at the ~30–40 workspace band: grow the single 32 GB host (cheapest, keeps SPOF) vs add a 2nd Windows host early to remove the single-host SPOF (TECH_DEBT #5).

B. EXECUTION / ARMING (Red — Nuno explicit approval)
6. Execution-cert subject (HIGH#2, top blocker): certify hosted execution on the LIVE account #1/1302561 with legacy logged out first, OR provision a disposable/dedicated demo account. Also sets the beta cohort account policy (no beta user reuses a live-traded account).
7. Authorise flipping HOSTED_PERSISTENT_MT5_ENABLED + HOSTED_MT5_EXECUTION_ENABLED and arming the workspace.
8. Authorise reversing the quiesce (execution_mode AUTO_SHADOW→AUTO_DEMO on asn#7/#8) — restores live/paper trading authority; must be the FINAL post-certification step, never concurrent with wiring.
9. Co-tenant handling: accept that account #1's cutover moves wayond + ti_signals together (readiness_provider is per-account), or first split them onto separate TradingAccounts.
10. Schedule the contention-swap window: log 1302561 out of legacy, in inside the RemoteApp, and capture the outstanding RULE-11 positive control (flushed 'authorized' in the portable journal).
11. Confirm the intended DARK flag combination for the CZ→beta window (master flag ON for observation while the execution flag stays OFF) — a posture confirmation, not an engineering change.

C. ARCHITECTURE / CONVERGENCE (CA — ADR-gated)
12. Model convergence: is the canonical hosted-EXECUTION runtime the slot pool (route A) or the TX-1 per-account terminal (route B)? Determines whether PR #336's auto-config reaches every future user; and whether to authorise the bounded route-B per-account execution-populate as the near-term unblock (TECH_DEBT #7).
13. Per-user host provisioning: a standing automated prepare_hosted_slot host-agent op vs operator-run for the ~5-user pilot — automating it widens the host agent's authority (creating local users, editing ACLs, publishing RemoteApps), an Amber security-posture expansion needing an ADR.
14. Approve the per-tenant Telegram destination design (per-account/workspace chat id, defaulting to the global env for CZ back-compat) — touches the notification contract, ADR-gated.
15. Approve standardising machine error-codes on customer-facing endpoints (backend error-contract change) to enable localised, non-leaky error copy.

D. PRODUCT / SCOPE (Sponsor / product)
16. Telegram equivalence scope: WIN-card only (current) vs also build signal-received / trade-open customer notifications (no legacy notifier for those was found).
17. i18n scope: full JA vs customer-path-only with staff screens left English; keep the homegrown dictionary vs adopt a library (needs an approved decision under no-speculative-infra); and who funds qualified human JA review of legal/compliance/disclaimer strings.
18. Notification localisation: is the JA Telegram WIN-card PNG (JA font + per-user locale + resolving the single global chat id) in or out of beta scope?
19. Fullscreen R5 "immersive / hide app nav": sign off the shared AppShell change or defer in favour of native Fullscreen only (ship R1/R2/R3/R4/R12 first).
20. Capacity posture: confirm raising TerminalNode.max_accounts as the scaling lever, NOT routing hosted through BETA_MAX_ACTIVE_RUNTIMES=5; and confirm/produce the CAPACITY_MODEL figures.
21. Timing of flipping the onboarding flags to open unassisted registration — gated on Phases 1–4 landing and host-cert markers (REMOTEAPP_ISOLATION + host-cert) being emitted.

E. OPERATIONS / DATA (Sponsor — operator-run, credentialed)
22. Beta-user reset (support@guvfx.com, beta.guvfx01@gmail.com): choose depth (soft/reversible — recommended — vs hard delete incl. erasing immutable provisioning-audit rows); host-teardown scope (irreversible); BetaTester allowlist remove vs deactivate; backup custody/retention; maintenance window; and sign-off that neither email owns Customer-Zero account id=1.
23. Telemetry authorisation: a one-off host RAM/CPU sampler to wire register_host_capacity_probe and make utilisation-% triggers enforceable — explicitly NOT a standing metrics platform (that would need its own ADR).
24. Confirm the beta-agent bundle deployment state on the host (operator check) so the certified PR #336 write is known to be live.
25. Provide a dedicated TEST Telegram chat id (distinct from the live customer channel) for the no-real-trade customer-path probe.
26. Authorise a separate task to RUN the execution test suites and capture machine-readable PASS evidence before any arming.
27. Confirm live prod state (flags, readiness_provider, asn#7/#8 execution_mode, pre-quiesce backup existence) — none verifiable from the repo; the cutover plan's branch points depend on them.

F. GOVERNANCE
28. PM owns advancing any Notion packet/decision lifecycle for these workstreams — not to be self-advanced by engineering.
29. Any density-raise above the conservative capacity band must be a documented ADR/Notion decision after a load test, never an in-passing config change.
30. REMOTEAPP_ISOLATION behavioural sign-off (8004 escape evidence) — decide whether it must close before or after the first hosted demo trade.
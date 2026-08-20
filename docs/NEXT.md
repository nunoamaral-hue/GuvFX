# NEXT — Priorities (keep this list short)

## ▶ Customer Telegram product policy — P0 implementation candidate; NO DEPLOYMENT (2026-08-20)

Local release gates and adversarial review are green (0 HIGH / 0 MEDIUM) on
`feat/customer-telegram-policy-preferences`. Open the focused draft PR, obtain exact-head CI, and merge only
while those gates remain green. Production still runs the pre-policy pilot with both flags ON, so broader beta
notification use is blocked. After merge, a separate explicit release packet must back up, migrate, deploy,
and prove that direct/historical trade-open rows suppress before broadening access. Do not deploy, alter flags,
restart the worker, replay history, or begin another workstream under this packet.

## ▶ Customer Telegram notifications — RECONCILED RC; DARK install gate (2026-08-19)
Draft PR #371 is reconciled onto main `cd05c03f`, DARK/default-OFF, with no execution-plane file changes.
Private numeric-chat binding, no WIMs/global fallback, durable TP/final-outcome messages, full owner MT5 account
number, EN/JA Settings, at-most-once outbox, secret-free heartbeat/binding monitoring, and adversarial proof
(**0 HIGH / 0 MEDIUM**) survive the rebase; fresh Wayond acquisition still seeds 0.01/leg. Next: full gate,
exact-head CI, merge, verified backup, additive migration, backend/frontend DARK deploy, and install the worker
definition stopped. Then STOP. Bot creation, credentials, webhook, flags, worker start, messages, and pilot
remain human-gated by the production activation runbook; never manufacture a trade.

## ▶ ADR-0048 Execution-path readiness + stale reconciler — BUILT, STOP for Sponsor (2026-08-18)
Branch `feat/node2-execpath-readiness` (`fad465f`), NOT pushed / NOT deployed, DARK, no prod mutation. Fixes
the Node-2 root-cause class: separates MT5 runtime / customer authorization / execution-path availability /
order authorization (ADR-0048). New read-only concept-C surface + `WorkerIdentity.last_seen` (mig 0031) +
`node_execution_operational` gate + `scan_execution_path_health`; stale reconciler cancels PENDING→FAILED +
closes plans to release the exposure cascade (refuses CZ + acct 18). Now also integrated into provisioning:
`execution/node_commission.py` + `commission_execution_node` command (dedicated node-aware worker + verify
operational; refuses CZ/legacy/cross-node/stale) and DARK `HOSTED_EXECUTION_PATH_GATE_ENABLED` gating
`allocate_workspace_node` to an execution-operational node (fail-closed). 50 tests + `make check` green. Live
bridge gate + ADR-0047 unchanged.
**ONE bounded next action → the Sponsor (Red / host-gated):** decide whether to (a) run the stale reconciler
in prod for acct 25 (releases the current `account_exposure_exceeded` cascade — a live bug today; places no
order), then (b) COMMISSION node 2 (`commission_execution_node --apply`; infrastructure only — no customer,
no order), and finally (c) AUTHORIZE the customer + start the live worker/bridge for the first-fill per
`docs/operations/hosted-workspace/NODE2_EXECUTION_ACTIVATION_RUNBOOK.md` (arms live dispatch — fires the next
real signal). Follow-up eng (optional): arm `HOSTED_EXECUTION_PATH_GATE_ENABLED` once nodes are commissioned
up-front; add the customer-facing EXECUTION-PATH tier (future UX packet).

## ▶ AJ#7.2.1 Wayond journey normalization — DEPLOYED to production (2026-08-18)
FF-merged → main `4ddb211` (pushed, CI GREEN); backend `8d1177449218` + frontend `374a51d4d5b0` (DARK),
rollback tags `rollback-preAJ721`, pg backup `guvfx_preAJ721_20260818T052636Z.sql.gz`. **No migration.** Live
read-model: acct 25 Wayond renders ONCE (dedup + `is_signal_copy_backed` "Automated" badge, chip Enabled+Manage,
Configure truthful). Wayond execution UNCHANGED (asn 10 `updated_at` identical, 0 new jobs/trades); CZ + acct 18
Golden AFTER==BEFORE byte-identical. **Live verdict = ENABLED AND LISTENING but ORDER EXECUTION BLOCKED** — 6
`PLACE_ORDER` jobs for acct 25 stuck PENDING (node 2, unclaimed), 0 fills.

**ONE bounded next action → the Sponsor (Red / host-gated):** the customer's Wayond is enabled and correctly
promoting signals to orders, but the **Node-2 order bridge is not dispatching them** (`Activate-Node2Bridge.ps1`
never run for support@). Decide whether to activate Node-2 execution for acct 25 — note this WILL immediately
fire the 6 queued XAUUSD PLACE_ORDER jobs as live demo orders, so it is a deliberate execution-authority step,
not part of this UX packet. Do NOT touch the enabled Wayond assignment (id 10), Customer Zero (acct 1) or acct 18
without that explicit decision.

## ▶ SUPERVISED_SINGLE_TENANT_BETA + autonomous arming — DARK (ADR-0044, 2026-08-14)
Sponsor-authorised bounded interim posture so the FIRST end-to-end beta journey can reach EXECUTION_READY
**without** the full `REMOTEAPP_ISOLATION_CERTIFIED` cert — fail-closed, single non-CZ demo tenant alone on a
dedicated ACTIVE non-CZ node (single-tenancy enforced at the physical-host/rdp_host level, at BOTH the
observation trust anchor and the order/arm gate). Emits **no** cert marker. Plus autonomous customer-specific
arming: `confirm_broker_account` activates the intent account, and `auto_arm_runner` (in the cron cycle) arms
`execution_enabled` via the certified path — no per-user operator CLI. All DARK
(`SUPERVISED_SINGLE_TENANT_BETA_ENABLED` + hosted execution/observation flags default OFF). 6-lens adversarial
review → 0 HIGH / 4 MEDIUM all fixed. branch `feat/supervised-single-tenant-beta`, `make check` green.
**ONE bounded next action → the Sponsor/operational** (needs the provisioned non-CZ beta node from the
co-residency-guard action below): deploy `main`+this branch, then on the beta node flip
`SUPERVISED_SINGLE_TENANT_BETA_ENABLED` + `HOSTED_WORKSPACE_ONBOARDING_ENABLED` + `HOSTED_OBSERVATION_SCHEDULER_ENABLED`
+ `HOSTED_MT5_OBSERVATION_ENABLED` + `HOSTED_MT5_EXECUTION_ENABLED` + `HOSTED_MT5_REMOTEAPP_ENABLED` +
`HOSTED_SLOT_PREP_ENABLED` + `HOSTED_HOST_EXECUTOR_ENABLED` + `HOSTED_TENANT_NODE_ISOLATION_ENABLED`, populate
`INTERNAL_PILOT_ARM_APPROVED_EMAILS`, keep global `signal_execution_mode=DEMO`. Full escape-battery cert still
required before a 2nd tenant / co-residency with Customer Zero / public launch.

## ▶ WAYOND BETA ENABLEMENT — multi-user path repo-complete + hardened, DARK (2026-08-14)
Beta Product Enablement (Sponsor). **Root cause "hosted not taking Wayond trades like before"** = the 2026-08-10
`AUTO_SHADOW` quiesce (0 real orders) + the hosted plane has no order-send transport (host-cert-gated) —
AutoTrading/golden is NOT the cause. **Merged (DARK):** #358 single customer-facing Wayond strategy on the
`ti_signals` production pipeline (`ti_signals` is a functional superset of legacy `wayond`; no execution
migration); #359 multi-user tenant isolation hardening (ADR-0020 amendment) — a Phase-2 adversarial verification
found 6 cross-tenant findings (2 HIGH+4 MED), all fixed + re-reviewed: fan-out configured-source never falls to
an unbound catch-all; fan-out implies terminal-node enforcement (no NULL-node PLACE_ORDER/OPEN_TRADE the shared
worker would run on another tenant's terminal); WIN-card resolution account-scoped. The multi-user path
(routing/assignment/isolation/risk/authority/dashboard/rollback) is now repo-complete + tested + DARK
(`MULTI_ACCOUNT_ROUTING_ENABLED` OFF).
**Deferred (infra/design):** node-aware ingest + per-account deal attribution (separate beta host); per-user
WIN-card delivery (Sponsor notification-destination decision — in-app dashboard already per-user).
**Gates to arm Wayond for beta (Sponsor/infra):** host cert `REMOTEAPP_ISOLATION_CERTIFIED` + separate beta host
+ flip `MULTI_ACCOUNT_ROUTING_ENABLED`/`BETA_SELF_SERVE_ARM_ENABLED` + populate `INTERNAL_PILOT_ARM_APPROVED_EMAILS`.
When the beta host exists, enabling Wayond is an operational action, not an engineering stream.

## ▶ Host-level CO-RESIDENCY GUARD — beta ⟂ Customer Zero (ADR-0043 Addendum B, DARK) (2026-08-14)
Sponsor-authorised allocation guard so a **non-Customer-Zero** hosted workspace can **never** be allocated to
Customer Zero's node. New `hosted_workspace/tenant_isolation.py` + flag `HOSTED_TENANT_NODE_ISOLATION_ENABLED`
(**default OFF → zero behaviour change**). Authoritative fail-closed enforcement at the execution-node single
writer `assign_workspace_execution_node` (covers the allocator **and** the `provision_hosted_execution` command);
the allocator additionally skips forbidden nodes with a distinct reason `ALLOC_CZ_NODE_FORBIDDEN`. **Finding it
closed:** the old allocator picked the lowest-id ACTIVE deliverable node — Customer Zero is node id 1 — so beta
users would have landed on CZ's live host *first*. Tests: `hosted_workspace/tests_tenant_isolation.py` (12).
**ONE bounded next action → the Sponsor** (infra, not repo): provision a **separate beta-pool host** distinct from
`100.79.101.19`, add its rdp_host to `HOSTED_BETA_FORBIDDEN_RDP_HOSTS`, flip the flag ON → a supervised beta can
run isolated from Customer Zero *before* `REMOTEAPP_ISOLATION_CERTIFIED` (it does NOT replace that cert; see
`docs/operations/BETA_READINESS_CHECKLIST.md` §1a).

## ▶ STREAM 10E — W^X host behavioural certification PACKAGE (repository deliverable, DARK) (2026-08-14)
STREAM 10D **MERGED to `main` `83400fa` (PR #354)** after four converging adversarial passes → 0 HIGH/0 MEDIUM/0
LOW. STREAM 10E authored the **complete on-host certification package as a repository deliverable — no host was
contacted** (Sponsor packet 2026-08-14): the turnkey runbook
[`STREAM_10E_HOST_CERTIFICATION_RUNBOOK.md`](operations/hosted-workspace/STREAM_10E_HOST_CERTIFICATION_RUNBOOK.md)
(numbered checklist, complete un-shortened 8004 escape battery, evidence collection, pass/fail, rollback decision
tree, CZ before/after, final cert checklist, disposable-host spec, operator-only manual actions) + the PowerShell
payloads `backend/terminal_provisioning/windows/escape_battery/` (tenant attempt runner, admin evidence collector
with a RULE-11 measurement positive control, before/after fingerprint) + the **reducible-half closure now SHIPPED**
in `applocker_policy.tenant_wx_dll_deny_fragment` (per-tenant Dll `Deny(*)`, fail-closed on empty/wildcard/
covers-writable exceptions) applied via `Set-GuvfxAppLockerTenant.ps1 -Mode MergeWx`.
**Certification environment = a SEPARATE DISPOSABLE host; the Customer Zero prod host is the deployment
environment only** — the escape battery is never run against CZ. **`REMOTEAPP_ISOLATION_CERTIFIED` stays WITHHELD**
until the runbook's §10 checklist is genuinely met on the disposable host.
**Operator (Nuno-only) manual actions before the cert can run:** provision the disposable host; enter the
disposable **demo** broker credentials into MT5 (agents never enter credentials); authorize + perform the Enforce
flip + escape-battery run; observe the two operator cases; later authorize the CZ production roll-out.

## ▶ STREAM 10D — Hosted Workspace W^X native-code elimination / G5v2 (ADR-0043, DARK) (2026-08-12)
Closes the ADR-0042 "load-bearing residual" (MQL5 `#import` native code inside signed `terminal64.exe`) + the
portable-copy vector V5. Canonical invariant **TENANT-WRITABLE ⇒ NON-EXECUTABLE**. Repository FOUNDATION landed
DARK behind `HOSTED_WX_ISOLATION_ENABLED` (default OFF): the pure testable brain — `applocker_policy` W^X
(**positive execution allowlist**: a per-tenant-SID `Deny(*)` excepting only the RX `terminal64` + approved
`%SYSTEM32%` session binaries, so a copied signed `terminal64` runs from nowhere — location-irrelevant; MetaEditor
`BinaryName` pin) + `workspace_acl` **G5v2** inverted ACL (v1 kept untouched; foreign-principal-checked) + 83
focused tests — plus host primitives `Set-GuvfxWorkspaceAclV2.ps1` and `Test-GuvfxGoldenMql.ps1`
(ASCII/RULE-9) + ADR-0043.
**Bounded next actions (repository, within the stream):** (1) wire `slot_preparation` to route to G5v2 + emit the
per-tenant W^X deny fragment when the flag is on; (2) wire `Test-GuvfxGoldenMql` into the `install_pool`
Test-GoldenImage gate. **Then the host behavioural cert (Sponsor/host-gated):** CZ before-fingerprint → apply
G5v2 to a disposable tenant → normal-MT5 validation → W^X escape battery (portable-copy, MetaEditor, `common.ini`
mutation, `#import`, writable EXE/DLL/Script, restart persistence) → rollback → CZ after → only then emit
`REMOTEAPP_ISOLATION_CERTIFIED`.


## ▶ STREAM 9E — Live observation bridge + ADR-0041 trust model (DARK, branch, not merged) (2026-08-12)
The live Hosted Workspace observation bridge is repository-complete on `feat/hosted-live-observation-bridge`
(backend `OBSERVE_WORKSPACE` → session-bound observer → certified chain). **ADR-0041** (Sponsor-accepted):
observation is a bounded readiness signal trusted ONLY after RemoteApp isolation is behaviourally certified,
enforced by the fail-closed anchor `HOSTED_REMOTEAPP_ISOLATION_CERTIFIED` (default OFF). LocalSystem corroboration
(process/owner/session/runtime + enumerated remote endpoints; public/private classified in the tested backend)
is kept as defence-in-depth; execution integrity is independent (order-time runtime-identity validation). Two
adversarial reviews + a final confirmatory pass; `make check` green; DARK; Customer Zero untouched.
**Bounded next action (Sponsor/host-gated):** the observation certification is now blocked on its stated
prerequisite — **`REMOTEAPP_ISOLATION_CERTIFIED`**, i.e. the behavioural RemoteApp/AppLocker escape-attempt
certification on the host (incl. the documented `%WINDIR%` LOLBIN residuals). Once that is certified and the
anchor set, drive workspace 5 / account 18 (demo 1302575 @ IS6Technologies-Demo) through the normal scheduler to
`WORKSPACE_READY` and emit `AUTONOMOUS_ONBOARDING_CERTIFIED` + `FIRST_UNASSISTED_USER_CERTIFIED`.

## ▶ Beta Readiness Stream 7C — Hosted signed-executor DAEMON built (DARK, not deployed) (2026-08-11)
The runnable host end of the Stream 5 signed transport now exists under `deploy/hosted-executor/` (ADR-0039):
an authenticated `/hosted/provision` listener → `dispatch`, a real `run_primitive` (reviewed-`.ps1` allow-list +
ParseFile gate + fixed-argv subprocess + password→stdin), a durable SQLite nonce store, host private-key
envelope-open, and a WinSW installer — all mirroring the proven beta agent. `host_protocol`/`host_agent_dispatch`
stay single-source in `backend/hosted_workspace/`; 72 daemon tests run under `make check`. Execution DARK,
Customer Zero untouched, nothing deployed.
**Bounded next action (Sponsor):** authorise the deployment + disposable-host certification packet
(`docs/operations/hosted-workspace/HOSTED_EXECUTOR_DEPLOY_RUNBOOK.md`) — stage the bundle, provision the
`HOSTED_EXECUTOR_*` machine secrets, install the reviewed service, and run the real `prepare_hosted_slot` on ONE
disposable non-CZ slot with the CZ before/after STOP-check. First resolve the two stated residuals: the client
30s read-timeout vs a long `MATERIALISE_RUNTIME` (poll-not-repost), and `VERIFY_SLOT` (unimplemented, off-path).

## ▶ ADR-0035 Operational Readiness — repository-complete (read-only, additive) (2026-08-09)
A purely read-only operational layer in `core/` unifying the existing health sources into a 7-state rollup
(`operational_health`), an authoritative fail-closed Hosted Workspace pre-flight (`hosted_workspace_preflight`),
a flag-disable rollback PLAN that executes nothing (`rollback_plan`), and schema-conformant evidence
collection (`collect_operational_evidence`), plus a staff-only DARK API `GET /api/operational-readiness/`.
No model, no migration, no write path, no host contact, nothing armed. **No fake READY**: dark subsystems
are AWAITING_SPONSOR and the pre-flight is honest that host certification is the standing external gate.
27 focused tests green; ADR-0035 + `docs/operations/operational-readiness/` (README/checklist/DR/rollback).
**Bounded next action (Chief Architect / Sponsor):** unchanged — the platform-wide open gate is the
disposable Windows/RDS host certification (see the Notion Host Certification Record); Operational Readiness
now gives the read-only console + pre-flight to run the moment that host arrives.

## ▶ ADR-0034 Onboarding — integrated onto merged Workspace Delivery, repository-complete (DARK) (2026-08-09)
**Workspace Delivery #316 is MERGED** to `main`; **Onboarding #318 is rebased + integrated** onto it and
merged-ready. The integration: `delivery_readiness` reads the real `delivery_state` (delivery is read-model
only, never order authority); node allocation binds BOTH `execution_node` (routing) and `workspace_node`
(delivery) on the one host as distinct facts; and the separate `owner` FK was **removed** — ownership is the
single immutable `trading_account.user` (simpler + consistent with the delivery authority; migration `0006`
deleted, graph `0001–0005` clean). Password-free invariant preserved. `hosted_workspace` 325 + `execution`/
`billing` 963 tests green; 10-lens adversarial review at the integrated head. Both flags default OFF; no
order, no host action. Markers: `WORKSPACE_DELIVERY_REPOSITORY_ACCEPTED`, `ONBOARDING_REPOSITORY_ACCEPTED`.
**Bounded next action (Sponsor/Chief Architect):** choose the next Hosted Workspace subsystem to build (all
four state-core/execution/delivery/onboarding boundaries are now repository-complete + DARK), OR authorise
the host-certification gate (RDS/RemoteApp/licensing) that every remaining hosted path waits on. No
host/production/arming action without an explicit Sponsor gate.

## ▶ ADR-0034 Workspace Delivery / RemoteApp — MERGED to main (DARK) (2026-08-09)
PR #316 **rebased onto current `main`** (after the Execution Engine capstone #315/#317) — one coherent
subsystem. The migration collision (#316's `hosted_workspace 0003/0004` vs main's) is resolved: the delivery
migration is regenerated as `0005_workspace_delivery_fields` on main's `0004`; `mt5 0009` dependency
repointed; graph linear + deterministic; fresh-DB migrate proven by the suite. **Two-node authority
invariant** documented + pinned (`tests_delivery_node_authority.py`): `execution_node` (order routing) and
`workspace_node` (RemoteApp delivery) are distinct durable facts — delivery reads only `workspace_node`,
execution only `execution_node`, and a RemoteApp connection never grants execution. Single-writer boundary
re-proved; one accidental cross-write (`last_correlation_id`) removed by giving delivery its own
`last_delivery_correlation_id`. Owner-bound mint (no staff bypass), credential only inside the AES token,
DARK-first, fail-closed `DA_*`. `execution`+`hosted_workspace`+`mt5` **1200 tests green**; adversarial review
(8 lenses) recorded on the PR. Host change (RDS/RemoteApp/licensing/AppLocker/NTFS-ACL) stays a Sponsor gate —
`docs/operations/hosted-workspace/WORKSPACE_DELIVERY_HOST_CERTIFICATION.md`. Architecture §9.
**Bounded next action (Chief Architect):** review/merge the rebased #316; then rebase the parked Onboarding
PR #318 (its `owner`-FK migration renumbers after `0005`). No host/production action without a Sponsor gate.

## ▶ ADR-0034 Execution Engine CAPSTONE — repository-complete (DARK); disposable-demo trade is Nuno's (2026-08-08)
On branch `feat/adr0034-execution-capstone` (off main `cc84117`; #315 merged): the durable workspace→node
binding + provisioning contract + routing/claim enforcement close the produce→claim→execute routing capstone
(one workspace → one authorised node; node drift/unbound/mismatch fail closed). The node-aware hosted worker
= the certified bridge in HOSTED mode + a node-aware `WorkerIdentity` (no fork; single-path proof). DARK,
demo-only, default-OFF; the live bridge stays sole order-time gate. +15 capstone tests; contract/arming/
failure-matrix/cert runbook in `docs/operations/hosted-workspace/EXECUTION_ENGINE_CAPSTONE.md`.
**Bounded next action (Nuno, HARD STOP — human trade):** run the disposable-demo certification — DARK setup
via `manage.py provision_hosted_execution`, Nuno logs into a disposable demo broker + **places the single
minimum-volume demo order + deterministic close through the loop** (Claude never trades, even demo), then
Claude verifies provenance/telemetry/blast-radius. Marker until then: `EXECUTION_ENGINE_REPOSITORY_COMPLETE —
HOST_CERT_PENDING`. Then recommend next subsystem (Delivery/RemoteApp vs Onboarding).

## ▶ ADR-0034 Execution Engine — G12 completion DELIVERED (provenance/telemetry/reconcile, DARK); capstone is Sponsor-gated (2026-08-08)
The Execution Engine subsystem is repository-complete on PR #315: the authority spine (routing/arming/
active-broker/authority/pause-resume) plus the G12 provenance layer — job↔workspace + HWX-key persistence,
append-only `HostedWorkspaceExecution` occupancy, `workspace.execution_started/finished` telemetry at
dispatch/complete, the ambiguous-send reconcile driver (alert+quarantine, never re-sends), and the explicit
no-auto-resend retry stance. DARK/demo-only/default-OFF; the live bridge stays sole order-time gate; the M3c
canonical `EXECUTING` enum is deliberately NOT driven (deferred ADR-level change). 1070 tests green.
**Bounded next action (Sponsor, HARD STOP):** authorise the **capstone** — bind a workspace to a
`TerminalNode` at provisioning and run a **node-aware hosted worker** (+ the reconcile driver's live broker
evidence source). The *code* is DARK-safe to build; **arming/running it on a host is the execution-plane
"make it real" step and requires explicit Sponsor authority — it must never be armed on merge.** Do not begin
it without that authorisation. See `docs/architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md` §8.1.

## ▶ ADR-0034 Execution Engine — decisions B/C/D RESOLVED; arming + owner-bound routing DELIVERED (DARK, demo-only) (2026-08-08)
Sponsor resolved the gating decisions (still DARK/demo-only/no-arming): **B** demo-only (real=RED, deferred);
**C** one-workspace→one-process→one-account→one-route, owner-bound; **D** layered explicit arming, every
flag/field defaults FALSE, no auto-arm. Implemented on PR #315's branch: new `HOSTED_MT5_EXECUTION_ENABLED`
flag (OFF) + per-workspace `execution_enabled` field (default False, migration `0003` additive/reversible);
Provider-B readiness now ANDs the full backend arm (+ demo-only hard-reject) with distinct fail-closed reason
codes; new `execution/hosted_routing.py` owner-bound route resolver + server-derived identity + result
taxonomy; a structural no-bypass test pins the mutation-job set. Live order-time bridge gate stays the sole
order authority; no order placed/closed/modified. 20 new tests; `make check` green (backend 3203).
**Bounded next action:** the remaining DARK repository work (a follow-up increment, no arming) — G2 scheduled
observation→persist runner (freshness), G4 account↔worker entitlement at the claim seam, G5 gated provisioning
opt-in, G6 bridge-flag startup assertion, G9 pause/resume producer, G10 hosted idempotency-key — then the
demo-only host certification (`docs/operations/hosted-workspace/EXECUTION_ENGINE_HOST_CERTIFICATION.md`).
See `docs/architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md` §8.

## ▶ (superseded) ADR-0034 Execution Engine — Provider-B enablement DELIVERED (DARK, demo-only); GATED on decisions B/C/D (2026-08-08)
Branch `feat/adr0034-execution-engine` (base fresh main `059b448`, NOT merged). Inventory proved the
order-safety spine already exists + is certified (bridge order-time identity authority + per-job pin +
idempotency = 114 tests; central gate; Provider-B readiness skeleton) — Provider B is *wiring*, not a new
engine. Delivered the two backend gaps: **G1** repointed Provider-B readiness from the legacy cache (which the
M3c writer doesn't maintain → would fail-close forever) to the M3c **canonical** projection; **G3** added the
server-derived per-job identity pin, injected centrally in `ExecutionJob.save()` for every mutation type,
fail-closed. DARK + regression-safe (Provider A / Customer Zero / dark = no-op); order authority stays the
live bridge gate; no order placed/closed/modified. 27 tests + pin mutation; `make check` green (backend 3186).
**Bounded next action (Sponsor decision):** decide **B** (real vs demo-only accounts — RED), **C** (isolation
topology: one-bridge-per-workspace vs shared+entitlement), **D** (per-workspace execution-mode scoping before
any arming). Those unblock the remaining repository work (G2 observation runner, G4 entitlement, G5
provisioning, G9 pause/resume producer, G10 hosted idempotency-key) and the demo-only host certification
(`docs/operations/hosted-workspace/EXECUTION_ENGINE_HOST_CERTIFICATION.md`, prepared/not run). Do NOT arm.
See `docs/architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md` §8.

## ▶ (superseded) ADR-0034 Hosted Workspace — M3c Workspace Core DELIVERED (DARK); Sponsor picks next subsystem (2026-08-08)
The **Workspace Core** subsystem is complete on branch `feat/adr0034-m3c-workspace-core` (DARK, not merged):
the observation chain now flows Agent→Snapshot→Observation→Manager→Decision → **authoritative persistence**
(single `select_for_update` writer, stale-observation + stale-decision protection, idempotent replay, version
monotonicity, append-only `WorkspaceTransition` provenance) → **telemetry** (emitted only from the write seam,
only on a real state change, secret-free, idempotent via the shared dedup key) → **read model + DARK IDOR-safe
API**. Additive/reversible migration `0002`. 29 focused tests + writer mutation-adequacy; `make check` green
(backend 3167); multi-lens adversarial review in the PR. No production caller wires the consumer/writer; API
404s and telemetry no-ops while flags OFF. Persisted `execution_ready` is read-model only — order authority
stays `evaluate_binding`. See `docs/architecture/HOSTED_PERSISTENT_MT5_WORKSPACE.md` §7.
**Bounded next action (Sponsor decision):** choose the next subsystem per the stated preference order —
**Execution Engine** (the human-gated control path that would *read* canonical `EXECUTION_READY` and drive
`EXECUTING`, still never letting an LLM place/size/approve an order) → then Workspace Delivery/RemoteApp →
Onboarding → multi-user. Do **not** auto-start the next subsystem. Optional LOW hardening carried forward:
hard PID-pin + DB-sourced expected binding in the host adapter; retire the diagnostic `_tick_present` check.

## ▶ (superseded) ADR-0034 Hosted Workspace — M-series MERGED to main; disposable-HOST cert is the gate (2026-08-08, DARK)
M1→M3b-2 (PRs #305–#310) MERGED to `main` in dependency order; the whole observation chain
(M1 guarded attach → M3b-2 agent → M3b-1 producer → M3a Manager) is on `main`, `make check` green, nothing
deployed. The M3b-2 **integration-cert entrypoint** (branch `feat/adr0034-m3b2-integration-cert`:
`certification.py` + `manage.py certify_workspace_observation`) composes the chain in a single guarded
attach, secret-free, refusing non-disposable paths — repo-certified (166 tests + adversarial review).
**Bounded next action (yours, HARD STOP):** run the **disposable-host certification** — on a disposable,
broker-connected demo MT5 you have **manually logged in** (Claude never enters credentials), execute
`manage.py certify_workspace_observation` per `docs/operations/hosted-workspace/M3B2_HOST_CERTIFICATION.md`
and capture TESTs A–H + never-launch/never-login/blast-radius before/during/after. Success →
`M3B2_HOST_CERTIFIED — OBSERVATION_CHAIN_PROVEN`. Do NOT begin M4 (telemetry emission). Recommended
milestone after cert: the **Workspace Manager persistence/consumer layer** (persist decisions, drive
transitions) BEFORE telemetry emission. No host mutation, no execution, no production enablement.

## ▶ (superseded) ADR-0034 Hosted Workspace — implementation-led M-series; M3b-2 repo-half done (2026-08-08, DARK)
Implementation-led development (Sponsor-authorised). Bounded, independently-certifiable, DARK increments on
stacked branches: M1 Guarded-Attach (PR #305) → M2a state machine (#306) → M2b telemetry (#307) → M3a
Workspace Manager (#308) → M3b-1 Workspace Observation Producer (#309, CERTIFIED) → **M3b-2 Hosted
Workspace Agent** (branch `feat/adr0034-m3b2-workspace-agent`, stacked on M3b-1). M3b-2 = the read-only
observation pipeline (`agent.py` + reference adapter `agent_host.py`): locate → M1 guarded attach →
read-only reads → `RawWorkspaceSnapshot` → producer → `WorkspaceObservation`. Observe-only, fail-closed,
uses ONLY M1 (injected), never launch/login/order. 145 hosted_workspace tests + AST mutation + 10-lens
adversarial review. **Bounded next action:** open the single focused M3b-2 PR (base = M3b-1 branch), confirm
genuine CI green, and **STOP**. Two Sponsor-owned items block full closure: (1) **disposable-host
certification** — needs a broker-connected disposable MT5 (Nuno; credentialed login is prohibited for the
agent) + live-host execution, per `docs/operations/hosted-workspace/M3B2_HOST_CERTIFICATION.md`; (2)
**merge-sequencing** of M1 #305 + the M3b-1 stack onto one integration base (disjoint unmerged branches). Do
NOT begin M4 (telemetry emission) until M3b-2 is certified and the Sponsor authorises it. No consumer wiring,
no migration, no flag, no host mutation, no execution.

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

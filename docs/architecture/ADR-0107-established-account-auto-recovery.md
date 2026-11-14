# ADR-0107 — Established-account auto-recovery: a fail-closed established-vs-onboarding distinction for automatic terminal relaunch

- **Status:** Accepted (repository-only; additive; migration-free; DARK behind a new default-OFF flag; no
  execution/Copy-Trading authority; no credential handling; no manual snapshot; no production/host mutation by
  this packet).
- **Packet:** GFX-DASHBOARD-ESTABLISHED-ACCOUNT-AUTO-RECOVERY-109.
- **Date:** 2026-11-13.
- **Builds on (never replaces):** the ADR-0044/P0 (2026-09-14) zero-terminal liveness recovery, AJ#6.3 Shape-3
  capability recovery, ADR-0075 monitoring gateway, ADR-0103/0105/0106 dashboard monitoring projection, ADR-0072
  LIVE positions demo gate, Stream D4d/D4e LIVE infrastructure-recovery gating (`HOSTED_LIVE_RECOVERY_ENABLED`).
  The armed liveness/capability passes, the collectors (`capture_equity_snapshots`, `ingest_monitored_deals`)
  and the live-position reads are **unchanged** when the new flag is OFF.

## Context — the gap the production incident (packet-108) proved

Account #50 (owner 29, a LIVE monitoring-only account — IS6Technologies-Live) went stale for ~12 days: its
per-tenant bridge was healthy and reachable, but its MT5 `terminal64` had **exited**, so the identity-firewalled
account read returned HTTP 400 `mt5_init_failed` and the dashboard correctly showed `SNAPSHOT_STALE`. The
packet-108 read-only diagnosis established that **nothing would ever relaunch it automatically**:

- `capability_recovery` returns `skipped_onboarding` for any workspace whose `trading_account.workspace_confirmed_at
  is None` ([capability_recovery.py](../../backend/hosted_workspace/capability_recovery.py)). That gate is correct
  for its purpose (ARMING-capability, strictly above onboarding) but it tarred an established, monitoring-only
  account — which is legitimately unconfirmed — with the same "onboarding" brush. The live scheduler log showed
  `recovery: candidates=12 … skipped_onboarding=12`, hiding 12 established accounts behind an onboarding label.
- `liveness_recovery` — the one mechanism that structurally recovers a terminal from ZERO (observe
  `process_running is False` → relaunch) — only ever considered **ARMED** workspaces
  (`execution_enabled ∧ execution_authorized_at ∧ workspace_confirmed_at`). A monitoring-only account is not armed,
  so it was never a candidate: `liveness: candidates=0 down=0`.

Result: an established, previously-monitored, identity-pinned account whose terminal died had **no automatic
relaunch path**. #50 depended on an external/human relaunch. This is a systemic defect: the same would happen to
any monitoring-only account on the dashboard.

## Decision

Introduce a single, authoritative, **fail-closed** classifier,
`hosted_workspace.recovery_eligibility.established_account_recovery_eligible(ws, account)`, and widen the existing
liveness relaunch pass with an **additive, disjoint candidate class** for established monitoring-only accounts,
behind a new default-OFF flag `HOSTED_ESTABLISHED_RECOVERY_ENABLED` (ANDed with the existing master +
liveness-recovery gates).

### The trust-anchor boundary (the load-bearing design choice)

For a **MONITORING** relaunch — which only starts the account's own `terminal64 /portable` and NEVER logs in,
supplies a credential, arms execution, enables Copy Trading, places an order, or writes a snapshot — the trust
anchor is **prior verified evidence**, not the ADR-0034 customer-confirmation ACK (`workspace_confirmed_at`):

- Confirmation remains the trust anchor for **ARMING** (capability recovery / auto-arm). It is deliberately NOT
  required for a monitoring relaunch, because an account that has previously produced an **identity-matched
  `account_read` equity snapshot under its current identity pin** has already proven the exact `(login, server,
  slot, terminal)` the relaunch would restore. Confirmation would be the wrong gate: a monitoring-only account is
  legitimately never execution-confirmed.
- A genuinely-new / awaiting-first-login account has **no such evidence** and is therefore always ineligible
  (`no_prior_verified_read`), fail-closed. Onboarding stays excluded.

`AccountEquitySnapshot` is the ideal durable anchor: it is append-only/immutable (data.md), and a row is written
**only** from an identity-verified fresh broker read (`capture_account_snapshot`, `verify_identity=True`), so a
row with a non-empty `observed_login` is proof the account previously connected to the correct broker identity.
Requiring at least one such row whose `observed_login` equals the **current** endpoint `expected_login` additionally
fails closed on a since-rebound identity (→ `identity_pin_drift`, human login required).

### The eligibility contract (all mandatory; first failing gate names the reason)

An existing, enabled, owner-bound, non-tombstoned account; not reserved / Customer Zero; a derivable
account-specific slot (`derive_slot(account.id)` — the exact existing relaunch target); the workspace is this
account's (`ws.trading_account_id == account.id`); a live (non-RETIRED) per-tenant `HostedExecutionEndpoint`
owned by this account whose denormalised ownership agrees with the workspace (no collision); a non-empty expected
`(login, server)` identity pin; **prior verified evidence** (an identity-matched `account_read` snapshot under the
current pin); and a recoverable environment — DEMO always, or clean LIVE only when the governed
`HOSTED_LIVE_RECOVERY_ENABLED` infrastructure-recovery gate is on (this flag never widens the LIVE surface on its
own). Any missing/ambiguous/mismatched/indeterminate fact ⇒ ineligible with a stable reason code.

The classifier reads **no credential**, reads/infers **no** execution or Copy-Trading authority, performs **no**
host call, and never mutates anything. The dynamic "is the terminal actually down" fact is NOT in the classifier —
it stays in the liveness pass's **read-only observe** (`process_running is False`, never inferred). Eligibility
answers "is this the KIND of account that may be relaunched"; the observe answers "does it need relaunching now".

### How the two recovery mechanisms now share the classification

- **`liveness_recovery`** gains `_run_established_monitoring_recovery` — a candidate class **disjoint** from the
  armed set (`.exclude(execution_enabled=True, execution_authorized_at__isnull=False)`), so no account is handled
  by both passes. "Monitoring stopped" is judged by **equity-snapshot staleness** (the real signal for a
  bridge-monitored account — `last_decision_at` is only touched by the hosted-observe writer, which does not run
  for these accounts), not `last_decision_at`. For an eligible, snapshot-stale account it runs the SAME read-only
  observe, and on a confirmed-down terminal issues exactly ONE account-scoped `RELAUNCH_TERMINAL`, bounded +
  single-flight (reusing `liveness_recovery_at/count` — no migration; attempt claimed under `select_for_update`
  before any host call; ≤3 attempts, 300 s cooldown; a persistently-failing relaunch backs off and raises ONE
  operator alert — never restart-looped). A terminal that is UP but still not producing reads is surfaced as
  `terminal_up_login_required` (human login/identity review), never relaunched. Ambiguous/unavailable observe ⇒
  fail-closed (no relaunch, no duplicate).
- **`capability_recovery`** now consults the SAME classifier for its onboarding gate. Its act/skip decision is
  **unchanged** — it still HOLDS an unconfirmed account — but an established-but-unconfirmed account is reported
  with the sharper reason `skipped_established` instead of `skipped_onboarding`, removing the packet-108 telemetry
  confusion. Its terminal-relaunch is the established-monitoring liveness pass's job, never capability recovery's.

### Post-relaunch verification (no fabricated readiness)

Recovery only relaunches the terminal. Re-establishing monitoring is re-proven solely by the normal
identity-firewalled collector on the next cycle: it re-verifies identity (`verify_identity=True`) before writing a
snapshot; a mismatched read ⇒ `identity_refused` ⇒ no snapshot ⇒ the account stays stale and the alert stays open.
The operator alert is RESOLVED only once a fresh identity-matched snapshot reappears AND eligibility still holds.
No READY/monitored state is set by recovery.

## Alternatives considered

- **Relax `capability_recovery`'s onboarding gate to relaunch established accounts.** Rejected: capability
  recovery re-asserts autotrading config (`AllowLiveTrading=1`) and pushes toward EXECUTION_READY — that is
  ARMING-capability for a NOT-armed monitoring account, and would pre-establish autotrading a customer has not
  authorised. Monitoring restore belongs to liveness, not capability.
- **Drop the ARMED requirement from the existing liveness query outright.** Rejected: the armed path's alert
  resolution, LIVE §3-authorization re-derivation and re-arm semantics are specific to armed accounts; mixing a
  monitoring-only class into it risks that path. A disjoint candidate class keeps the armed path byte-identical.
- **Use `workspace_confirmed_at` / `proj_account_match` as the trust anchor.** Rejected: confirmation is the wrong
  gate (monitoring-only accounts are never confirmed); `proj_account_match` is an in-memory last-projection that
  can be stale. An immutable identity-matched `account_read` snapshot under the current pin is durable, account-
  scoped and non-forgeable by another tenant.
- **Ship armed (no flag).** Rejected: the liveness/capability recovery passes run in production once deployed;
  every behavior change in this subsystem ships behind a DARK flag so current production behavior is unchanged
  until a separate deploy + Sponsor arming decision.

## Consequences

- With `HOSTED_ESTABLISHED_RECOVERY_ENABLED` **OFF (default)** the liveness pass is byte-identical to before
  (armed candidates only; the established sub-summary is `{"enabled": False}`). No production behavior changes from
  merging this packet.
- When armed (separate Sponsor decision + deploy), a previously-monitored account whose terminal dies self-heals:
  bounded relaunch → next-cycle collector re-proves identity → dashboard freshness returns — the #50 class no
  longer needs a human relaunch. Genuinely-new, awaiting-login, rebound-identity, cross-owner, reserved/CZ,
  disabled, tombstoned, no-endpoint/pin, LIVE-without-the-LIVE-gate and indeterminate-environment accounts remain
  excluded, fail-closed, each with a stable reason code and per-reason telemetry.
- **No migration.** Reuses existing `liveness_recovery_at/count` columns. No new model, no new index.
- Operator visibility: a deduped `reliability.AlertEvent` (component `MT5_TERMINAL`) opens on a stale/down
  established monitoring account and resolves on recovery; the scheduler log gains an `established: …` section.
  No broker secret in any alert (internal account id only).

## Evidence

- New: `backend/hosted_workspace/recovery_eligibility.py`, `backend/hosted_workspace/tests_established_recovery.py`
  (39 tests). Changed: `liveness_recovery.py`, `capability_recovery.py`, `flags.py`,
  `management/commands/run_hosted_observations.py`.
- Local verification (PostgreSQL): new module 39/39; recovery+observation cluster 106/106
  (`tests_liveness_recovery`, `tests_capability_recovery`, `tests_bounded_observation`,
  `tests_hosted_observation_command`, `tests_d4d_live_recovery`, `tests_established_recovery`); account-policy
  governance guard 11/11; enrolment + equity 85/85; full `hosted_workspace` app 1470 tests, 1468 pass, **2
  pre-existing failures unrelated to this change** (`tests_observer_attach.test_cim_positive_and_negative_controls`
  — a bare-terminal PID classifier that imports none of the changed modules; reproduced identically on a clean
  `origin/main` worktree). `manage.py check` clean; `makemigrations --check` introduces **no** migration for the
  touched apps. Mutation discrimination: with the flag OFF the #50-shaped regression reproduces (no relaunch); with
  it ON the account recovers.

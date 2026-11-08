# ADR-0083 — Copy Trading LIVE lifecycle completion: MODIFY / PARTIAL_CLOSE / FULL_CLOSE on a bound follower

- **Status:** Accepted (repository-only / DARK, default-OFF). Arming any per-operation flag, granting the new
  per-operation authority, host/bridge enablement, and any live test are separate Sponsor gates. No production
  deploy, no broker order, no credential handling is part of this packet.
- **Packet:** GFX-COPIER-FULL-LIFECYCLE-FOUNDATION-024.
- **Date:** 2026-11-08.
- **ADR number:** 0083 — the next genuinely free number (0082 = `set_node_capacity`, PR #622; no 0083 exists
  anywhere in the repo at authoring time).
- **Builds on:** ADR-0057 (LIVE copier exact-pair — the LIVE follower OPEN admission `admit_open_live` and the
  `CopierLiveAuthorization` / `CopierLiveKillSwitch` model, the one-shot entry allowance, the order-time identity
  pin). **Composes, unchanged:** ADR-0071 (the per-master-ticket lifecycle state machine `trade_copier.lifecycle`
  + the DARK projection), ADR-0074 (hybrid event ingestion + projection fast/reconcile paths), ADR-0053/0055
  (the DEMO SHADOW producers for MODIFY / PARTIAL_CLOSE / FULL_CLOSE), ADR-0049 (per-tenant live bridge
  transport). Nothing in those ADRs is rewritten.

## Context — the proven gap

Production (`e12a20c9`, reconciled 2026-11-08 — the temporary attestation release `20f5491b` has been retired;
production == canonical main) already contains the full hybrid lifecycle FOUNDATION: an 8-state pure lifecycle
engine (`trade_copier.lifecycle`), event ingestion/projection, SHADOW producers for `admit_open/close/modify/
partial_close`, follower-ticket reconciliation, failure alerts, and a 15-gate fail-closed LIVE follower **OPEN**
admission (`admit_open_live`, ADR-0057). What is missing is the LIVE execution of the *rest* of the lifecycle:
`admit_open_live` line "`requested_op != 'OPEN' → op_not_producible`" and the demo-only producers leave LIVE
`MODIFY` / `PARTIAL_CLOSE` / `FULL_CLOSE` **fail-closed** (`follower_not_demo`). A copied LIVE OPEN is therefore
not auto-managed: a human must modify/close the follower by hand. This packet closes that gap, additively and
DARK.

## Decision

Complete the three LIVE lifecycle operations as **additive, fail-closed, default-OFF** admissions and typed
commands, with a **separately-auditable** authority model and an explicit **close-only containment** policy. The
existing pure lifecycle engine, the DEMO producers, and `admit_open_live` are **not** rewritten.

### 1. Canonical lifecycle vocabulary — reconciled, not replaced

The packet names a 19-state canonical lifecycle (`MASTER_OPEN_OBSERVED … CLOSED`). The repository already owns a
correct, heavily-tested 8-state engine (`LifecycleState`: `OBSERVED_OPEN, ENTRY_PENDING, LINKED, MODIFY_PENDING,
CLOSE_PENDING, ENTRY_FAILED_FINAL, CLOSED, CONTAINED_FAILURE`). Rewriting a proven engine to a literal 19 states
would violate the architecture rule (no whole-subsystem rewrite; preserve behaviour) and risk regressing the
no-late-entry / ordering / idempotency guarantees. **Decision (Amber — documented):** adopt the 19 names as the
**external audit vocabulary** and map them onto the existing engine states plus the new execution-command
sub-states. The granular `*_SUBMITTED` / `*_CONFIRMED` states are the existing `*_PENDING` state + the
reconcile-driven link confirmation; `*_FAILED` / `DIVERGED` / `CONTAINED_FAILURE` already exist as engine
transitions.

| Canonical (audit) state | Realised by |
|---|---|
| MASTER_OPEN_OBSERVED | `OBSERVED_OPEN` |
| ENTRY_INTENT_CREATED / ENTRY_SUBMITTED | `ENTRY_PENDING` (OPEN replication armed) |
| ENTRY_CONFIRMED / FOLLOWER_BOUND | `LINKED` (reconcile MATCHED → immutable `follower_ticket`) |
| ENTRY_FAILED_FINAL | `ENTRY_FAILED_FINAL` (terminal; never re-entered) |
| MODIFICATION_OBSERVED → _SUBMITTED | `MODIFY_PENDING` + `MODIFY_POSITION` ExecutionJob (`admit_modify_live`) |
| MODIFICATION_CONFIRMED | link update `modify_confirmed` → back to `LINKED` |
| MODIFICATION_FAILED | link update `modify_failed` (non-fatal; stays `LINKED`, surfaced) |
| PARTIAL_CLOSE_OBSERVED → _SUBMITTED | `CLOSE_PENDING` + `PARTIAL_CLOSE_TRADE` ExecutionJob (`admit_partial_close_live`) |
| PARTIAL_CLOSE_CONFIRMED | link update `partial_reconciled` → back to `LINKED` |
| FULL_CLOSE_OBSERVED → _SUBMITTED | `CLOSE_PENDING` + `CLOSE_TRADE` ExecutionJob (`admit_full_close_live`) |
| FULL_CLOSE_CONFIRMED / CLOSED | link update `close_confirmed`/`ticket_lost` → `CLOSED` |
| DIVERGED / CONTAINED_FAILURE | `CONTAINED_FAILURE` (close_failed / ambiguous / ticket_lost) |

Invariants preserved by the existing engine (unchanged): one lifecycle per immutable master ticket; one follower
binding per route; monotone per-ticket sequence (stale/out-of-order rejected); close supersedes a queued modify;
terminal states absorb all later events (no resurrection); **entry failure is final — no late entry**.

### 2. Authorization model — separate, auditable lifecycle authority (`CopierLiveAuthorization`)

The ADR-0057 authorization carries the one-shot **entry** allowance (`max_orders`, consume-once). It confers **no**
lifecycle authority. This ADR adds, additive and **default-False**:

- `modify_authority`, `partial_close_authority`, `full_close_authority` — each a separate, auditable grant that a
  lifecycle op requires. A legacy/entry-only authorization manages nothing (fail-closed).
- `close_only_after_expiry` — the per-authorization policy allowing a SAFETY-REDUCING close/partial-close after
  the entry window expires (never a new entry, never a modify, never an increase).
- `correlation_id` — threaded through every decision/command/job for audit.

The authority that governs a bound position is the one the entry was placed under — which becomes `CONSUMED` once
the single entry is used. Lifecycle ops therefore look up an **ACTIVE-or-CONSUMED, non-REVOKED** authorization
(`lifecycle_authorization_for`) and **never consume** the entry allowance. A REVOKED authorization governs nothing.

### 3. Window / kill-switch / close-only containment policy

`_lifecycle_window_ok` + `_kill_switch_policy_ok` (single source of truth, reused everywhere):

- **Within `[valid_from, expires_at)`**: any op whose per-op flag + authority are set is in-window.
- **After expiry**: a `MODIFY` is **never** propagated (it does not reduce exposure). A close/partial-close may
  CONTAIN a bound position **only when BOTH** the authorization's `close_only_after_expiry` **and** the global
  `copier_live_close_only_after_expiry_enabled()` are set — exposure strictly reduced.
- **Kill switch engaged**: ALWAYS blocks every new entry and every `MODIFY`. It blocks a close/partial-close too
  **unless** the explicit, default-OFF `copier_live_contain_on_kill_switch_enabled()` policy is set — the one
  deliberately-chosen exception, permitting ONLY a safety-reducing close/partial-close. **Chosen default: OFF**
  (freeze everything; the operator closes manually). This is the Sponsor-gated policy the packet asked to specify;
  it is DARK and asserted both ways by tests.

No containment path can ever open, increase or reopen exposure.

### 4. Fail-closed LIVE admissions + typed command contract

`trade_copier.live_execution` gains `admit_modify_live` / `admit_partial_close_live` / `admit_full_close_live`
(mirroring `admit_open_live`'s identity/pair/symbol/runtime gates) with three extra invariants OPEN does not need:
they act **only** on an already-LINKED, reconciled follower ticket (`_matched_open_for_close`, never by symbol);
they can **never** increase/reopen exposure (a MODIFY carries only SL/TP; a PARTIAL reduces via `_partial_reduction`
rounded DOWN, rejected if it would be a full close / below min / sub-min remainder; a FULL close closes the one
bound ticket); and they require the **separate per-op authority** + per-op flag. `_sl_weakening_reason` keeps an
SL remove/widen fail-closed. The demo producers (`admit_close/modify/partial_close`) delegate a non-demo follower
here exactly as `admit_open` delegates OPEN — the demo path is unchanged.

The ExecutionJob payload **is** the typed bridge command. `create_live_{modify,full_close,partial_close}_job`
re-lock the authorization and re-apply the policy under the lock (`_relock_live_authz_for_lifecycle`) so a
mid-flight revoke/expiry/kill-switch fails closed, then emit exactly one command of a **fixed typed operation**
(`MODIFY_PROTECTION` → `MODIFY_POSITION`, `PARTIAL_CLOSE` → `PARTIAL_CLOSE_TRADE`, `FULL_CLOSE` → `CLOSE_TRADE`) —
no PLACE_ORDER, no generic arbitrary-order surface. Each command carries: operation id (`hosted_idempotency_key`),
copier operation type, the verified follower binding (ticket), account/server/symbol identity pin, master revision,
authorization reference + correlation id, expiry/deadline, an **expected pre-state fingerprint**, and the requested
post-state. None consumes the entry allowance; duplicates are rejected by `_{modify,close,partial_close}_already_targets`.

### 5. Runtime `MT5_ALLOW_LIVE` boundary (Amber)

A bound position outlives the entry allowance, so the per-runtime live boundary must stay authorized to *manage*
it after the entry is CONSUMED. **Decision (Amber — documented):** add `copier_live_lifecycle_authorized(account)`
to `execution.live_authz` and OR it into `bridge_config.allow_live_for_account` (which sets `MT5_ALLOW_LIVE`),
**alongside — never replacing** the strict entry predicate `copier_live_execution_authorized`. It is fully DARK:
with every per-op arm OFF (the default) it returns False, so the `MT5_ALLOW_LIVE` posture is **unchanged** until a
Sponsor arms a lifecycle op. It returns True only when at least one lifecycle op is *currently permissible* for
that exact follower account (arm + authority + window/containment + kill-switch all pass, identity matched), using
the same single-source policy predicates. It authorizes no order by itself — the per-op admission + order-time
identity pin remain the money-path authority.

## Detection architecture & latency

Unchanged from ADR-0074: event-driven fast path (signed, account-scoped, replay-protected ingestion → projection
→ one authoritative `MasterEvent` sequence) is primary; periodic snapshot reconciliation is the fallback and
**repairs observation state only** — it cannot create a late entry after `ENTRY_FAILED_FINAL`, and may restore a
modify/close intent only for a confirmed existing binding under this authorization policy. **Latency is not
claimed for production here**; the synthetic detection→decision evidence lives in `tests_measurement` (p95 under
the ADR-0074 target on synthetic data). Sub-second production polling remains gated on host-capacity certification.

## Consequences

- LIVE SL/TP modify, partial close and full close become implementable for an explicitly-authorized bound pair —
  the demo-scope limitation noted in packet 023 (manual follower close) is removed **once armed**, which remains a
  separate Sponsor gate.
- The whole feature is DARK/default-OFF. With no flag armed and no per-op authority granted, behaviour — including
  the `MT5_ALLOW_LIVE` posture — is byte-for-byte unchanged.
- Four new arm flags + two policy flags; five additive `CopierLiveAuthorization` fields (migration `0012`, nullable
  defaults, no backfill).

## Limitations / deployment prerequisites (NOT in this packet)

- **Bridge-side enforcement of the pre-state fingerprint and the typed-command deadline is a deployment
  prerequisite.** The command carries the fingerprint/expiry/identity-pin; the bridge already enforces the
  order-time identity pin and wrong-account-fails-closed, but rejecting on pre-state-mismatch / past-deadline at
  the bridge is a separate, reviewed bridge change before any live use.
- **Unknown-broker-outcome handling** (reconcile-before-retry; bounded close retries only where they reduce
  exposure) is specified here and partly realised by the existing reconcile/lifecycle (`close_failed` →
  `CONTAINED_FAILURE`, never auto-retried); the bounded-retry executor is Layer-2b, not armed here.
- **Operator failure-alert surfacing for the new op reasons + the read-only operator view** (which lifecycle step
  failed, redacted identities, whether exposure exists / manual close required, no fabricated zero) is Layer 3,
  additive on the existing `failure_alerts` + operations summary — a follow-up layer.
- No live readiness is claimed on synthetic tests. Promotion to a live test requires the governance path: arm the
  per-op flags, grant the per-op authority, bridge pre-state enforcement, and a Sponsor-present human-operated test.

## Separate future Sponsor gates

Arm `COPIER_LIVE_MODIFY_ENABLED` / `_PARTIAL_CLOSE_ENABLED` / `_FULL_CLOSE_ENABLED`; grant the per-op authority on a
`CopierLiveAuthorization`; decide the `close_only_after_expiry` and `contain_on_kill_switch` policies; land the
bridge pre-state enforcement; then a Sponsor-present live test. None is performed in this packet.

## Adversarial review (Phase — see evidence manifest)

Two independent adversarial lanes (exposure-safety; authority/identity/containment) reviewed the diff against the
invariants above; findings and their disposition are recorded in the evidence manifest
`evidence/manifests/GFX-EVD-COPIER-FULL-LIFECYCLE-FOUNDATION-024.json`.

# Beta Customer Journey — Consolidation & Readiness

Status: **repository engineering, DARK** (no flag armed, no order path, no deploy). This document is the
durable artifact for the *Customer Journey Consolidation and Telegram Readiness* packet. It records the
consolidated journey (WS-A), the customer account status model (WS-E), the Telegram readiness dependency
tree (WS-C/D), the canonical acceptance journey (WS-H), and the navigation / marketplace / product review
(WS-B/F/G/I) with a clear **Fixed vs Deferred** split.

**The browser is the product specification.** §4 is authoritative: the intended customer experience is
defined by what the browser does at each step — screen, API, customer message, backend state — not by any
separate design artefact. Any code change that alters a step must update §4.

**2026-08-05 — browser-product feedback (Sponsor-directed) incorporated.** The previously-deferred product
items are now implemented: `/broker-accounts` is removed as a journey (redirect only); navigation tells one
ordered story (Broker Accounts → Marketplace → Live Trading) via a default-open primary group with each
destination de-duplicated; every marketplace blocked state explains what's missing and the next action, and
the dead affordances (Preview no-op, placeholder metrics strip, empty "Structure" filter) are removed. §5–§7
below reflect this.

Grounded against `main @ dcea807` (the deployed DARK runtime) plus the changes on
`feat/ipr-journey-consolidation`. Verified facts are marked; inferences are called out.

---

## 1. Canonical journey (WS-A) — one broker-account page

`/accounts` is now the **single** customer-facing broker-account page:

- Flag ON (`NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED`) → `/accounts` renders the WP4 broker-connectivity
  journey in place; the per-account detail is `/accounts/[id]`.
- Flag OFF (default / DARK) → `/accounts` renders the legacy content **byte-identically** to before.
- `/broker-accounts` and `/broker-accounts/[id]` **permanently redirect** to `/accounts` and
  `/accounts/[id]` (bookmarks/deep links preserved). The single nav entry points at `/accounts`.
- **Loop-safe**: the canonical page never redirects back; the deprecated routes make no broker API call —
  they only redirect (proven in `broker-accounts/flag-gate.test.tsx` + `accounts/redirect.test.tsx`).

This **reverses** the earlier ADR-0031 "AREA C" redirect (which sent `/accounts → /broker-accounts`).

---

## 2. Customer account status model (WS-E)

**Problem (verified).** An account's true state was scattered across five surfaces that could disagree:
`build_account_status` (`overall` + `lifecycle.phase` + ~10 stage states), `AccountRuntime.state` (14
`RuntimeState` values), `validation_status` (4 values), `BrokerRuntimePause.paused`, and
`is_active`/`is_demo`/`disconnected_at` — plus a per-**user** pilot-approval gate that no status surface
represented. The account page read an *optimistic* RUNNING check while the marketplace read the *strict*
`runtime_ready`, so one account could read "ready" on one page and "not ready" on the other.

**Model.** Exactly **one** customer-facing state per account, chosen by strict precedence so the first
matching condition wins and two conflicting labels can never show:

| State | Meaning (customer) | Wins over |
|---|---|---|
| `CLOSED` | Disconnected; history preserved | everything |
| `NEEDS_ATTENTION` | Something needs the customer / us to act | all below |
| `SETUP_INCOMPLETE` | Missing a prerequisite (demo / active / broker login) | below |
| `PREPARING` | Getting the account ready to trade | CONNECTING/TRADING_ON/READY |
| `CONNECTING` | Runtime up, connecting to broker (only when broker login required) | TRADING_ON/READY |
| `TRADING_ON` | Auto-copy is running | READY |
| `READY` | Ready to enable trading | — |

**Precedence:** `CLOSED > NEEDS_ATTENTION > SETUP_INCOMPLETE > PREPARING/CONNECTING > TRADING_ON > READY`.

**Backend condition → state (every condition mapped exactly once):**

| Backend condition | State |
|---|---|
| `disconnected_at` set (tombstone) | `CLOSED` |
| `validation_status ∈ {CONNECTION_FAILED, TECHNICAL_ERROR}` | `NEEDS_ATTENTION` |
| `BrokerAccountHealth.state != HEALTHY` | `NEEDS_ATTENTION` |
| `BrokerRuntimePause.paused = True` | `NEEDS_ATTENTION` |
| duplicate active AUTO_DEMO arm on another source | `NEEDS_ATTENTION` |
| `AccountRuntime.quarantined = True` | `NEEDS_ATTENTION` |
| `is_demo = False` | `SETUP_INCOMPLETE` (add a demo account) |
| `is_active = False` (no tombstone) | `SETUP_INCOMPLETE` (activate) |
| `password_enc` empty | `SETUP_INCOMPLETE` (add broker login) |
| runtime not `runtime_ready`, broker not required | `PREPARING` |
| runtime not `runtime_ready`, broker required & not connected | `CONNECTING` |
| `runtime_ready`, armed & enabled | `TRADING_ON` |
| `runtime_ready`, not enabled | `READY` |

`RuntimeState` (14) collapse into the above: `NOT_PROVISIONED/REMOVED → SETUP_INCOMPLETE/PREPARING`;
`QUEUED/BLOCKED/PROVISIONING/STARTING/AUTHENTICATING/STOPPING/STOPPED/REPAIRING/DEPROVISIONING/REMOVING →
PREPARING`; `RUNNING → READY/CONNECTING/TRADING_ON` (per broker/arm); `DEGRADED/FAILED/quarantined →
NEEDS_ATTENTION`.

**Single source of truth.** The model is implemented for the copy card by
`strategies.views._signal_copy_readiness(user, account, source)`, which **reuses the exact arm-gate
helpers** (`_account_execution_ready`, `_arm_cohort_approved`, `_arm_extra_containment`), so the panel can
never claim readiness the arm endpoint would refuse. Two known over-optimistic duplicates
(`account_status.rt_running`, `resolve_setup_stage`) are **deliberately left** as-is (tightening them broke
existing tests and stranded the RUNNING-before-first-report window); the arm gate is the authority and
re-checks the strict predicate. Converging those surfaces onto `account_runtime_ready` is the recommended
follow-up (see §7 High-2).

`pilot_arm_approved` is a per-**user** attribute (email allowlist), not account state — it is carried on the
readiness *checklist*, not the account state enum.

---

## 3. Telegram readiness dependency tree (WS-C/D) — why "Not armed", and the fix

**Root cause (verified).** The card badge is driven solely by `/signal-copy/status/`, which returns
`armed:false` because no `AUTO_DEMO / stage=LIVE / demo+active` `StrategyAssignment` on `ti_signals` exists
for the user (arming is the only thing that creates it). So "Not armed" is **true**. It was also **terminal
in the UI**: the arm affordance rendered only when `brokerConnectivityEnabled()`, so a DARK build showed a
passive jargon hint with no path forward.

Dependency chain (each classified against the DARK default):

| Dependency | State (DARK) | Notes |
|---|---|---|
| Marketplace card visible | Satisfied | no flag gate |
| `/signal-copy/status` reachable | Satisfied | truthful `armed:false` |
| Demo + active account | Unsatisfied (typical) | fresh beta account is `is_active=False` |
| Stored broker credentials | Unsatisfied (typical) | `password_enc` empty |
| Runtime ready (`runtime_ready`) | Unsatisfied | per-account provisioning not live in DARK |
| Broker health / runtime pause | Unreachable | evaluated only inside an arm POST |
| AUTO_DEMO assignment (the armed authority) | Unsatisfied | only `signal_copy_arm` creates it |
| `signal_copy_arm` (arm action) | Unreachable / triple-gated | build flag + `BETA_SELF_SERVE_ARM_ENABLED` + cohort allowlist |
| Running (armed + enabled) | Unreachable | needs arm + enable + global execution levers |

**Fix (WS-D).** The opaque "Not armed" hint is replaced by a **readiness panel** — a ✓/✕ checklist
(Demo account · Account active · Broker login added · Account ready to trade · Trading access enabled) plus
**one** customer-safe next action — for the selected demo account, backed by the read-only
`/signal-copy/readiness` endpoint. The panel always renders (even DARK) so the customer sees exactly what is
needed; the Enable-Trading button appears only when the journey is built **and** the backend reports
`can_arm`.

---

## 4. Canonical browser acceptance journey (WS-H) — the product specification

**This section IS the product specification.** It defines the one coherent story the navigation now tells —
**Login → Broker Accounts → Marketplace → Running** — and the single acceptance path a certified pilot user
walks once the environment is armed (a **separate, Sponsor-gated** step — not part of this DARK packet). Per
step: expected screen · API · customer message · backend state.

Navigation reflects this order directly: the primary, default-open **"Get started"** group lists
**Broker Accounts** (`/accounts`) → **Marketplace** (`/strategies/marketplace`) → **Live Trading**
(`/trading/live-trading`), each appearing exactly once (removed from its old thematic group).

| # | Step | Screen | API | Customer message | Backend state |
|---|---|---|---|---|---|
| 1 | Login | `/login` → `/dashboard` | `POST /api/auth/cookie/login/` | — | cookie JWT set |
| 2 | Open Accounts | `/accounts` (canonical) | `GET /api/trading/accounts/` | "Broker accounts" | list |
| 3 | Add demo account | Add form / wizard | `POST …/accounts/add-with-mt5-login/` | "Account connected successfully." | account (demo), intent recorded |
| 4 | Validate | account card / detail | `POST …/test-mt5/` or broker validate | "Connection verified." | `validation_status=VALIDATED` |
| 5 | Runtime ready | setup panel | `GET …/account-status` / readiness | "Your account is ready." | runtime `RUNNING` + report → `runtime_ready` |
| 6 | Marketplace | `/strategies/marketplace` | `GET …/signal-copy/status/` | "Not set up" → readiness panel | `armed:false` |
| 7 | Select account | readiness panel selector | `GET …/signal-copy/readiness/` | checklist ✓✓✓✓✓ · "Your account is ready. Enable trading to start copying." | `state=READY, can_arm=true` |
| 8 | Enable Trading (arm) | Enable button | `POST …/signal-copy/arm/` | "Trading enabled for this account." | AUTO_DEMO/LIVE assignment created |
| 9 | Running | card | `GET …/signal-copy/status/` | "Enabled" | `armed:true, enabled:true` |
| 10 | Operations timeline | `/operations/accounts/[id]` (staff) | operational-event API | — | events projected |
| 11 | Disable (safety stop) | Disable button | `POST …/signal-copy/toggle/ {enabled:false}` | "Strategy disabled." | assignment `is_active=false` (mode/stage untouched) |
| 12 | Disconnect | detail → Disconnect | broker disconnect | "This account is disconnected. Its history is preserved." | tombstone: `is_active=false`, `disconnected_at` set, creds destroyed |
| 13 | History preserved | detail | validation history | history table | records retained |

Guardrails proven in code/tests: arm never fires from the toggle; ARMED/RUNNING shows only from the
backend-confirmed status (never the optimistic arm reply); Disable is always allowed (safety stop);
re-Enable is cohort-gated and refused honestly.

---

## 5. Navigation review (WS-F)

**Fixed (2026-08-05):**
- One canonical destination (`/accounts`) with a single nav entry; the deprecated `/broker-accounts` route
  redirects in; the Telegram card no longer dead-ends (readiness panel).
- **One coherent ordered story.** A new default-open, first **"Get started"** group presents the journey in
  dependency order — **Broker Accounts → Marketplace → Live Trading** — and each destination is removed from
  its old thematic group so it appears exactly once (no duplication). "Broker Accounts" was previously buried
  in a collapsed "Settings" group.
- **Label coherence.** The nav entry, the `/accounts` page H1, and the WP4 broker page all read
  **"Broker Accounts"** (the legacy H1 was "Trading Accounts") — one term for one destination, distinct from
  the billing "Account" group.

**Deferred (still recommended):**
- No top-level Home/Dashboard entry (the logo links to `/dashboard`; Overview is under a collapsed group).
- "Live Trading" is demo-only and self-declares it executes nothing — its page copy vs demo-trade buttons is
  a separate content fix; "Terminal Access" empty state still has no CTA for beta users.

---

## 6. Marketplace behaviour (WS-G)

**Fixed:** the readiness panel (Status + guidance always meaningful); the five arm rejections that
collapsed to a generic "try again" (`not_pilot_approved`, `broker_validation_unhealthy`, `runtime_paused`,
`duplicate_active_assignment`, `source_single_tenant`) now map to their own customer-safe copy; a
still-armed Enable refused by the cohort gate is explained honestly; the account selector offers **only demo
accounts**; the raw `ENTITLEMENT_RESTRICTED` slug is no longer shown on Assign; operator jargon removed from
card copy; the signal-copy card summary no longer leaks the execution pipeline.

**Fixed (2026-08-05 — every blocked state explains what's missing + the next action):**
- **Generic template cards.** When the customer has no eligible account, the card now says *"You'll need a
  trading account first"* with a **Go to Accounts** link, instead of a silently-disabled Assign button; when
  accounts exist but none is selected, a hint explains the block. The plan/entitlement denial shows plain
  guidance (no raw slug).
- **Dead affordances removed.** The permanent-no-op **Preview** button (every card) and the hardcoded
  **"Preview metrics unavailable"** strip are gone; the empty **"Structure"** category filter (zero seed
  members → forced empty state) is removed.

**Deferred (still recommended):**
- Timeframes rendered twice per card — show once.
- Seed strategy identities leak internal shorthand/personal names (Wayond, Ali, ALTS, SCE, TBP, TC1). The
  `mp-010` card name is intentionally left (the arm flow keys the created Strategy off it; renaming needs a
  coordinated migration + Sponsor sign-off).
- The signal-copy *badge* still defaults to "Not set up" on a status-fetch error (the new readiness panel
  already distinguishes loading / unavailable) — recommend the same treatment for the badge.

---

## 7. Product critique — ranked, with status (WS-I)

| Rank | Finding | Status |
|---|---|---|
| Critical | Signal-copy card leaked source/pipeline/codename | **Fixed** (summary + execution copy; name deferred w/ note) |
| High | Accounts errors/success leaked "Windows agent/backend/EA/MT5 session" | **Fixed** |
| High | Ambiguous-copy error leaked the data model ("armed assignment bound to this source") | **Fixed** |
| High | Assign leaked raw `ENTITLEMENT_RESTRICTED` slug | **Fixed** |
| High | Marketplace "deploy" vs Assign success wording | **Fixed** (assign copy) |
| High-2 | Optimistic RUNNING vs strict `runtime_ready` divergence across pages | **Deferred** — converge on `account_runtime_ready` |
| High | Internal codenames / personal names as strategy identity | **Deferred** (product/marketing) |
| Medium | "dedicated runtime"/"trading terminal is up" in setup panel | **Fixed** |
| Medium | Signal-copy operator status words ("Not armed", "arming", "auto-demo", "gated") | **Fixed** |
| Medium | Assign failure "endpoint"/"server" developer wording | **Fixed** |
| Medium | Dead Preview button + placeholder metrics strip | **Fixed** (removed) |
| Medium | "Structure" dead filter; three names for one destination | **Fixed** (filter removed; nav/H1 aligned to "Broker Accounts") |
| Medium | Silently-disabled Assign with no explanation | **Fixed** (blocked-state message + link) |
| Medium | Navigation tells no coherent story | **Fixed** (ordered "Get started" group; de-duplicated) |
| Medium | Timeframes rendered twice per card | **Deferred** (product) |
| Low | Broad nav exposes non-functional areas; single-account deactivate tooltip; emoji | **Deferred** (emoji removed from the reworded strings) |

---

## 8. Browser journey status (WS-B) — Login → Running

Nine stages, DARK build. **Repo-fixable dead-ends are fixed** (Telegram card guidance, customer-safe copy,
canonical accounts). The remaining blockers are **environment/Sponsor-gated**, out of this DARK packet:

| Stage | Status | Owner / gate |
|---|---|---|
| 1 Login | COMPLETE | — |
| 2 Onboarding gate (email verify) | CONFUSING | SMTP for the pilot cohort (env) |
| 3 Reach Accounts | COMPLETE (canonical `/accounts`) | — |
| 4 Add broker account | Clearer copy | deferred-validation model (env) |
| 5 Runtime provisioning | BLOCKED | `BETA_RUNTIMES_ENABLED` on a certified host + broker-login ACL (Sponsor) |
| 6 Find Telegram strategy | COMPLETE | — |
| 7 Enable affordance | Guided (readiness panel) | Enable button needs build flag + `can_arm` |
| 8 Arm grants authority | Contained | `BETA_SELF_SERVE_ARM_ENABLED` + `INTERNAL_PILOT_ARM_APPROVED_EMAILS` (Sponsor) |
| 9 Running (auto-copy) | BLOCKED | global AUTO_DEMO levers + multi-account routing (WP6B, Sponsor) |

**Net:** the browser journey is coherent and honest end-to-end at the repository level. Reaching a *Running*
Telegram strategy for a new pilot customer still requires the separate, Sponsor-gated arming sequence
(certified host + flags + cohort allowlist + WP6B multi-tenant execution) — none of which is performed by
this DARK packet.

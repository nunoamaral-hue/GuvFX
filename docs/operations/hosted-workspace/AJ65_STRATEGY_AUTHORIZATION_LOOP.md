# AJ#6.5 — Final strategy-selection / explicit-authorization journey loop (Option B)

**Status:** implemented + tested (frontend-only). Certification against support@/account 24 pending deploy.
**Surface:** `frontend/src/components/marketplace/SignalCopyReadiness.tsx` (+ its test bar),
`frontend/src/app/(app)/strategies/marketplace/page.tsx`, `frontend/src/lib/i18n.ts`. No backend change, no
migration. The hosted onboarding side (`HostedWorkspaceJourney.tsx`) is **unchanged**.

## 1. The defect (P0, proven in prod 2026-08-17)

An EXECUTION_READY hosted customer had **no forward path** to enable the Wayond strategy — a reciprocal loop:

```
Marketplace (Wayond card, all-green)  --"Continue"-->  /onboarding/hosted
/onboarding/hosted ("Workspace Ready") --"Choose Strategy"--> /strategies/marketplace  --> (loop)
```

**Root cause (code-proven).** In `SignalCopyReadiness.tsx` the card linked to `/onboarding/hosted` whenever
`readiness.can_arm && !armUiEnabled` (the "Continue"/`navOpenWorkspace` leg) and, when `!can_arm`, via
`NEXT_NAV['preparing'|'connecting'|'add_credentials'] → /onboarding/hosted` (`navFinishWorkspace`).
`armUiEnabled = brokerConnectivityEnabled() = NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED` is a **build-time flag,
OFF in production** — so the marketplace could never surface a live arm control and instead bounced the ready
customer to onboarding, which bounced them back. Neither surface owned the forward action. The hosted
onboarding side (`HostedWorkspaceJourney` "Choose Strategy" → `/strategies/marketplace`) is correct and stays.

**Live confirmation (support@/account 24, read-only):** `can_arm=True`, `next_action=ready_enable`, checklist
all ✓, `armUiEnabled=False` → the `navOpenWorkspace` bounce — exactly the loop.

## 2. Product contract (must never collapse)

`MT5 capability (EXECUTION_READY/trade_allowed) ≠ customer authorization (execution_authorized_at) ≠ strategy
arm (StrategyAssignment) ≠ order authorization (order-time gate)`. Sponsor preference **Option B**:
strategy-selection / Wayond **owns** the explicit "Enable automated trading" authorization step.

## 3. Backend contract the fix relies on (verified, not re-derived)

- For a **hosted** account, signal-copy `can_arm` **already requires** ADR-0047 authorization: gate 7
  (`_account_execution_ready`) delegates to `execution.readiness.evaluate_readiness`, which fail-closes on both
  `execution_enabled != True` (`RW_EXECUTION_DISABLED`) and `execution_authorized_at is None`
  (`RW_EXECUTION_NOT_AUTHORIZED`). `can_arm` also folds in `BETA_SELF_SERVE_ARM_ENABLED` + the cohort gate — so
  **`can_arm=True` means the backend has already committed to accept the arm.**
- An **order** for a hosted account needs the **conjunction** of (a) an active `StrategyAssignment`
  (auto-router target) **and** (b) ADR-0047 authorized+enabled (readiness gate at promotion **and**
  `ExecutionJob.save`). Neither alone fires an order. `signal-copy/arm` creates only a `StrategyAssignment`;
  `authorize-execution` only sets `execution_authorized_at` (+arms the workspace) — **no assignment, no order.**
- Read-model: `execution_ready = (state==EXECUTION_READY)`; `execution_authorized = (execution_authorized_at
  is not None)`; `can_enable_automated_trading = EXECUTION_READY ∧ confirmed ∧ matched ∧ execution_authorized_at
  is None`. `can_enable_automated_trading` and `execution_authorized` are **mutually exclusive** by construction.

## 4. The fix (Option B — a three-case state machine keyed on the ONBOARDING-COMPLETE threshold)

`SignalCopyReadiness` gains optional hosted-journey props (`hostedComplete`, `hostedAuthorized`,
`canEnableAutomatedTrading`, `authorizing`, `onAuthorize`). The critical design point (see §4a) is that
`hostedComplete = strategy_eligible` (journey phase `WORKSPACE_READY` = connected+matched+confirmed) — the **same
threshold** at which hosted onboarding's "Choose Strategy" sends the customer to the marketplace. Whenever
`hostedComplete` is true the card **OWNS** the forward path and NEVER bounces to `/onboarding/hosted`; the
specific control is chosen by the execution tier (the three are mutually exclusive):

- **CASE A** — `canEnableAutomatedTrading` (EXECUTION_READY, not yet authorized): the ADR-0047 **"Enable
  automated trading"** button (`onAuthorize`) + the authorize hint. On success the page re-reads the journey;
  the skip-mount reload effect re-fetches readiness (so `can_arm` flips) and the card advances to CASE B.
- **CASE B** — `hostedAuthorized` (authorized): the live **"Enable this strategy"** arm button when
  `readiness.can_arm` — surfaced **regardless of `armUiEnabled`**, because `can_arm` already folds in the
  self-serve + ADR-0047 gates. If `!can_arm` (transient NEEDS_ATTENTION), the goal button disabled.
- **CASE WAITING** — onboarding-complete but not yet executable (e.g. AutoTrading off / market closed →
  CONNECTED-not-EXECUTION_READY): a reassurance line + a **disabled** "Enable automated trading" button. No
  bounce — this is the band that reintroduced the loop.

Outside `hostedComplete` (non-hosted / customer still onboarding) the **pre-AJ6.5 legacy branches are
byte-preserved** (including the `/onboarding/hosted` link, which is *correct* routing for a customer who
genuinely must finish onboarding — the onboarding page then shows the Confirm/login step, not "Choose Strategy",
so there is no loop). Fail-closed: a null/empty/partial journey degrades to legacy.

The marketplace page fetches the hosted journey (`fetchJourney`, fail-closed to `null` on 404/error), passes
the props, and adds `handleAuthorizeExecution` (calls `authorizeExecution`, re-reads the journey; creates **no
assignment and no order**). A monotonic generation guard in `load()` ensures out-of-order readiness fetches
cannot strand the card. Side B (hosted onboarding "Choose Strategy" → `/strategies/marketplace`) is untouched.

## 4a. Threshold alignment (the adversarial-review HIGH, found + fixed)

The first adversarial review (11 agents) confirmed a **HIGH**: an earlier cut keyed ownership on
`execution_ready` (canonical `EXECUTION_READY`), but side B fires at the weaker `WORKSPACE_READY` phase — so a
confirmed customer resting at CONNECTED-not-EXECUTION_READY (the *normal* freshly-onboarded state: AutoTrading
off / weekend / market closed) still bounced, re-forming the loop. Fixed by keying ownership on the SAME
onboarding-complete signal side B uses (`strategy_eligible`) and adding CASE WAITING. Two LOWs were also
addressed: EXECUTION_READY-but-unconfirmed now correctly falls to the legacy Confirm route (escapable, not a
loop); the fetch race is closed by the generation guard.

## 5. Tests

`SignalCopyReadiness.test.tsx` — 6 pre-existing (legacy behaviour preserved) + 6 new: CASE A shows authorize,
no bounce, `onAuthorize` not `onArm`; CASE B (armUiEnabled OFF — the prod condition) shows the live arm, no
bounce, `onArm` not `onAuthorize`; CASE B `!can_arm` disabled + no bounce; **CASE WAITING** (onboarding-complete
but not EXECUTION_READY — the exact HIGH band) shows the reassurance + disabled control + no bounce; CASE A→B
transition re-fetches and swaps authorize→arm; non-hosted caller unchanged (legacy DARK bounce still renders).
Full frontend suite: 211/211. Also fixed a pre-existing red inherited from AJ#6.4 —
`tests_host_primitive_runner.py` asserted `RESERVED_ACCOUNT_IDS = @(1)` while the deployed PS1 (and the
account-18 SACRED invariant) is `@(1, 18)`. `make check`, lint (0 errors), build: green.

## 6. Certification — PASS (2026-08-17, support@/account 24, current authorized state)

Deployed frontend only: FF-merged to `main` (`cc7bde1` test fix → `dbe42bb` AJ#6.5), rebuilt image
`dee99b7f` (rollback `guvfx-prod-guvfx-frontend:rollback-preAJ65` = `cf0536f41f99`), `NEXT_PUBLIC_BROKER_
CONNECTIVITY_ENABLED` / `NEXT_PUBLIC_OPERATIONS_ENABLED` **stay DARK** (not passed — the fix does not depend
on them). The served bundle contains the fix ("Enable this strategy", "finishing getting ready for automated
trading").

**Live journey (support@/account 24), current authorized baseline** (Nuno performed the ADR-0047 authorization
himself at 15:10 after the AJ#6.4 stop; per his direction the CURRENT authorized+armed state is the acceptance
baseline): `phase=WORKSPACE_READY`, `strategy_eligible=True`, `execution_ready=True`, `execution_authorized=True`,
`execution_armed=True`, `can_enable_automated_trading=False`. Wayond readiness (`mp-010`): `state=READY`,
`can_arm=True`, `armed=False`. → deterministic FE case: **owned=True, caseA=False, caseB=True → the card renders
the live "Enable this strategy" arm control, NOT a `/onboarding/hosted` bounce.** The reciprocal loop is gone.

**Safety invariants held:** Wayond arm = FALSE (active AUTO_DEMO/LIVE/ti_signals assignment = 0); ExecutionJobs = 0;
Trades = 0. The customer-only Wayond arm click was **NOT** performed. **Customer Zero Golden byte-identical**
(`b57182b4…`, AFTER==BEFORE); **account 18 untouched** (WAITING_FOR_LOGIN, `capability_recovery_count=0`).

**Disclosed customer activity (not this packet's doing, not order-producing):** support@ (owner 27) also assigned a
GENERIC strategy `London Session Box Breakout` (id=9, `exec_mode=MANUAL`, `stage=TEST`, `signal_source=''`) at 15:37
via the normal (non-signal-copy) Assign flow — a surface AJ#6.5 does not touch. MANUAL/TEST does not auto-route, so
it yields 0 jobs / 0 orders and does not arm Wayond; it is orthogonal to the loop fix and left as-is.

**STOP:** the marketplace Wayond card now presents the un-clicked "Enable this strategy" control for support@.
The customer-only strategy-arm click belongs to Nuno.

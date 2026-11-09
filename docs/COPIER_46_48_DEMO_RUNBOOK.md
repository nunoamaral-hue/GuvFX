# #46 → #48 XAUUSD copy-trading demo runbook (PREPARATION ONLY — DO NOT EXECUTE)

**Status:** preparation only. This runbook is NOT armed and MUST NOT be executed without a separate, explicit,
Sponsor-present GO. The live path is DARK and default-OFF. No `CopierLiveAuthorization` exists, no route is
active, no flag is armed, no order has been placed. Claude does NOT arm or execute any step below.

**Governed route (sole):** master **#46** TradersWay / Tradersway-Live (acct 55442, LIVE) → follower **#48**
IS6FX / IS6Technologies-Live (acct 51763831, LIVE). Symbol: **XAUUSD** (the single governed admitted symbol).
Intended human test size: **0.01 lot**.

Legend — **[SPONSOR]** a human must perform it; **[CLAUDE/OBSERVE]** Claude may observe/verify read-only.

## A. Preflight (read-only) — **[CLAUDE/OBSERVE]**
1. Verify production revision carries the transport (ADR-0087) and all copier/execution/transport/admission
   flags are OFF; `CopierLiveAuthorization` active = 0; zero queued/running copier jobs; zero broker orders.
2. Verify #46 and #48 immutable identity: account id, broker login, server, workspace, host, terminal and
   bridge identity; the governed XAUUSD symbol map; both accounts' per-tenant endpoints READY.
3. Verify both terminals logged in + healthy; bridge, observer and watchdog healthy and identity-attested;
   XAUUSD visible + `trade_mode=FULL` on both; quotes fresh + market open; follower volume min/step accept 0.01;
   stops/freeze + contract metadata captured; free margin sufficient; **no conflicting XAUUSD position or
   pending order on either account**; no stale lifecycle record / replay item / queued copier job.
   **If any gate fails → STOP; do not arm.**

## B. Arm the minimum controls — **[SPONSOR]** (each a deliberate, logged change; shortest-lived possible)
4. Activate the #46→#48 route (activate #46 and #48) and confirm their per-tenant bridges are READY.
5. Create a **short-lived** `CopierLiveAuthorization` for exactly this route (modify + full-close authority as
   needed), with a tight expiry.
6. On the follower bridge host set the exact-account admission env (`COPIER_LIVE_MUTATION_ADMIT_ENABLED=1`,
   `MT5_ALLOW_LIVE=1`, a fresh short `MT5_LIVE_EXPIRY`, `MT5_EXPECTED_LOGIN`/`MT5_EXPECTED_SERVER` = #48,
   `COPIER_LIVE_ADMIT_SYMBOL=XAUUSD`) and the backend arm flags (`COPIER_ENABLED`, `COPIER_EXECUTE_ENABLED`,
   `COPIER_LIVE_EXECUTE_ENABLED`, `COPIER_MUTATION_DISPATCH_ENABLED`, `COPIER_MUTATION_TRANSPORT_ENABLED`,
   `COPIER_LIFECYCLE_GUARD_ENABLED`). Confirm the kill switch is NOT engaged.
7. **[CLAUDE/OBSERVE]** Re-verify the armed state is scoped to #46→#48 + XAUUSD only and nothing else changed.

## C. The demo sequence
8.  **[SPONSOR]** Open the master XAUUSD trade on #46 (0.01 lot) — optionally without SL/TP initially.
9.  **[CLAUDE/OBSERVE]** Observe + correlate the follower OPEN on #48 (ticket, volume, symbol, latency, deviation).
10. **[SPONSOR]** Add SL and TP to the master #46.
11. **[CLAUDE/OBSERVE]** Observe the follower MODIFY on #48 (SL/TP reflected).
12. **[SPONSOR]** Change SL/TP again on #46.
13. **[CLAUDE/OBSERVE]** Observe the second follower MODIFY.
14. **[SPONSOR]** (optional) Partially close #46 → **[CLAUDE/OBSERVE]** observe the follower partial close.
15. **[SPONSOR]** Close the master #46.
16. **[CLAUDE/OBSERVE]** Observe the follower CLOSE on #48 (position flat).

## D. Return to OFF (immediately) — **[SPONSOR]**
17. Disarm every flag + env knob set in B, revoke/expire the `CopierLiveAuthorization`, and deactivate the route.
18. **[CLAUDE/OBSERVE]** Verify execution + all copier controls OFF; `CopierLiveAuthorization` active = 0; zero
    residual copier jobs; zero open positions attributable to the demo on #48; zero new broker orders beyond the
    demo's intended ones.
19. **[CLAUDE/OBSERVE]** Record latency, deviation, per-op outcomes and any failures (every failure is recorded
    + alerted; a failed OPEN is never recovered late).

## E. Containment (if anything is wrong at any step) — **[SPONSOR]**
- Engage the kill switch and disarm the arm flags/env immediately.
- Manually close any follower #48 position in the MT5 terminal (manual-close containment); the system never
  re-opens a failed/late entry.
- Capture the state for review before re-attempting.

**No step here is armed or executed by Claude. The demo proceeds only under a separate Sponsor-present GO.**

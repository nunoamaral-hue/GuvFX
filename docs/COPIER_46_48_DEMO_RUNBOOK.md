# #46 → #48 XAUUSD demo runbook — authoritative, deterministic (2026-11-10)

**Status: preparation only. NOT armed.** Executed only under a separate, explicit, Sponsor-present GO. The
live path is DARK and default-OFF. Every step is labelled:

- **[HUMAN]** — the Sponsor performs it (all MT5 actions + all arming/authorization commands).
- **[READ-ONLY AGENT]** — Claude may run it; it mutates nothing (reads/verifies/observes).
- **[PROD-OPERATOR]** — a separately-authorized production operator runs the signed host command.

**Governed route (sole):** master **#46** TradersWay / `Tradersway-Live` (55442, LIVE) → follower **#48** IS6FX /
`IS6Technologies-Live` (51763831, LIVE). Symbol **XAUUSD → XAUUSD.std**. Size **0.01 lot**, **1 order**, **1
position**. Authorization window **≤ 15 min**. Authorities: **MODIFY + FULL_CLOSE only** (no partial, no new
entry after expiry). No credentials appear here.

> **ADR-0094 update (real-time + arming corrections).** The 2026-11-12 demo exposed two facts corrected here:
> (1) the ~2-min observer is **reconciliation only**, not the copy trigger — the primary path is now the signed
> ingestion endpoint driving `dispatch_master_realtime` synchronously when `COPIER_IMMEDIATE_DISPATCH_ENABLED`
> is armed (target ≤1s, ≤2s ceiling); and (2) the arming flag stack previously omitted the sub-gates
> **`COPIER_OBSERVER_ENABLED`, `COPIER_REPLICATION_ENABLED`, `COPIER_LIFECYCLE_ENABLED`** (and, for the
> real-time path, `COPIER_EVENT_PROJECTION_ENABLED`, `COPIER_RECONCILE_ENABLED`, `COPIER_IMMEDIATE_DISPATCH_ENABLED`),
> so observe/replicate/lifecycle silently no-op'd. Use the authoritative per-worker stack in
> `docs/COPIER_LIVE_WORKER_SEQUENCE.md`. The governed MODIFY/CLOSE still needs the **route-scoped** follower-read
> authority (`COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED` + an active authorization for #48) — a separate
> Sponsor-gated enablement; until then #48's `/mt5/positions` returns `account_not_demo` and a copied LIVE OPEN
> needs manual close. All of this remains DARK/default-OFF; this runbook stays preparation-only.

## Timing / containment limits
- Authorization window ≤ 15 min; disarm immediately after step 13 regardless of time left.
- Per-step observe wait: **≤ 90 s** after each human MT5 action (observer cadence is 2 min — run the live-path
  workers on demand immediately after each action rather than waiting for a cron).
- Containment triggers: wrong account/symbol/side/volume; duplicate follower position; UNKNOWN_OUTCOME /
  PRE_STATE_MISMATCH / DEADLINE_EXCEEDED; identity/authorization/expiry drift; bridge/observer/watchdog
  degradation; any extra job/order. **No automatic retry or late recovery is ever permitted.**

## Authoritative ordered sequence
1. **[READ-ONLY AGENT]** `copier_live_demo_preflight --master-account-id 46 --follower-account-id 48 --source-symbol XAUUSD --follower-symbol XAUUSD.std` → require `verdict=READY_TO_ARM` for the backend gates; resolve every `HOST_PROBE_REQUIRED` item out of band ([PROD-OPERATOR] confirms bridge/watchdog health, host admission absent, #48 position-clean).
2. **[HUMAN]** Create a fresh 15-min authorization (revoke any stale ACTIVE row first — see step 16 command):
   `grant_copier_live_authorization --user-email support@guvfx.com --master-account-id 46 --follower-account-id 48 --source-symbol XAUUSD --follower-symbol XAUUSD.std --max-volume 0.01 --max-concurrent 1 --max-orders 1 --valid-minutes 15 --approved-by <STAFF_EMAIL> --reason "..." --modify-authority --full-close-authority`
3. **[READ-ONLY AGENT]** Independent DB read-back: exact identities/scope/window/watermark/authorities, staff approver, `status=ACTIVE`, unexpired, sole ACTIVE for the pair.
4. **[HUMAN]** Backend worker arming — run the live-path workers with the flag stack injected per invocation
   (`COPIER_ENABLED COPIER_EXECUTE_ENABLED COPIER_LIVE_EXECUTE_ENABLED HOSTED_LIVE_EXECUTION_ENABLED COPIER_MUTATION_DISPATCH_ENABLED COPIER_MUTATION_TRANSPORT_ENABLED COPIER_LIFECYCLE_GUARD_ENABLED COPIER_LIVE_MODIFY_ENABLED COPIER_LIVE_FULL_CLOSE_ENABLED`): `run_copier_observer`, `run_copier_replication`, `run_copier_execute`, `run_copier_lifecycle`, `run_copier_execute_modifies`, `run_copier_execute_closes`, `run_copier_mutation_dispatch` (see `COPIER_LIVE_WORKER_SEQUENCE.md`).
5. **[PROD-OPERATOR]** Signed host-admission **dry-run** (with the flags injected): `provision_authorized_live_copier_bridge --account-id 48 --authorization-id <N>` → require `gates_ok=true`, `env_will_be_live=true`.
6. **[PROD-OPERATOR]** Signed host-admission **commit**: same command **+ `--commit`** → sets `MT5_ALLOW_LIVE` + exact-account/route/symbol/time admission env on #48's bridge + restart.
7. **[READ-ONLY AGENT]** Read-back the exact #46→#48 XAUUSD boundary: authorization ACTIVE+unexpired; the 5 gates TRUE; host admission exact-account/route/symbol/time-pinned; **0 jobs; 0 broker tickets**; kill switch clear; sole route.
8. **[HUMAN]** Open exactly **one 0.01 XAUUSD** position on #46 (SL/TP optional).
9. **[READ-ONLY AGENT]** Observe exactly **one** follower OPEN on #48: master/follower tickets, volume, latency, deviation. On any trigger → containment (step 14).
10. **[HUMAN]** Add or change SL/TP on #46.
11. **[READ-ONLY AGENT]** Observe the follower MODIFY on #48: master/follower revision, latency, resulting SL/TP, any broker normalization (report it exactly — never claim equality when values differ).
12. **[HUMAN]** Close the #46 master position.
13. **[READ-ONLY AGENT]** Observe exactly one follower FULL_CLOSE on #48; both flat; no residual; no pending mutation; no retry scheduled.
14. **[HUMAN/PROD-OPERATOR]** Immediate return to execution OFF: stop invoking the live-path workers (shadow crons continue).
15. **[PROD-OPERATOR]** Signed host-admission OFF: `restore_copier_bridge_execution_off --account-id 48` (dry-run) then `--commit` → removes `MT5_ALLOW_LIVE` + admission env, restarts #48's bridge to DARK.
16. **[HUMAN]** Revoke the authorization: `revoke_copier_live_authorization --authorization-id <N> --owner-email support@guvfx.com --master-account-id 46 --follower-account-id 48 --revoked-by <STAFF_EMAIL> --reason "demo complete"` (or `reconcile_expired_copier_authorizations --commit` for an expired one).
17. **[READ-ONLY AGENT]** Final OFF read-back: all copier flags OFF; host admission absent (bridge DARK); 0 ACTIVE authorizations; `LiveExecutionAuthorization` 0; kill switch clear; 0 queued/running copier jobs; no residual #48 position; route monitoring-only; sibling accounts/routes unchanged.

## Failure classifications
`OPEN_REJECTED/TIMEOUT` → record FAILED, **never place later** (position abandoned); master-open/follower-not is an accepted asymmetric outcome. `MODIFY_UNKNOWN` → record, no retry without a separate human decision. `CLOSE_FAILED/TIMEOUT` → alert + **[HUMAN] manual close of #48 in MT5** (contingency). `IDENTITY/ROUTE/SYMBOL/EXPIRY drift` / `GUARD/CONTROL missing` → admission fails closed, no broker contact, record.

## Containment (any step)
[HUMAN] set backend flags OFF → [PROD-OPERATOR] `restore_copier_bridge_execution_off --commit` → [HUMAN] manually close any #48 position in MT5 → [HUMAN] revoke the authorization → capture state. The kill switch (`copier_live_disarm`) is engaged only if separately authorized for containment. **The system never re-opens or late-reconciles a failed/late entry** (transport one-shot; admission time-bound + fail-closed on restart/expiry).

**No step here is armed or executed by Claude. Claude's role is read-only preflight, read-back verification, and observation only.**

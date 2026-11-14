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

## Quick-start — one page (GFX-115; what actually has to happen tomorrow)

The next live acceptance needs **Nuno at the keyboard for exactly three things**; Claude/the operator drive
everything else (non-secret). Nuno never pastes a key into chat and never asks Claude to place a trade.

**What Nuno clicks/types (the ONLY human steps):**

1. **[HUMAN]** RDP into the account-46 Windows desktop (`WIN-RD8VDS93DK7`).
2. **[HUMAN]** In an elevated, non-transcribed PowerShell, run the reviewed hidden-input key provisioner and type
   the ingest key **once** at the hidden prompt (confirm phrase `provision-key-account-46`):
   ```
   cd C:\GuvFX\copier-emitter\operator
   .\Provision-GuvfxCopierOneShotKey.ps1
   ```
   Reply only `DONE` (or a redacted one-line `reason`). See `COPIER_ONE_SHOT_ATTESTATION_RUNBOOK.md` Role B2.
   *(Provisioner staged + hash-verified `84e931fa…` under GFX-COPIER-ONESHOT-KEY-PROVISIONING-114; the ingest
   key is `COPIER_INGEST_HMAC_KEY`, held by Nuno.)*
3. **[HUMAN]** During the armed window, perform the explicit **master** MT5 actions in MetaTrader on #46, one at
   a time: **OPEN** 0.01 XAUUSD (with SL+TP) → **MODIFY** (tighten SL) → **FULL CLOSE**. Nothing on #48.

**What Claude / the operator drive (non-secret, no trade, no key):**
preflight (`copier_live_demo_preflight`) · authorization read-back · ARM/DISARM orchestration (ADR-0097, staff
command) · verifier-window sequencing + signed attestation read-back (non-secret) · admission verification ·
per-action latency observation (target **≤1 s**, hard ceiling **≤2 s** acceptance→job — see the synthetic proof
below) · follower read-back / ADR-0096 ticket binding · deadline + abort enforcement · teardown and final
zero-state verification.

**Latency expectation (proven synthetically, no broker):** OPEN/MODIFY/CLOSE each clear well under the **2 s hard
ceiling** from authenticated event acceptance to governed job creation. Evidence: `trade_copier.tests_synthetic_e2e_latency`
(p50/p95/max printed; broker fill is **not** part of this measurement and must never be reported as such).

**Expected success markers (in order):** preflight `verdict=READY_TO_ARM` → authorization `status=ACTIVE` (sole
for the pair) → one `CopierLiveArm` engaged → OPEN → one LIVE `PLACE_ORDER`, `orders_consumed=1`,
`CopierRealtimeDispatch OPEN_SUBMITTED` (`is_late=false`) → ADR-0096 bind `reconciliation_status=MATCHED` →
MODIFY → one `MODIFY_POSITION` → FULL CLOSE → one `CLOSE_TRADE` → DISARM → zero-state (no arm, no in-flight job,
key file absent).

**Maximum waiting periods:** authorization window **≤ 15 min** total; per-action observe **≤ 90 s**; hidden-key
step **≤ 10 min** before the Abort path; latency per action **≤ 2 s** acceptance→job (a slower observation means
the slow reconciler ran — investigate, do not retry).

**Immediate STOP / containment triggers:** wrong account/symbol/side/volume; a second follower order or any extra
job; a PARTIAL_CLOSE on this LIVE route (must never happen); `UNKNOWN_OUTCOME` / `PRE_STATE_MISMATCH` /
`DEADLINE_EXCEEDED`; identity/authorization/expiry drift; any `#48`-initiated action; bridge/observer/watchdog
degradation. **Containment:** `copier_live_arm --disengage …` (or emergency `copier_live_disarm` = kill switch +
disarm-all), `.\Provision-GuvfxCopierOneShotKey.ps1 -Remove`, close the verifier window, restore host admission
OFF (`restore_copier_bridge_execution_off`), verify zero-state. **No automatic retry or late recovery, ever.**

The authoritative, fully-labelled ordered sequence follows below; the quick-start is a timed index into it.

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

> **ADR-0096 update (post-fill follower-ticket binding).** The copied OPEN's follower ticket is now bound
> **event-driven from the OPEN order's fill result** at job completion (`bind_open_result`), with **no
> `/mt5/positions` read**. So for a **position-clean** follower (this route's precondition) the governed MODIFY
> (step 11) and FULL_CLOSE (step 13) can fire **without** the route-scoped follower-read authority
> (`COPIER_FOLLOWER_READONLY_POSITIONS_ENABLED`) — the positions-read reconcile is now the FALLBACK, used only
> when the OPEN result carries no ticket (UNKNOWN). At step 9, confirm the follower ticket bound from the OPEN
> result (`reconciliation_status=MATCHED`, `status=EXECUTION_VERIFIED`). The result reports the opening ORDER
> ticket; for a position-clean fresh open it equals the position ticket the mutations resolve by (a NETTING
> follower with a pre-existing same-symbol position would diverge and fail CLOSED at MODIFY/CLOSE — the route's
> position-clean precondition forecloses this). Still DARK/default-OFF; preparation-only. See the end-to-end
> source + reconcile readiness in `docs/COPIER_REALTIME_SOURCE_ACTIVATION.md`.

> **ADR-0097 update (one-action ARM/DISARM — replaces the multi-flag/worker ceremony).** Step 4's ~15-flag +
> 7-worker arming is superseded by a SINGLE audited command: **[HUMAN/STAFF]** `copier_live_arm
> --authorization-id <N> --by <STAFF_EMAIL> --reason "..."` creates one authorization-bound, auto-expiring
> `CopierLiveArm` (capped at the authorization expiry) after re-running the backend preflight fail-closed. The
> LIVE admission now requires that engaged, route-exact arm as an ADDITIONAL AND — the base `COPIER_*` gates stay
> deployment config and do nothing without an arm. The real-time pipeline (immediate dispatch + ADR-0096 post-fill
> binding) drives OPEN/MODIFY/CLOSE event-driven, so **no worker is re-run between trade actions**. Return-to-OFF
> is one command: **[HUMAN/STAFF]** `copier_live_arm --disengage --authorization-id <N> --by <STAFF_EMAIL>
> --reason "demo complete"` (does NOT engage the durable kill switch). Backend DISARM does NOT restore Windows
> host admission — the signed `restore_copier_bridge_execution_off` remains a separate operator step. The
> emergency `copier_live_disarm` engages the kill switch AND disengages all arms. Still DARK/default-OFF. See ADR-0097.

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
4. **[HUMAN/STAFF]** Backend ARM (ADR-0097) — one audited command: `copier_live_arm --authorization-id <N>
   --by <STAFF_EMAIL> --reason "..."`. It re-runs the backend preflight fail-closed and creates one
   authorization-bound, auto-expiring `CopierLiveArm` (capped at the authorization expiry). With the base
   `COPIER_*` gates set as deployment config, this single arm is the additional AND that enables the LIVE path;
   the real-time pipeline (immediate dispatch + ADR-0096 post-fill binding) then drives OPEN/MODIFY/CLOSE
   event-driven — no worker is re-run between trade actions. (The legacy per-worker stack in
   `COPIER_LIVE_WORKER_SEQUENCE.md` is superseded for arming.)
5. **[PROD-OPERATOR]** Signed host-admission **dry-run** (with the flags injected): `provision_authorized_live_copier_bridge --account-id 48 --authorization-id <N>` → require `gates_ok=true`, `env_will_be_live=true`.
6. **[PROD-OPERATOR]** Signed host-admission **commit**: same command **+ `--commit`** → sets `MT5_ALLOW_LIVE` + exact-account/route/symbol/time admission env on #48's bridge + restart.
7. **[READ-ONLY AGENT]** Read-back the exact #46→#48 XAUUSD boundary: authorization ACTIVE+unexpired; the 5 gates TRUE; host admission exact-account/route/symbol/time-pinned; **0 jobs; 0 broker tickets**; kill switch clear; sole route.
8. **[HUMAN]** Open exactly **one 0.01 XAUUSD** position on #46 (SL/TP optional).
9. **[READ-ONLY AGENT]** Observe exactly **one** follower OPEN on #48: master/follower tickets, volume, latency, deviation. On any trigger → containment (step 14).
10. **[HUMAN]** Add or change SL/TP on #46.
11. **[READ-ONLY AGENT]** Observe the follower MODIFY on #48: master/follower revision, latency, resulting SL/TP, any broker normalization (report it exactly — never claim equality when values differ).
12. **[HUMAN]** Close the #46 master position.
13. **[READ-ONLY AGENT]** Observe exactly one follower FULL_CLOSE on #48; both flat; no residual; no pending mutation; no retry scheduled.
14. **[HUMAN/STAFF]** Immediate backend return to OFF (ADR-0097): `copier_live_arm --disengage --authorization-id <N> --by <STAFF_EMAIL> --reason "demo complete"` (does NOT engage the kill switch). This makes the arm-dependent gates false at once; it does NOT restore Windows host admission (step 15).
15. **[PROD-OPERATOR]** Signed host-admission OFF: `restore_copier_bridge_execution_off --account-id 48` (dry-run) then `--commit` → removes `MT5_ALLOW_LIVE` + admission env, restarts #48's bridge to DARK.
16. **[HUMAN]** Revoke the authorization: `revoke_copier_live_authorization --authorization-id <N> --owner-email support@guvfx.com --master-account-id 46 --follower-account-id 48 --revoked-by <STAFF_EMAIL> --reason "demo complete"` (or `reconcile_expired_copier_authorizations --commit` for an expired one).
17. **[READ-ONLY AGENT]** Final OFF read-back: all copier flags OFF; host admission absent (bridge DARK); 0 ACTIVE authorizations; `LiveExecutionAuthorization` 0; kill switch clear; 0 queued/running copier jobs; no residual #48 position; route monitoring-only; sibling accounts/routes unchanged.

## Failure classifications
`OPEN_REJECTED/TIMEOUT` → record FAILED, **never place later** (position abandoned); master-open/follower-not is an accepted asymmetric outcome. `MODIFY_UNKNOWN` → record, no retry without a separate human decision. `CLOSE_FAILED/TIMEOUT` → alert + **[HUMAN] manual close of #48 in MT5** (contingency). `IDENTITY/ROUTE/SYMBOL/EXPIRY drift` / `GUARD/CONTROL missing` → admission fails closed, no broker contact, record.

## Containment (any step)
[HUMAN] set backend flags OFF → [PROD-OPERATOR] `restore_copier_bridge_execution_off --commit` → [HUMAN] manually close any #48 position in MT5 → [HUMAN] revoke the authorization → capture state. The kill switch (`copier_live_disarm`) is engaged only if separately authorized for containment. **The system never re-opens or late-reconciles a failed/late entry** (transport one-shot; admission time-bound + fail-closed on restart/expiry).

**No step here is armed or executed by Claude. Claude's role is read-only preflight, read-back verification, and observation only.**

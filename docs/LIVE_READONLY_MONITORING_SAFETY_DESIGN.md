# LIVE Read-Only MT5 Monitoring — Architecture & Safety Design (Phase 1)

**Packet:** LIVE Read-Only Data Access + Dashboard Integrity (P1). **Priority:** below Withdrawal Intelligence V1
(P0). **Stream:** separately governed, narrowly scoped.
**Date:** 2026-10-08. **Phase 1 is READ-ONLY** (code review only; no host/bridge/broker contact, no mutation).
**Gate verdict: SAFE SEPARATION PROVEN.** A LIVE read-only access policy can be added without weakening any
execution protection. Implementation (Phase 2+) may proceed. **No production bridge restart / operational mutation
is performed in this stream** — the deployment prerequisites in §7 are returned for a separate operational gate.

---

## 1. Goal

Support complete **read-only** monitoring of LIVE MT5 accounts — balance, equity, open positions, floating P&L,
deals/history, dashboard analytics — **independent of LIVE execution authorization**. Today the bridge read routes
`/mt5/positions` and `/mt5/snapshots/deals` refuse any non-demo terminal (`account_not_demo`, HTTP 400), which is
why the Dashboard falsely reports Account 46 "not reachable" (see `ACCOUNT_46_DASHBOARD_INTEGRITY_DIAGNOSIS.md`).

## 2. The change, in one sentence

Make the two read handlers **pure reads** (like `fetch_account_snapshot`, which is already ungated and already
serves LIVE Account 46's balance of 205.81), report the observed session identity from them, and **move DEMO/LIVE
environment *verification* to the backend read firewall** — while leaving **every** order/close/modify/authorization
gate byte-for-byte unchanged.

## 3. Why this is safe — structural proof (Phase 1)

Reviewed: all bridge read handlers, all execute/mutation paths, the HTTP dispatch + auth, the backend read path +
identity firewall, and the full LIVE-execution-authorization chain. Method: 5 independent read-only audit lenses +
2 adversarial, refute-by-default escalation hunts (workflow `wf_76ab6fa1-bcb`, 7 agents, 0 errors). All 5 audits:
`SAFE_SEPARATION_PROVABLE`. Both escalation hunts: `any_exploitable=false`, `SAFE_SEPARATION_PROVEN`.
*(Honesty note: 2 of the 5 audit agents returned placeholder/stub structured output — a workflow-quality blemish,
not a safety gap; every load-bearing claim below is independently grounded in the execute-path lens, the
exec-authorization lens, both escalation hunts, and direct code reads cited here.)*

**(a) `order_send` is unreachable from any read.** There are exactly four `mt5.order_send` call sites, all in
execute/mutation functions, none in a read handler:
`execute_mt5_trade` ([bridge:1284](../scripts/mt5_signal_bridge.py)), `execute_demo_order` (:1609),
`close_position` (:2040), `modify_position` (:2175). The two handlers being changed — `fetch_positions`
(:1899-1948) and `fetch_deals_snapshot` (:1789-1854) — call **only** `mt5.account_info()`,
`mt5.positions_get()` / `mt5.history_deals_get()`, `guarded_initialize`, `mt5.shutdown()`. No `order_send`, no
`TRADE_ACTION_*` request builder, no execute helper.

**(b) The demo gate is not a shared helper.** The `if account_info.trade_mode != 0: return account_not_demo` check
is an independent inline statement duplicated at six sites. The two in the read handlers (:1807, :1917) are
physically separate from the four execution copies (`execute_demo_order` :1533, `shadow_order_check` :1707,
`close_position` :1971, `modify_position` :2098) and the send-time re-assert `evaluate_mutation_identity` :670.
Removing the two read copies cannot touch the execution copies.

**(c) Dispatch is disjoint.** `do_GET` (:2347) maps only read handlers; `do_POST` (:2410) maps only execute
handlers. A GET request can never reach an order/close/modify handler. Agent-token auth is enforced fail-closed on
every protected route (:2311). Status mapping is `200` on `ok`, `400` otherwise — so a flat LIVE account returns
**`200` with an empty positions list** once the demo refusal is gone.

**(d) The LIVE-execution-authorization chain is untouched and unreachable from reads.**
- `MT5_ALLOW_LIVE` is written in exactly one place — the provisioning env-renderer
  `bridge_config.render_bridge_env` ([:41](../backend/execution/bridge_config.py)), gated by
  `is_live_environment(account)` **and** a valid §3 authorization. No read path reaches it.
- `LiveExecutionAuthorization` is created in exactly one place — the human, owner-scoped, flag-gated ceremony
  `provisioning.authorize_live_execution` ([:617](../backend/hosted_workspace/provisioning.py)).
- Readiness condition 11 ([readiness.py:177-186](../backend/execution/readiness.py)) derives solely from the
  account's environment classification + the D4 flag + the durable authorization row; `evaluate_monitoring`
  (:234-266) omits condition 11 entirely. A bridge read mutates none of these.

**(e) The only shared surface is `guarded_initialize`** (called by both reads and executes). The change does **not**
modify it. Operational caveat → §7 (guarded-attach must be armed so an attach can never *launch*+auto-login a LIVE
terminal).

**(f) Backend read path preserves isolation.** `resolve_account_snapshot_base` routes every read to the account's
**own** per-tenant endpoint and fails closed otherwise (a hosted account with no READY endpoint is refused — never
a global/sibling bridge). `resolve_scope` scopes to `request.user` (ownership / IDOR-safe). `verify_snapshot_identity`
pins login **exact** + server **casefold**. No cross-account/cross-tenant exposure.

## 4. The 12 invariants after the change

| # | Invariant | How preserved |
|---|---|---|
| 1 | Account ownership | `resolve_scope(request.user)` — unchanged |
| 2 | Broker identity matched | per-tenant endpoint + identity firewall — unchanged |
| 3 | Exact MT5 login | `verify_snapshot_identity` login EXACT — unchanged |
| 4 | Server identity | `verify_snapshot_identity` server casefold — unchanged |
| 5 | DEMO/LIVE environment | **NEW**: observed `trade_mode` vs account's expected environment, added to the read firewall (§5), fail-closed on a known disagreement |
| 6 | Tenant isolation | per-tenant base, fail-closed; hosted never uses global agent — unchanged |
| 7 | No cross-account exposure | login-exact discriminator + per-tenant base — unchanged |
| 8 | No order placement | `order_send` only in execute paths, all gates intact — unchanged |
| 9 | No position modification | `modify_position` demo + identity gates intact — unchanged |
| 10 | No position closure | `close_position` demo + identity gates intact — unchanged |
| 11 | No LIVE execution authorization | `MT5_ALLOW_LIVE`/`LiveExecutionAuthorization`/cond-11 unreachable from reads — unchanged |
| 12 | No `MT5_ALLOW_LIVE` activation | written only by provisioning renderer under §3 auth; reads never reach it — unchanged |

Invariant 5 is the ONLY one that requires new code; all others are preserved by *not touching* the code that
enforces them.

## 5. Invariant-5 implementation (the one required addition)

1. **Bridge** reports observed environment so the backend can verify it: add `trade_mode` (and, for
   `fetch_positions`, the observed `account_login`/`account_server`) to the read responses. Pure additive fields.
2. **Backend firewall** `verify_snapshot_identity(account, observed_login, observed_server, *, require_server=False,
   observed_trade_mode=None, require_environment=False)` gains an additive environment branch: when
   `require_environment` is on and both the observed demo-ness (`trade_mode == 0`) and the account's expected
   environment are **known**, a disagreement returns a new `ID_ENVIRONMENT_MISMATCH` and refuses the read
   (fail-closed). When the expected environment is unknown/unset it is SKIPPED (login+server already pin the tenant),
   so no existing DEMO account regresses. Default `require_environment=False` keeps every current caller
   byte-identical.
3. **Read consumers** (`_fetch_mt5_account_balance`, `_fetch_mt5_open_positions`, `_fetch_mt5_balance_ops`, and the
   deals-ingest identity check) pass the observed `trade_mode` + `require_environment=True`.
4. Expected environment is read from `TradingAccount.environment` (authoritative `demo`/`live`), i.e.
   `expected_is_demo = (environment == DEMO)`.

**Test matrix:** LIVE-observed terminal (`trade_mode=2`) for a DEMO-classified account → **refused**; DEMO-observed
for a LIVE-classified account → **refused**; observed==expected → **passes** (positive control, RULE 11); expected
unknown → **skipped** (no regression); plus login/server mismatch still refuse.

## 6. Out of scope (hard boundaries)

No change to `execute_mt5_trade`, `execute_demo_order`, `close_position`, `modify_position`, `shadow_order_check`,
`evaluate_binding`, `evaluate_mutation_identity`, `_bridge_allow_live`, `guarded_initialize`, the poller, or any
readiness/authorization code. No LIVE order submission. No `MT5_ALLOW_LIVE` activation. All DEMO behaviour preserved
byte-for-byte (a demo account's observed `trade_mode=0` matches expected demo → passes exactly as today). No
expansion toward automated LIVE execution.

## 7. Deployment prerequisites (returned for a SEPARATE operational safety gate — NOT performed here)

A production bridge redeploy is required for the bridge-side change to take effect; it is the gated operational step.

1. **Per-tenant bridge restart = operational mutation → requires an approved maintenance plan.** Do **not** restart a
   tenant bridge serving open positions without one. Account 46 is currently **flat** (0 open positions), which is the
   safest window, but the restart still requires the operational gate and Sponsor sign-off.
2. **Guarded-attach must be armed** on any LIVE-monitoring bridge: `MT5_GUARDED_ATTACH=1` so `mt5.initialize(path=)`
   can only ATTACH to a running terminal, never launch + auto-login a LIVE session (closes the pre-existing attach
   TOCTOU for the LIVE case). Startup assertion + test to be added.
3. **`MT5_ALLOW_LIVE` must stay UNSET** on a monitoring-only bridge (Account 46). Reads never set it; this is the
   belt-and-braces operational confirmation.
4. **LIVE accounts must read via their own per-tenant endpoint only**, never the legacy shared global agent (for a
   LIVE account `resolve_account_snapshot_base` already fails closed without a per-tenant endpoint; confirm no LIVE
   account is ever routed to the global base).
5. **Backend deploys first, additively** (the `require_environment` firewall + dashboard are backward-compatible and
   DARK-safe); the bridge redeploy for Account 46 follows under the maintenance plan.
6. **Rollback:** revert the bridge code + redeploy restores the demo refusal; the backend firewall kwarg defaults
   off; the dashboard change is cosmetic.

## 8. Certification posture

Code-level certification (this stream): tests prove LIVE reads succeed, flat → `200` empty (not `400`), orders still
refused, environment firewall enforces invariant 5, floating-P&L computed from **simulated** positions (no live
trade). Production certification of Account 46's live endpoints is **gated** behind §7 and returned as a prerequisite.

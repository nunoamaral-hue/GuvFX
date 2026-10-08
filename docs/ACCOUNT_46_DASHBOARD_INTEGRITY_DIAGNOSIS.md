# ACCOUNT46_DASHBOARD_INTEGRITY_DIAGNOSIS

**Packet:** Account 46 Dashboard Data Integrity (P1 — LIVE Monitoring Completion)
**Date:** 2026-10-08
**Mode:** STRICTLY READ-ONLY. No MT5 restart / reconnect / order / exec / recovery / self-connect. Code-level
forensic only; no live-host probe issued this session (see §Evidence posture). Withdrawal Intelligence V1 remains P0
and was not touched.
**Verdict:** The reported inconsistencies are **explained and benign to safety**. Account 46 is **NOT actually
unreachable**. There is **no broker failure, no data-isolation breach, and no LIVE execution exposure.** The
Dashboard surfaces are driven by *three different projections*, two of which behave differently for a LIVE
(non-demo) account than the Broker Accounts page. All root causes are read-path/UI issues. **Dashboard is NOT
certified** until the remediation below is Sponsor-reviewed.

---

## 1. Observed state (from the Sponsor screenshots)

Broker Accounts: Account 46 · TradersWay · 55442 · LIVE · Broker connected · "Connected — monitoring only" · 0 strategies.
Dashboard (Account 46 selected): Equity USD 205.81 · Daily P&L 0.00 · Trading "Status unavailable" · Open Trades 0 ·
Floating P&L $0.00 · "partial" · "1 account not reachable".

The account is flat (no open positions), so Open Trades 0 is correct. The discrepancy is the **reachability /
status reporting**.

---

## 2. The three projections (answers Q3, Q9)

The Dashboard does **NOT** use the same canonical observation projection as the Broker Accounts page. Three distinct
sources feed the header:

| Dashboard element | Source | Nature |
|---|---|---|
| "Connected — monitoring only" (Broker Accounts page) | host-side **observation projection** (`hosted_workspace` matcher → `run_hosted_observations`, cron-applied) | async, DB-cached; needs no request-time bridge contact |
| Equity 205.81 | `GET /api/analytics/trade-history/?account_id=46` → `perf.mt5_equity_current` | **request-time LIVE** bridge read (`_fetch_mt5_account_balance` → `/mt5/snapshots/account`) |
| Open Trades / Floating P&L / "N not reachable" / "partial" | `GET /api/analytics/portfolio/open-trades/` → `stale_accounts` | **request-time LIVE** bridge read (`_fetch_mt5_open_positions` → `/mt5/positions`) |
| "Trading: Status unavailable" | `GET /api/reliability/trading-health/` (RX-2) | reliability `TradingHealthSnapshot` rows |

Because they are different projections, a LIVE account can read CONNECTED on one and degraded on another at the same
instant. That is the whole inconsistency.

---

## 3. Root cause of "1 account not reachable" (answers Q1, Q2, Q4, Q5, Q7, Q11, Q12)

**Affected account (Q1): Account 46**, unambiguously. The screenshot is account-46-scope, and
`portfolio.resolve_scope` returns exactly `[46]` for that scope, so `stale_accounts` can only contain 46.

**The bridge IS reachable from the Dashboard backend (Q2): YES — proven.** `mt5_equity_current` has **no durable
fallback** (`backend/analytics/views_trade_history.py:918-920`); it is set *only* from the live
`_fetch_mt5_account_balance`. Equity 205.81 therefore proves the per-tenant snapshot transport resolved to a READY
endpoint, the bridge answered, and the identity firewall passed (`login 55442` exact + server casefold match from
the 1ad646b fix). The same transport serves the positions read.

**The positions read is refused by a DEMO-ONLY gate (Q4, Q5).** The bridge route `/mt5/positions` →
`fetch_positions()` refuses any non-demo terminal:

```
scripts/mt5_signal_bridge.py:1917
    if account_info.trade_mode != 0:        # 0=DEMO, 1=CONTEST, 2=REAL
        return {"ok": False, "error": "account_not_demo"}
```

Account 46 is LIVE (`trade_mode=2`). The route returns **HTTP 400** `{"ok":false,"error":"account_not_demo"}`
(dispatch `scripts/mt5_signal_bridge.py:2383-2386` sends `400` when `not ok`). The backend treats any
non-`ok` positions response as unavailable and returns `None`
(`backend/analytics/views_trade_history.py:190-191`), which marks the account **stale**
(`backend/analytics/portfolio.py:316-327` → `stale_accounts=[46]`).

**Consequences, all from the single stale flag:**
- "1 account not reachable" (`OpenTradesPanel.tsx:31,123,176-181`, fed by `data.stale_accounts`).
- Floating P&L "· partial" — `portfolio_open_pl` forces `basis="PARTIAL"` whenever any account is stale and records
  `{"account_id":46,"reason":"stale_read"}` (`portfolio.py:322-326`). This is the "Data completeness: partial" the
  Sponsor saw; there is no literal "completeness" string — it is the Floating P&L partial badge
  (`OpenTradesPanel.tsx:138-139`).
- Open Trades 0 — correct independently (account is flat), but it would read 0 regardless because the list is empty
  on a stale read.

**The balance/equity snapshot route is NOT demo-gated** (`fetch_account_snapshot`,
`scripts/mt5_signal_bridge.py:1857-1892` — no `trade_mode` check), which is exactly why 205.81 renders while
positions do not. **The deals route `/mt5/snapshots/deals` IS demo-gated** (`fetch_deals_snapshot`,
`:1808` same `account_not_demo`), so Account 46 also 400s there; impact is only the cash-flow-aware balance baseline
(`_fetch_mt5_balance_ops` → `None` → chart keeps its reconstruction, fail-closed). No other remaining 400s on the
dashboard read path (Q7).

**Q11 (LIVE classification → partial coverage): YES, directly.** The LIVE classification (`trade_mode=2`) is the
sole trigger for both demo gates. A DEMO account in the same state would read positions fine.

**Q12 (actual failure vs incorrect label): INCORRECT LABEL.** "Not reachable" is a mislabel — the bridge is
reachable and the account is healthy; the positions *read* is deliberately refused for a LIVE terminal. The warning
copy conflates "we chose not to read this" with "we could not reach it."

---

## 4. Root cause of "Trading: Status unavailable" (answers Q9, Q10)

The header Trading badge reads `tradingHealth` from `GET /api/reliability/trading-health/` fetched **once on mount
with no `account_id` and no re-fetch on scope change** (`dashboard/page.tsx:377-379`, deps `[]`).

`TradingHealthView` (`backend/reliability/views.py:26-55`) scopes by identity:
- **non-staff, no `account_id`** → returns `{ok:false, state:"UNKNOWN", reasons:["Provide your own account_id."]}`
  by design (IDOR / GFX-BETA-PHASE0 C14). → badge is **always** "Status unavailable" for a beta member, on every
  account.
- **staff (Nuno)** → `_latest("GLOBAL")`; if no GLOBAL `TradingHealthSnapshot` exists (reliability_tick dormant) →
  `{ok:false, state:"UNKNOWN"}`. GLOBAL operator health is **not** Account 46's state anyway.

Either path yields UNKNOWN → "Status unavailable" (`dashboard/page.tsx:617,622`). This is a **wiring + projection-
source mismatch**, not a broker failure: the badge never asks about Account 46, so it cannot agree with the Broker
Accounts page's "Connected — monitoring only."

**Q10 (`is_active=False` contribution):** Not a direct cause. `resolve_scope` filters `disconnected_at__isnull`,
not `is_active`; the open-trades and trading-health paths do not gate on `is_active`. `is_active=False` /
`workspace_confirmed_at=None` (the ADR-0044 activation step) may mean no per-account `TradingHealthSnapshot` is ever
produced, but the dominant reason the badge is UNKNOWN is that the Dashboard never passes `account_id`.

**Q8 (stale cache):** Not a caching artefact. `OpenTradesPanel` keeps last-known data and shows "reconnecting" only
on *transient* failure; Account 46's `account_not_demo` is **deterministic**, so every poll fails identically — a
persistent refusal, not a stale cache. Trading-health is fetched once and not refreshed on scope change (a mild
staleness), but the UNKNOWN is structural, not cached.

**Q6 (balance/equity reads consistent after the server-name fix):** The one observed read succeeded (205.81),
consistent with the casefold fix (1ad646b) letting `TradersWay-Live` vs `Tradersway-Live` pass identity. Not
independently re-probed this session (see Evidence posture).

---

## 5. Phase 2 — performance empty-state integrity

For a newly connected account with **zero closed trades** (Account 46), `_compute_balance_series` returns a
zero-filled stat block, NOT nulls:

```
backend/analytics/views_trade_history.py:624-628
    { "total_trades": 0, "wins": 0, "losses": 0, "win_rate_pct": 0.0,
      "longest_loss_streak": 0, "max_drawdown_pct": 0.0, "net_pnl_total": 0.0 }
```

Because the frontend uses `?? null` (which does not catch a real `0.0`), these zeros render as **measured values**:

| Metric | Empty-state today | Truthful? |
|---|---|---|
| Win rate | **0%** (`win_rate_pct: 0.0`) | ✗ reads as "measured 0% win rate" |
| Max drawdown | **"Low"** (`max_drawdown_pct: 0.0` < 10 → green "Low", `dashboard/page.tsx:564`) | ✗ reads as a good measured value |
| Net P&L / Trend | **"Stable"** (`net_pnl_total: 0.0` → `trend` "Stable", not "No data", `:558-561`) | ✗ reads as measured flat |
| Profit factor | `null` → no label (`acctProfitFactor`, `:527,563`) | ✓ already truthful |
| Expectancy | `null` → no label (`acctExpMoney`, `:529,565`) | ✓ already truthful |
| Equity curve | ledger returns state `BUILDING`/`SINGLE`, "history is building" (`views_portfolio.py:104-106`) | ✓ already truthful |

So three of six metrics present missing history as measured zero; three already degrade honestly. No trading records
are wrong; this is purely a presentation defect for insufficient history.

---

## 6. Smallest safe remediation proposal (NOT implemented — Sponsor review required)

All additive, behaviour-preserving, no broker/exec/strategy change. Independent of the Withdrawal V1 P0 path.

1. **"Not reachable" mislabel + LIVE positions read (primary).** The cleanest truthful fix is to distinguish
   *refused-by-policy* from *unreachable*. Options, smallest first:
   - **(1a) Frontend copy only:** when `stale_accounts` is present but equity read succeeded, show "monitoring only —
     live positions not shown" instead of "not reachable". Zero backend change; removes the false alarm but still
     shows no positions.
   - **(1b) Backend reason surfacing:** have `_fetch_mt5_open_positions` carry the bridge reason (`account_not_demo`)
     through to a per-account `reason` so the panel can say "live account — positions read disabled" distinctly from
     a true transport failure. The backend already models `reason:"stale_read"` in `portfolio_open_pl`.
   - **(1c) Read-only LIVE positions (larger, Amber):** add a read-only, no-order positions path for LIVE terminals
     (lift the demo gate on **read** `/mt5/positions` + `/mt5/snapshots/deals` only, never on close/modify/order).
     This is a genuine behaviour change to a safety gate → requires an ADR + adversarial review; **do not** bundle
     with 1a/1b. The order/close/modify demo gates (`:1971`, `:2098`, send-time re-assert `:666-670`) MUST remain.
   Recommend **1a + 1b now** (honest label, no gate change); defer 1c to its own governed packet if the Sponsor wants
   live floating P&L on the Dashboard.

2. **Trading badge source (secondary).** Pass the selected scope's `account_id` to
   `/api/reliability/trading-health/` and re-fetch on scope change, OR source the Dashboard Trading badge from the
   same canonical observation projection the Broker Accounts page uses (so it reads "Monitoring only" instead of
   "Status unavailable"). Preferred: align the badge to the broker/observation projection for a monitoring-only
   account, since a per-account reliability snapshot may legitimately not exist yet.

3. **Empty-state (Phase 2).** When `total_trades == 0`, treat `win_rate_pct` / `max_drawdown_pct` / `net_pnl_total`
   as **absent**, not `0.0` — either return `null` from the no-trades branch (`views_trade_history.py:624-628`) or
   gate the three frontend labels on `totalTrades > 0`, mirroring how profit factor / expectancy already null out.
   Trend should read "No trades yet" for `total_trades == 0`. No trading records are modified.

4. **Partial tooltip copy (minor).** The Floating P&L "partial" tooltip hardcodes "Some accounts use a currency that
   could not be converted to USD" (`OpenTradesPanel.tsx:139`), but Account 46's partial basis is `stale_read`, not a
   currency issue. Make the tooltip reflect the actual `unconverted[].reason`.

---

## Evidence posture (limitations)

- Root causes are proven by **source reading** + the **internal consistency of the Sponsor's own screenshot** (equity
  205.81 ⇒ balance route works ⇒ bridge reachable; positions 0 + "not reachable" + partial ⇒ positions route
  refused). The positive control for "bridge reachable" is the rendered equity value itself.
- **Not run this session:** a live read-only `GET /mt5/positions` against Account 46's bridge to capture the exact
  `400 account_not_demo` body. The dashboard already performs this read every 30s, so a one-shot confirmation is
  within the read-only envelope; it was withheld to keep strictly to code-level read-only on the live host and to not
  delay Withdrawal V1 P0. Available on request.
- No DB query against production was run; account/endpoint state is inferred from code + screenshot, not re-queried.
- Safety invariants unchanged: 25/35/36 untouched, 33 quarantined, LIVE exec/recovery OFF, `MT5_ALLOW_LIVE` unset,
  self-connect OFF, monitoring read-only (Account 46 only).

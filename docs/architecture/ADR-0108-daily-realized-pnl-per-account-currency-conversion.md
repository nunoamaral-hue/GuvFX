# ADR-0108 — Daily realized P&L: per-account currency conversion + honest coverage (never a false $0.00)

- **Status:** Accepted (repository-only; additive; migration-free; no execution/Copy-Trading authority; no
  credential handling; no manual snapshot; no production/host mutation by this packet).
- **Packet:** GFX-DASHBOARD-DAILY-PNL-AUTOMATIC-INGESTION-131.
- **Date:** 2026-11-15.
- **Builds on (never replaces):** ADR-0066 (single-currency realized conversion + per-account `account_net_pnl`),
  the `portfolio_fx.aggregate_usd` per-account USD aggregation already used by the balance/equity/open-P&L totals,
  ADR-0103/0105/0106 monitoring enrolment/health projection, the `monitored_ingest` automatic closed-deal sync, and
  the Fed H.10 / BoE `StoredDbFxRateSource` (gated per-user via `fx_conversion_enabled_for`). The lifetime
  performance metric (`portfolio_metrics` "Net PnL") currency policy is **deliberately unchanged** (see Decision §4).

## Context — the defect

The dashboard summary card **"Daily PnL (realized)"** showed `+$0.00` on days with real same-day closed trades.
Read-only production diagnosis (owner 29) reproduced it against real data: e.g. **2026-11-11** the card read `$0.00`
while Vantage #52 (GBP) had **+£38.04 (+$50.36)** of same-day realized; **2026-11-05** it read `-$8.48` while the true
day was **+$17.38** (GBP accounts #49 +£16.58 / #52 +£2.95 dropped); **2026-11-10** read `+$8.25` vs true `+$24.47`.

**Root cause:** `analytics.portfolio.daily_realized_pnl` summed the figure over a **whole-scope single denomination**:
it included the USD accounts and either converted the non-USD accounts only when the ENTIRE scope was one non-USD
currency (ADR-0066) or **excluded every non-USD account** otherwise. A realistic mixed USD+GBP monitored population
therefore dropped the GBP accounts' same-day realized entirely, and a GBP-only trading day summed to `0.0` over the
(idle) USD subset. The frontend rendered that `0.0` as a bare `+$0.00`, indistinguishable from "nothing closed
today" — a **false zero** that hid real activity. The GBP→USD rate was available the whole time (Fed `DEXUSUK`
1.3238); only the aggregation was wrong. #58 Dominion (USD) was a separate, self-resolving ingest-timing artifact
(its +34.37 on 2026-11-13 is present once the automatic `monitored_ingest` sync ran).

## Decision

1. **Per-account conversion, summed over the scope — the same machinery as the balance total.** `daily_realized_pnl`
   now computes each account's same-day realized in its OWN denomination (profit+commission+swap of positions closed
   on today's broker-server calendar date) and converts each to USD INDEPENDENTLY — USD at identity, a non-USD
   account via the vetted `rate_source` — then sums the convertible contributions with `portfolio_fx.aggregate_usd`
   (the proven per-account aggregator already used by balance/equity/open-P&L). A new `daily_realized_pnl_detail`
   returns the full `UsdAggregate`; `daily_realized_pnl` stays a back-compatible `(value, basis)` wrapper.

2. **Honest coverage; never a false zero.** The figure's `state` is **OK** (all convertible), **PARTIAL** (a measured
   total over the included subset — some same-day activity could not be converted; the dropped accounts are surfaced
   with their NATIVE amount + currency + reason, never summed), **UNAVAILABLE** (same-day activity existed but none
   converted — `daily_realized_pnl_usd` is `null`, **never** `0.0`), or **EMPTY** (no account closed a trade today —
   a genuine `0.0`). The API exposes `daily_realized_pnl_state`, `_included_count`, `_excluded_count`,
   `_excluded_accounts` and `_excluded_detail`. The dashboard card renders the figure for OK, the figure + a
   "Partial · N excluded" disclosure (naming each excluded account's native figure) for PARTIAL, and "—" for
   UNAVAILABLE — a bare `+$0.00` can no longer stand for dropped activity.

3. **Automatic; no special-case account IDs.** The account set is whatever the caller (the owner's monitored scope)
   passes. A newly monitored account — already auto-enrolled into closed-deal ingestion by `monitored_ingest`
   (connected + READY endpoint, no hard-coded ids) — enters the daily figure with no per-account packet or code
   change. A non-convertible denomination fails closed to PARTIAL/UNAVAILABLE, never a silent cross-currency sum.

4. **Scope boundary (deliberate):** this changes only the DAILY realized figure. The lifetime "Net PnL" /
   performance-snapshot metric (`portfolio_metrics`) keeps its existing documented currency policy (mixed non-USD
   excluded, its own test + UI copy). Aligning that metric is a separate decision, out of scope here. The single
   non-USD `account_net_pnl` path (ADR-0066) is unchanged.

## Consequences

- The daily figure now reflects every monitored account's same-day realized when a vetted rate exists, and is
  truthfully PARTIAL/UNAVAILABLE (never `$0.00`) when it does not. Day window remains the stored broker-server
  `close_time` calendar date (≈UTC; a true-UTC/member-local frame stays a documented platform-wide follow-up —
  `KNOWN_ISSUES`). Deposits/withdrawals are broker BALANCE deals (no `position_id`), never ingested as Trades, so
  they never contribute. Tenant-local: the scope is the authenticated owner's accounts (`resolve_scope`, IDOR-safe);
  Copy-Trading participation grants no execution authority and causes no cross-tenant leakage. Migration-free,
  additive, one bounded trade query for the whole scope (no N+1). DARK/no-op on production until deployed; this
  packet opens a repository PR only (STOP before merge).

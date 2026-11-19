# ADR-0119 — Daily Realized P&L never denominates an unknown currency as USD

- **Status:** Accepted (repository-only; additive; migration-free; corrects a Daily Realized P&L currency-integrity
  defect in `analytics/portfolio.py`; no production deploy, no flag, no schema/DB change, no broker/MT5 contact, no
  snapshot fabrication by this packet).
- **Packet:** GFX-DASHBOARD-DAILY-REALIZED-CURRENCY-INTEGRITY-161 (Dashboard / financial-data integrity).
- **Date:** 2026-11-19.
- **Relates to:** ADR-0066 (per-account single-currency realized conversion), pkt-131 (per-account daily aggregate),
  and the open-position currency-integrity helper `portfolio_fx.resolve_position_currency`.

## Context

The Daily Realized P&L USD aggregate resolved each account's denomination through `_account_realized_currency`, which
returned `FX.normalise_currency(observed_ccy.get(id) or account_currency)`. `normalise_currency(None)` returns **USD**
(the documented blank→USD estate default for BALANCE/EQUITY). Separately, `build_summary` pre-computes `observed_ccy`
with that same blank→USD default (for the balance/equity labels) and fed it to the realized path.

**The defect (proven by `test_blank_currency_live_fail_is_unknown_not_usd`):** when an account's currency could *not*
be established — the live balance read failed **and** no fresh tenant-local snapshot existed **and**
`account_currency` was blank (the production norm) — the realized path defaulted the account to **USD** and summed its
native same-day realized into the USD total, with `basis="USD"` (no exclusion, no PARTIAL). For a non-USD account this
silently denominated, e.g., GBP realized as dollars and reported false completeness. The OPEN-POSITION path already
guarded against exactly this by resolving through `resolve_position_currency` (blank→`UNKNOWN_CURRENCY`); the realized
path did not — an asymmetry.

## Decision

Resolve the realized denomination **only** from an authoritative tenant-local source and **never** default to USD.

1. **`_account_realized_currency`** now resolves via `FX.resolve_position_currency(observed, account_currency)` —
   returning `UNKNOWN_CURRENCY` when neither a fresh observed currency (live read / fresh tenant-local snapshot) nor the
   verified model currency is populated. Identical semantics to the open-position path.
2. **`build_summary`** builds a separate, unknown-**preserving** `realized_ccy` map (the same
   `resolve_position_currency` over the already-resolved `snap` currency + model currency) and passes **it** to
   `daily_realized_pnl_detail`. The existing `observed_ccy` (blank→USD) still drives **balance/equity/metrics**, which
   are **byte-identical** — the realized map is kept deliberately separate so this fix does not touch equity/positions.
3. **`daily_realized_pnl_detail`** excludes an `UNKNOWN_CURRENCY` account with a stable reason **`unknown_currency`**
   (category `currency_unconvertible`): its native figure is surfaced (labelled `UNKNOWN`), never summed as USD, so the
   aggregate becomes **PARTIAL** (some others convertible) or **UNAVAILABLE** (`total_usd = null`, never a false
   `$0.00`). The per-account map is keyed by id, so no account can borrow another's currency or rate.

## Scope and non-goals

- **Daily Realized P&L only.** The genuine-USD path, the governed single-currency FX conversion (ADR-0066), and the
  `rate_unavailable` exclusion for a *known* non-USD currency with no vetted feed are all preserved unchanged and
  remain distinct from `unknown_currency`.
- **Balance / equity / `portfolio_metrics` / `account_net_pnl` are intentionally NOT changed** (constraint: keep the
  correction narrow; do not change equity/positions). `account_net_pnl` (LIFETIME net) and `portfolio_metrics`
  (lifetime performance) share the same blank→USD root pattern via `observed_ccy`/`observed_usd`; they are a documented
  **follow-up**, out of this packet's daily-realized scope.
- **Single-account view.** The dashboard's single-account Daily PnL card is served by the `DailyPnlView`
  (`/api/analytics/daily-pnl/`) **native** figure (no USD conversion), so it structurally cannot commit the
  "sum native as USD" aggregate defect. The summary endpoint's single-account *scope* flows through the same
  `daily_realized_pnl_detail` and therefore inherits this fix (tested).
- **No migration, no flag, no schema/DB change, no backfill, no snapshot fabrication, no broker/host contact.**

## Consequences

- An account whose currency cannot be established is excluded as `unknown_currency` and the daily figure is truthfully
  PARTIAL/UNAVAILABLE — the dashboard never presents a non-USD native figure as dollars, and an all-unknown portfolio
  reads "—", never `$0.00`.
- A future reader must keep `unknown_currency` (currency not established) distinct from `rate_unavailable` (currency
  known, no vetted rate) and from `mixed_trade_currency` (trade legs span denominations). All three exclude; only the
  first two are new/changed here.

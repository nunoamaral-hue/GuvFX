# ADR-0122 — Lifetime net P&L and portfolio metrics never denominate an unknown currency as USD

- **Status:** Accepted (repository-only; additive; migration-free; corrects a LIFETIME net-P&L / portfolio-metrics
  currency-integrity defect in `analytics/portfolio.py`; no production deploy, no flag, no schema/DB change, no
  broker/MT5 contact, no snapshot fabrication by this packet).
- **Packet:** GFX-DASHBOARD-LIFETIME-PNL-CURRENCY-INTEGRITY-163 (Dashboard / financial-data integrity).
- **Date:** 2026-11-19.
- **Relates to:** ADR-0120 (the GFX-161 DAILY-realized fix — same blank→USD root pattern; this ADR is its documented
  LIFETIME follow-up), ADR-0066 (per-account single-currency realized conversion), and the open-position
  currency-integrity helper `portfolio_fx.resolve_position_currency`.

## Context

The LIFETIME figures on the dashboard — each account's realized net P&L (`account_net_pnl`, shown on the account card)
and the portfolio-wide performance metrics (`portfolio_metrics`: `net_pnl_total`, `profit_factor`, `expectancy`,
win/loss, `max_drawdown_money`) — shared the same blank→USD root pattern that ADR-0120 corrected for the daily path.

`build_summary` fed:

- `account_net_pnl` the balance-label currency `cur = FX.normalise_currency(snap_currency or account_currency)`, where
  `normalise_currency(None)` returns **USD** (the documented blank→USD estate default); and
- `portfolio_metrics` an `observed_nonusd_ids` set derived from that same `cur` — which flags only accounts
  **positively known** to be non-USD.

**The defect (proven):** when an account's currency could *not* be established — the live balance read failed **and**
no fresh tenant-local snapshot existed **and** `account_currency` was blank (the production norm) — `cur` collapsed to
USD, so the account was indistinguishable from a genuine-USD account:

- `account_net_pnl` (`test_blank_currency_account_net_pnl_is_none_not_usd`) returned the account's native realized sum
  **as a USD `net_pnl`** (e.g. `38.04` "USD" for an unknown-currency account); and
- `portfolio_metrics` (`test_blank_currency_excluded_from_lifetime_metrics`, `test_all_unknown_metrics_unavailable_null`)
  placed the account in the **USD partition** and summed its closed trades into `net_pnl_total` / `profit_factor` /
  `expectancy` / win-loss with `basis="USD"`, `state="OK"` — false completeness.

The open-position and (after ADR-0120) daily paths already guarded against exactly this by resolving through
`resolve_position_currency` (blank→`UNKNOWN_CURRENCY`); the lifetime paths did not — the remaining asymmetry ADR-0120
flagged as a follow-up.

## Decision

Resolve the lifetime denomination **only** from an authoritative tenant-local source and **never** default to USD,
reusing the GFX-161 unknown-preserving `realized_ccy` map.

1. **`account_net_pnl`** gains an explicit guard: when the caller-resolved `account_ccy` is `UNKNOWN_CURRENCY` the
   figure is withheld (returns **None**) — the account's native realized is never summed as dollars. A missing
   `account_ccy` (`None`, used only by direct unit callers that supply no currency knowledge) is unchanged and keeps the
   documented blank→USD estate default.
2. **`build_summary`** passes each account's unknown-preserving resolved currency `rc = realized_ccy[id]` to
   `account_net_pnl` (so an **established** currency is byte-identical — `rc == cur` for USD or a known non-USD that
   converts via the vetted rate — and an **unestablished** one is withheld), and computes
   `unknown_ids = {id : rc == UNKNOWN_CURRENCY}`.
3. **`portfolio_metrics`** / **`_partition_usd`** gain an additive `unknown_ids` input: accounts whose denomination
   could not be established are excluded from the USD partition **exactly like a non-USD account**, so the money block
   degrades to **PARTIAL** (the USD subset is measured; the unknown is surfaced under `excluded_accounts`) or
   **UNAVAILABLE** (`net_pnl_total = null`, `total_trades = null`, never a false `$0.00`). The per-account map is keyed
   by id, so no account borrows another's currency or rate.

## Scope and non-goals

- **Lifetime net P&L and portfolio metrics only.** The genuine-USD path, the governed single-currency FX conversion
  (ADR-0066), the `rate_unavailable` exclusion for a *known* non-USD currency with no vetted feed, `mixed_trade_currency`
  (trade legs span denominations), the GFX-161 daily behaviour, and the **balance / equity** aggregates are all preserved
  **byte-identical**. The `observed_ccy` map and `observed_nonusd_ids` passed to `portfolio_metrics` are unchanged — only
  the additive `unknown_ids` input changes which accounts are excluded.
- **Layering.** The fix lives in `build_summary` (the layer that resolves authoritative currency), consistent with how
  `observed_nonusd_ids` already works and with ADR-0120. A **direct** unit caller of `portfolio_metrics` /
  `account_net_pnl` that supplies no currency knowledge keeps the documented blank→USD estate default — those callers
  cannot know an account's currency and are intentionally left unchanged.
- **Reason granularity.** The lifetime metrics block exposes a single block-level `reason` (`currency_unconvertible`) for
  both non-USD and unknown exclusions, by existing design. The finer per-account distinction (`rate_unavailable` vs
  `unknown_currency`) lives in the daily `excluded_detail` (ADR-0120) and is preserved and tested.
- **Conservative mixed scope.** A scope that mixes a *convertible* known non-USD account with an *unestablished* one is
  fail-closed **UNAVAILABLE** (the metrics single-currency conversion requires a homogeneous established-currency scope);
  it never overstates. Per-account lifetime conversion like the daily path is out of scope.
- **Frontend: one truthfulness label change.** The existing dashboard already renders metrics `state` UNAVAILABLE →
  `N/A` + a context line and PARTIAL → the measured USD figure + "N included · M excluded", and a null per-account
  `net_pnl` as "—" (the same machinery GFX-161 relies on). The ONLY runtime change is the `currency_unconvertible`
  context label in `metricsStateLabel` (page.tsx): it previously asserted "non-USD accounts excluded…", but now that an
  UNKNOWN-currency account can drive that state the label is made denomination-neutral ("accounts whose realized P&L
  can't be shown in USD are excluded" / "realized P&L can't be shown in USD", EN + JA) so it never mislabels an
  unestablished-currency account as "non-USD". A dedicated test (`dashboard/lifetime-metrics-coverage.test.tsx`) plus
  the updated `summary-row-stability` assertions lock this.
- **No migration, no flag, no schema/DB change, no backfill, no snapshot fabrication, no broker/MT5/host contact.**

## Consequences

- An account whose currency cannot be established is excluded from the lifetime USD aggregate: its per-account net P&L
  reads "—" and the portfolio metrics read `N/A` + "Unavailable", never a non-USD native figure as dollars and never a
  false `$0.00`. A mixed portfolio measures the USD subset honestly as PARTIAL with a truthful excluded count.
- A future reader must keep `unknown_currency` (currency not established) distinct from `rate_unavailable` (currency
  known, no vetted rate) and `mixed_trade_currency` (trade legs span denominations) — all three exclude.
- Still native and structurally immune (documented in KNOWN_ISSUES): the per-account trade-history and
  `/api/analytics/daily-pnl/` series are single-account NATIVE figures (not USD-aggregated across accounts). The
  broker-server-calendar-day window shared with the daily aggregate remains a separate platform-wide follow-up.
- Zero-activity precision: the lifetime path mirrors the daily path's currency RESOLUTION
  (`resolve_position_currency`), not its per-day zero-activity windowing. An account whose currency cannot be
  established and that has ZERO closed trades now contributes to PARTIAL/UNAVAILABLE rather than the pre-fix OK/0 — the
  same way a zero-trade KNOWN non-USD account already does, and consistent with the balance/equity aggregate that
  already excludes the same unobservable account (its live balance also failed). The frontend shows `N/A` tiles either
  way, never a false `$0.00`. See the dispositions below.

## Review dispositions (8-lens adversarial review)

No CRITICAL/HIGH findings. Actioned in this packet:

- **FIXED — fx-provenance (LOW).** `_single_currency_rate` now fails closed on the `UNKNOWN_CURRENCY` sentinel at the
  conversion boundary, so a homogeneous unestablished-currency scope (every account fell back to a fresh snapshot with a
  blank currency → `_equity_snapshot_fallback` sets `"UNKNOWN"`) can never hand `"UNKNOWN"→USD` to the rate source. A
  positive-control test (a rate source that WOULD quote the sentinel) proves the guard is real.
- **FIXED — frontend-truthfulness (MEDIUM).** The `currency_unconvertible` metrics label is denomination-neutral (above)
  so it never calls an unestablished-currency account "non-USD".
- **FIXED — scope (LOW).** The per-account `net_pnl` comment no longer overstates byte-identicality (a whitespace-only
  observed currency now resolves to the verified model currency instead of defaulting to USD — a strict improvement).

Dispositioned (documented, intentionally NOT changed in this narrow packet):

- **backward-compatibility (MEDIUM).** The zero-activity behaviour above: a zero-closed-trade unestablished-currency
  account now contributes to PARTIAL/UNAVAILABLE instead of OK/0. Consistent with the balance/equity aggregate and with
  the existing treatment of a zero-trade KNOWN non-USD account; the pre-fix OK/0 was a consequence of the blank→USD
  defect. The frontend renders `N/A` either way (never a false figure).
- **financial-correctness (MEDIUM) / mixed-currency-arithmetic (LOW).** The ADR-0066 single-currency CONVERSION path (a
  KNOWN non-USD account's `account_net_pnl` non-USD branch and `portfolio_metrics`' single-currency branch) lacks the
  mixed-trade-currency guard that the USD and daily paths apply, so a KNOWN-currency account whose trade legs span
  denominations would fold a cross-currency native sum before converting. This is PRE-EXISTING (not introduced or
  worsened by this diff), LATENT in production (MT5 ingest populates all three per-leg currency fields from the single
  account deposit currency, so divergent legs do not arise from the live path), and OUTSIDE the unknown-currency defect
  this packet targets. Recorded as a separate follow-up in KNOWN_ISSUES; the guard is satisfied for the three paths the
  defect concerns (USD path, daily path, unknown-currency withholding).
- **frontend-truthfulness (LOW).** An all-unknown portfolio that HAS closed trades reads "Insufficient data" / "No
  closed-trade history yet" (driven by `total_trades == null` in UNAVAILABLE). The figure is never false (`N/A`, not
  `$0.00`) and the state label is accurate; reason-aware trend copy is a minor polish follow-up.

/** Dashboard — LIFETIME performance-metric currency integrity (GFX-163, ADR-0122).
 *
 * The backend now EXCLUDES an account whose denomination could not be established from any authoritative
 * tenant-local source from the USD lifetime aggregate (``portfolio_metrics``) and withholds its per-account net P&L,
 * so the dashboard must render the resulting states truthfully with NO frontend runtime change:
 *   - all accounts unknown  -> metrics state UNAVAILABLE (``net_pnl_total``/``total_trades`` null): the performance
 *     strip reads N/A + an "Unavailable" context line, NEVER a measured "$0.00" and NEVER the native figure as USD;
 *   - a known-USD account mixed with an unknown one -> metrics state PARTIAL: the strip shows the MEASURED USD net
 *     P&L of the USD subset + an honest "N included · M excluded" line, and never folds the unknown native figure in.
 *
 * These lock the existing component's handling of the GFX-163 backend contract (the same null-money / state-label
 * machinery GFX-161 relies on for the daily card). No runtime dashboard code is changed by this packet.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, waitFor } from "@testing-library/react";
import type { PortfolioSummary } from "@/types/portfolio";

const { apiFetch, locale } = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  locale: { value: "en" as "en" | "ja" },
}));
vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/components/AppShell", () => ({ useLang: () => locale.value }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href, ...props }: any) => <a href={href} {...props}>{children}</a> }));

import DashboardPage from "./page";

// All accounts' denomination is UNKNOWN: real native trades exist but none can be denominated in USD, so the
// lifetime metrics are UNAVAILABLE (every monetary/classified field withheld — never a measured 0).
function summaryAllUnknown(): PortfolioSummary {
  return {
    reporting_currency: "USD", scope: "ALL", generated_at: new Date().toISOString(),
    account_count: 2, trading_count: 0, stale_accounts: [], accounts: [],
    aggregate: {
      balance_usd: { total_usd: null, basis: "PARTIAL", converted_count: 0, state: "UNAVAILABLE", reason: "missing_snapshot", included_count: 0, excluded_count: 2, unconverted: [] },
      equity_usd: { total_usd: null, basis: "PARTIAL", converted_count: 0, state: "UNAVAILABLE", reason: "missing_snapshot", included_count: 0, excluded_count: 2, unconverted: [] },
      open_pl_usd: { total_usd: null, basis: "PARTIAL", converted_count: 0, state: "UNAVAILABLE", reason: "stale_observation", included_count: 0, excluded_count: 2, unconverted: [] },
      daily_realized_pnl_usd: null, daily_realized_pnl_basis: "PARTIAL", daily_realized_pnl_state: "UNAVAILABLE",
      metrics: {
        total_trades: null, wins: null, losses: null, breakeven: null, win_rate_pct: null, profit_factor: null,
        profit_factor_infinite: false, expectancy: null, net_pnl_total: null, gross_profit: null, gross_loss: null,
        max_drawdown_money: null, breakeven_rule: "x", basis: "PARTIAL", excluded_accounts: [71, 72],
        state: "UNAVAILABLE", reason: "currency_unconvertible", included_count: 0, excluded_count: 2,
      },
    },
  };
}

// One known USD account summed, one UNKNOWN account excluded -> PARTIAL: a MEASURED USD figure over the USD subset,
// the unknown native (500) never folded in.
function summaryMixedUsdUnknown(): PortfolioSummary {
  return {
    reporting_currency: "USD", scope: "ALL", generated_at: new Date().toISOString(),
    account_count: 2, trading_count: 0, stale_accounts: [], accounts: [],
    aggregate: {
      balance_usd: { total_usd: 1000, basis: "PARTIAL", converted_count: 1, state: "PARTIAL", reason: "currency_unconvertible", included_count: 1, excluded_count: 1, unconverted: [] },
      equity_usd: { total_usd: 1000, basis: "PARTIAL", converted_count: 1, state: "PARTIAL", reason: "currency_unconvertible", included_count: 1, excluded_count: 1, unconverted: [] },
      open_pl_usd: { total_usd: 0, basis: "USD", converted_count: 0, state: "EMPTY", reason: null, included_count: 0, excluded_count: 0, unconverted: [] },
      daily_realized_pnl_usd: 10, daily_realized_pnl_basis: "PARTIAL", daily_realized_pnl_state: "PARTIAL",
      metrics: {
        total_trades: 1, wins: 1, losses: 0, breakeven: 0, win_rate_pct: 100, profit_factor: null,
        profit_factor_infinite: true, expectancy: 10, net_pnl_total: 10, gross_profit: 10, gross_loss: 0,
        max_drawdown_money: 0, breakeven_rule: "x", basis: "PARTIAL", excluded_accounts: [72],
        state: "PARTIAL", reason: "currency_unconvertible", included_count: 1, excluded_count: 1,
      },
    },
  };
}

function apiWith(summary: PortfolioSummary) {
  return (path: string): unknown => {
    if (path.startsWith("/api/analytics/portfolio/summary/")) return summary;
    if (path.startsWith("/api/analytics/portfolio/open-trades/")) return { reporting_currency: "USD", scope: "ALL", generated_at: "", count: 0, truncated: false, open_pl_usd: { total_usd: 0, basis: "USD", converted_count: 0, unconverted: [] }, stale_accounts: [], trades: [] };
    if (path.startsWith("/api/analytics/portfolio/equity-curve/")) return { state: "OK", count: 0, points: [] };
    if (path.startsWith("/api/reliability/trading-health")) return { ok: true, state: "UNKNOWN", can_trade: false };
    if (path.startsWith("/api/strategies/strategies/")) return [];
    if (path.startsWith("/api/trading/accounts/")) return [];
    if (path.startsWith("/api/auth/me/")) return { first_name: "Trader" };
    if (path.startsWith("/api/backtests/feature-attribution/")) return { ok: true, normalisation_attribution: {} };
    if (path.startsWith("/api/backtests/strategy-selection/")) return { ok: false };
    if (path.startsWith("/api/onboarding/account-status/")) return { overall: "HEALTHY", stages: [] };
    if (path.startsWith("/api/analytics/trade-history/")) return { mt5_balance_current: 200, mt5_equity_current: 200, currency: "USD", observed_stats: { total_trades: 0, win_rate_pct: 0, max_drawdown_pct: 0, net_pnl_total: 0 }, balance_series: [] };
    if (path.startsWith("/api/analytics/daily-pnl/")) return { series: [] };
    return {};
  };
}

const lsStore = new Map<string, string>();
beforeEach(() => {
  locale.value = "en";
  apiFetch.mockReset();
  lsStore.clear();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => (lsStore.has(k) ? lsStore.get(k)! : null),
      setItem: (k: string, v: string) => { lsStore.set(k, String(v)); },
      removeItem: (k: string) => { lsStore.delete(k); },
      clear: () => lsStore.clear(),
    },
  });
  window.localStorage.setItem("guvfx.dashboard.scope", "ALL");
});
afterEach(() => cleanup());

describe("Dashboard lifetime metrics — unknown-currency integrity (GFX-163)", () => {
  it("all-unknown portfolio reads UNAVAILABLE / N/A, never a measured figure in the lifetime strip", async () => {
    apiFetch.mockImplementation(async (p: string) => apiWith(summaryAllUnknown())(p));
    const { container } = render(<DashboardPage />);
    await waitFor(() => expect((container.textContent || "").includes("Unavailable")).toBe(true));
    const text = container.textContent || "";
    // The lifetime performance strip is explicitly UNAVAILABLE: every metric tile reads N/A (the withheld state),
    // NEVER a measured number denominated in USD. (The "$0.00" elsewhere on the page is the unrelated Open-Trades
    // floating-P/L panel — a genuine zero-open-positions state — so the strip-scoped tiles are what we assert.)
    expect(text).toContain("Unavailable — realized P&L can't be shown in USD (0 included · 2 excluded)");
    expect(text).toContain("Net PnLN/A");                 // lifetime net P&L withheld — never a native sum as USD
    expect(text).toContain("Win RateN/A");
    expect(text).toContain("Profit FactorN/A");
    expect(text).toContain("ExpectancyN/A");
    expect(text).toContain("Insufficient data");          // trend never a measured zero
  });

  it("known-USD + unknown reads PARTIAL with the MEASURED USD figure and an honest excluded count", async () => {
    apiFetch.mockImplementation(async (p: string) => apiWith(summaryMixedUsdUnknown())(p));
    const { container } = render(<DashboardPage />);
    await waitFor(() => expect((container.textContent || "").includes("$10.00")).toBe(true));
    const text = container.textContent || "";
    expect(text).toContain("Partial — accounts whose realized P&L can't be shown in USD are excluded");
    expect(text).toContain("1 included · 1 excluded");
    expect(text).not.toContain("$500");                   // the unknown native is NEVER folded into the USD figure
    expect(text).not.toContain("$510");                   // nor summed with the USD account
  });
});

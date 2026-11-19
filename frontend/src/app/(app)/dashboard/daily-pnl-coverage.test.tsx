/** Dashboard pkt-131 — Daily PnL (realized) coverage rendering.
 *
 * The ALL-scope Daily PnL card must render the honest per-account state the backend now produces:
 *   - OK        -> the measured USD figure, no "Partial" badge;
 *   - PARTIAL   -> the measured figure over the convertible accounts PLUS a "Partial · N excluded" badge whose
 *                  tooltip names each dropped account's NATIVE same-day realized (never a complete-looking number);
 *   - UNAVAILABLE -> "—", never a false "$0.00".
 *
 * Guards the exact defect: a GBP-only / mixed day must not show "+$0.00" as if complete.
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

type Agg = PortfolioSummary["aggregate"];

function summaryAll(dailyOverride: Partial<Agg>): PortfolioSummary {
  const usdAgg = { total_usd: 1000, basis: "USD" as const, converted_count: 1, state: "OK", reason: null, included_count: 1, excluded_count: 0, unconverted: [] };
  return {
    reporting_currency: "USD", scope: "ALL", generated_at: new Date().toISOString(),
    account_count: 2, trading_count: 0, stale_accounts: [], accounts: [],
    aggregate: {
      balance_usd: { ...usdAgg }, equity_usd: { ...usdAgg },
      open_pl_usd: { total_usd: 0, basis: "USD", converted_count: 0, state: "EMPTY", reason: null, included_count: 0, excluded_count: 0, unconverted: [] },
      daily_realized_pnl_usd: 0, daily_realized_pnl_basis: "USD",
      metrics: {
        total_trades: 10, wins: 6, losses: 4, breakeven: 0, win_rate_pct: 60, profit_factor: 1.5,
        profit_factor_infinite: false, expectancy: 1.0, net_pnl_total: 10.0, gross_profit: 30, gross_loss: 20,
        max_drawdown_money: 5, breakeven_rule: "x", basis: "USD", excluded_accounts: [],
        state: "OK", reason: null, included_count: 2, excluded_count: 0,
      },
      ...dailyOverride,
    } as Agg,
  };
}

function apiFor(summary: PortfolioSummary) {
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

function dailyCellText(): string {
  const el = screen.getByTestId("account-summary");
  const cell = Array.from(el.querySelectorAll("div")).find((d) => (d.textContent || "").includes("Daily PnL"));
  return cell?.textContent || "";
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

describe("Daily PnL card — pkt-131 per-account coverage", () => {
  it("OK: shows the measured USD figure with no Partial badge", async () => {
    apiFetch.mockImplementation(async (p: string) => apiFor(summaryAll({ daily_realized_pnl_usd: 35.6, daily_realized_pnl_state: "OK" }))(p));
    render(<DashboardPage />);
    await waitFor(() => expect(dailyCellText()).toContain("+$35.60"));
    expect(dailyCellText()).not.toContain("Partial");
  });

  it("PARTIAL: shows the figure PLUS a 'Partial · N excluded' badge naming the excluded native amount", async () => {
    apiFetch.mockImplementation(async (p: string) => apiFor(summaryAll({
      daily_realized_pnl_usd: 34.37, daily_realized_pnl_state: "PARTIAL",
      daily_realized_pnl_excluded_detail: [{ account_id: 52, native: 38.04, native_currency: "GBP", reason: "rate_unavailable" }],
    }))(p));
    const { container } = render(<DashboardPage />);
    await waitFor(() => expect(dailyCellText()).toContain("+$34.37"));
    expect(dailyCellText()).toContain("Partial · 1 excluded");
    // the dropped account's NATIVE same-day realized is disclosed in the tooltip (never silently hidden as $0)
    expect((container.innerHTML || "")).toContain("#52 +38.04 GBP");
  });

  it("UNAVAILABLE: shows '—', never a false $0.00", async () => {
    apiFetch.mockImplementation(async (p: string) => apiFor(summaryAll({
      daily_realized_pnl_usd: null, daily_realized_pnl_state: "UNAVAILABLE",
      daily_realized_pnl_excluded_detail: [{ account_id: 52, native: 38.04, native_currency: "GBP", reason: "rate_unavailable" }],
    }))(p));
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByTestId("account-summary")).toBeTruthy());
    const txt = dailyCellText();
    expect(txt).toContain("—");
    expect(txt).not.toContain("$0.00");
    expect(txt).not.toContain("+$0.00");
  });

  it("shows a measured daily figure even when the lifetime metric is UNAVAILABLE (no USD trade set)", async () => {
    // All-non-USD multi-currency scope: metrics.total_trades is null (lifetime hasHistory=false), but the daily
    // aggregate converted a non-USD account per-account and produced a measured PARTIAL figure — it must SHOW the
    // figure, not be hidden as "—" behind the lifetime gate (adversarial-review LOW; different aggregates).
    const metrics = {
      total_trades: null, wins: null, losses: null, breakeven: null, win_rate_pct: null, profit_factor: null,
      profit_factor_infinite: false, expectancy: null, net_pnl_total: null, gross_profit: null, gross_loss: null,
      max_drawdown_money: null, breakeven_rule: "x", basis: "PARTIAL", excluded_accounts: [],
      state: "UNAVAILABLE", reason: "currency_unconvertible", included_count: 0, excluded_count: 2,
    };
    apiFetch.mockImplementation(async (p: string) => apiFor(summaryAll({
      daily_realized_pnl_usd: 50.36, daily_realized_pnl_state: "PARTIAL",
      daily_realized_pnl_excluded_detail: [{ account_id: 60, native: 20.0, native_currency: "EUR", reason: "rate_unavailable" }],
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      metrics: metrics as any,
    }))(p));
    render(<DashboardPage />);
    await waitFor(() => expect(dailyCellText()).toContain("+$50.36"));   // measured figure shown despite no lifetime history
    expect(dailyCellText()).toContain("Partial · 1 excluded");
  });

  // ADR-0119 (GFX-161): an account whose currency could not be ESTABLISHED (unknown_currency) is excluded exactly
  // like a non-convertible one — its native figure surfaces labelled UNKNOWN, and the card never reads as a false $0.
  it("PARTIAL with an unknown-currency account: excluded (labelled UNKNOWN), measured figure over the rest", async () => {
    apiFetch.mockImplementation(async (p: string) => apiFor(summaryAll({
      daily_realized_pnl_usd: 8.0, daily_realized_pnl_state: "PARTIAL",
      daily_realized_pnl_excluded_detail: [{ account_id: 71, native: 100.0, native_currency: "UNKNOWN", reason: "unknown_currency" }],
    }))(p));
    const { container } = render(<DashboardPage />);
    await waitFor(() => expect(dailyCellText()).toContain("+$8.00"));
    expect(dailyCellText()).toContain("Partial · 1 excluded");
    expect((container.innerHTML || "")).toContain("#71 +100.00 UNKNOWN");   // native surfaced, never summed as USD
    expect(dailyCellText()).not.toContain("$108.00");                        // the unknown 100 is NEVER folded in
  });

  it("UNAVAILABLE when every account's currency is unknown: '—', never a false $0.00", async () => {
    apiFetch.mockImplementation(async (p: string) => apiFor(summaryAll({
      daily_realized_pnl_usd: null, daily_realized_pnl_state: "UNAVAILABLE",
      daily_realized_pnl_excluded_detail: [
        { account_id: 71, native: 100.0, native_currency: "UNKNOWN", reason: "unknown_currency" },
        { account_id: 72, native: 34.0, native_currency: "UNKNOWN", reason: "unknown_currency" },
      ],
    }))(p));
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByTestId("account-summary")).toBeTruthy());
    const txt = dailyCellText();
    expect(txt).toContain("—");
    expect(txt).not.toContain("$0.00");
    expect(txt).not.toContain("+$0.00");
  });
});

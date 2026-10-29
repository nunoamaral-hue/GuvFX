/** Dashboard P0 — Account Status Summary ROW STABILITY.
 *
 * Regression guard for the reported defect: the Trading / Equity / Daily PnL / Updated row changed position after
 * live values arrived / on the background refresh. These tests assert the STRUCTURAL invariants that make the row
 * stable (jsdom does no layout, so pixel position is held by the fixed CSS grid in globals.css — see the
 * `@media (min-width: 769px)` block; here we lock the invariants that CSS relies on):
 *   - the summary card is ALWAYS rendered (same `account-summary` wrapper) with EXACTLY its four cells, in the
 *     loading, populated, partial and error branches — no conditional wrapper swap or cell add/drop;
 *   - the Equity FX-disclosure slot is ALWAYS reserved at ALL scope, whether or not the `fx` block has arrived, so
 *     the cell's height does not grow when values arrive (the vertical jump);
 *   - a summary payload whose scope does not match the selected scope never drives the header (stale-scope race);
 *   - single-account scope omits the ALL-scope FX slot, so that cell's geometry is unchanged there;
 *   - the realized-metrics PARTIAL state is disclosed honestly (non-USD accounts excluded from realized
 *     performance), NOT as "currency conversion unavailable" (equity IS converted via the disclosed rate).
 *
 * Fixtures mirror the live support@ portfolio (6 accounts, 0 closed trades → metrics PARTIAL with the 2 GBP
 * accounts excluded; equity reconciles 6/6 via the BoE rate).
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

function summaryAll(): PortfolioSummary {
  return {
    reporting_currency: "USD", scope: "ALL", generated_at: new Date().toISOString(),
    account_count: 6, trading_count: 0, stale_accounts: [],
    accounts: [],
    aggregate: {
      balance_usd: { total_usd: 2631.07, basis: "USD", converted_count: 6, state: "OK", reason: null, included_count: 6, excluded_count: 0, unconverted: [] },
      equity_usd: { total_usd: 2831.07, basis: "USD", converted_count: 6, state: "OK", reason: null, included_count: 6, excluded_count: 0, unconverted: [] },
      open_pl_usd: { total_usd: 0, basis: "USD", converted_count: 0, state: "EMPTY", reason: null, included_count: 0, excluded_count: 0, unconverted: [] },
      daily_realized_pnl_usd: 0, daily_realized_pnl_basis: "PARTIAL",
      metrics: {
        total_trades: 0, wins: 0, losses: 0, breakeven: 0, win_rate_pct: 0, profit_factor: null,
        profit_factor_infinite: false, expectancy: 0, net_pnl_total: 0, gross_profit: 0, gross_loss: 0,
        max_drawdown_money: 0, breakeven_rule: "x", basis: "PARTIAL", excluded_accounts: [49, 52],
        state: "PARTIAL", reason: "currency_unconvertible", included_count: 4, excluded_count: 2,
      },
    },
    fx: {
      enabled: true, pair: "GBPUSD", available: true, source: "BOE", source_series: "XUDLUSS",
      direction: "USD_PER_GBP", as_of: "2026-10-27", rate: "1.3313", effective_date: "2026-10-27", age_days: 0,
    },
  };
}

// Minimal valid responses for the page's other boot reads (kept inert so the summary drives the assertions).
function defaultApiFetch(path: string): unknown {
  if (path.startsWith("/api/analytics/portfolio/summary/")) return summaryAll();
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
}

function cells(): number {
  const el = screen.getByTestId("account-summary");
  return Array.from(el.children).filter((c) => c.tagName === "DIV").length;
}

// Some jsdom builds expose a non-functional localStorage; install a Map-backed stub so scope control is
// deterministic (the page itself wraps every localStorage access in try/catch, so this only steadies the test).
const lsStore = new Map<string, string>();
beforeEach(() => {
  locale.value = "en";
  apiFetch.mockReset();
  apiFetch.mockImplementation(async (p: string) => defaultApiFetch(p));
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
  window.localStorage.setItem("guvfx.focus.symbol", "EURUSD");
  window.localStorage.setItem("guvfx.dashboard.scope", "ALL");
});
afterEach(() => cleanup());

describe("Dashboard summary row — stable geometry across states", () => {
  it("renders the 4-cell summary card + reserved FX slot while the summary is still loading", () => {
    // Summary never resolves: the loading branch. The card + its four cells + the reserved FX slot must already be
    // present (they are not gated on data), so nothing appears/disappears when data later arrives.
    apiFetch.mockImplementation((p: string) =>
      p.startsWith("/api/analytics/portfolio/summary/") ? new Promise(() => {}) : Promise.resolve(defaultApiFetch(p)));
    render(<DashboardPage />);
    expect(screen.getByTestId("account-summary")).toBeTruthy();
    expect(cells()).toBe(4);
    expect(screen.getByTestId("equity-fx-slot")).toBeTruthy();     // reserved even before the fx block arrives
  });

  it("keeps the same wrapper, 4 cells and FX slot once populated, and discloses the rate", async () => {
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByText("$2,831.07")).toBeTruthy());
    expect(cells()).toBe(4);                                        // unchanged from the loading render
    const fx = screen.getByTestId("equity-fx-slot");
    expect(fx).toBeTruthy();
    expect(fx.textContent || "").toContain("GBP→USD 1.3313");
    expect((fx.textContent || "")).toContain("2026-10-27");
  });

  it("discloses the realized-metrics PARTIAL honestly (not 'currency conversion unavailable')", async () => {
    const { container } = render(<DashboardPage />);
    await waitFor(() => expect(screen.getByText("$2,831.07")).toBeTruthy());
    const text = container.textContent || "";
    expect(text).toContain("Partial — non-USD accounts excluded from realized performance");
    expect(text).toContain("4 included · 2 excluded");
    // The equity tile DOES convert GBP (6/6), so the performance strip must not claim conversion is unavailable.
    expect(text).not.toContain("currency conversion unavailable");
  });

  it("keeps the 4-cell card + reserved FX slot when the ALL summary fails (never collapses the row)", async () => {
    apiFetch.mockImplementation((p: string) =>
      p.startsWith("/api/analytics/portfolio/summary/") ? Promise.reject(new Error("unreachable")) : Promise.resolve(defaultApiFetch(p)));
    const { container } = render(<DashboardPage />);
    await waitFor(() => expect((container.textContent || "").includes("Couldn't load")).toBe(true));
    expect(cells()).toBe(4);
    expect(screen.getByTestId("equity-fx-slot")).toBeTruthy();
  });

  it("never lets a stale-scope summary drive the header (scope-change race)", async () => {
    // Selected scope is ALL, but the summary resolves echoing a DIFFERENT scope ("999"): the header must not adopt
    // its equity — it stays on the pending placeholder, proving an out-of-order / wrong-scope response is ignored.
    apiFetch.mockImplementation((p: string) => {
      if (p.startsWith("/api/analytics/portfolio/summary/")) return Promise.resolve({ ...summaryAll(), scope: "999" });
      return Promise.resolve(defaultApiFetch(p));
    });
    render(<DashboardPage />);
    await waitFor(() => expect(apiFetch.mock.calls.some(([x]) => String(x).includes("/portfolio/summary/"))).toBe(true));
    await Promise.resolve();
    expect(screen.queryByText("$2,831.07")).toBeNull();            // wrong-scope payload never shown
    expect(cells()).toBe(4);                                       // geometry still intact
  });

  it("keeps the same stable geometry + honest JA disclosure in Japanese (no English leak in the row)", async () => {
    locale.value = "ja";
    const { container } = render(<DashboardPage />);
    await waitFor(() => expect(screen.getByText("$2,831.07")).toBeTruthy());
    expect(cells()).toBe(4);
    expect(screen.getByTestId("equity-fx-slot")).toBeTruthy();
    const text = container.textContent || "";
    expect(text).toContain("一部 — 非USD口座を確定実績から除外");          // JA honest metrics disclosure
    expect(text).not.toContain("excluded from realized performance");    // no English leak in the summary row
  });

  it("omits the ALL-scope FX slot in single-account scope (cell geometry unchanged there)", async () => {
    window.localStorage.setItem("guvfx.dashboard.scope", "50");
    render(<DashboardPage />);
    await waitFor(() => expect(apiFetch.mock.calls.some(([x]) => String(x).includes("account_id=") || String(x).includes("scope=50"))).toBe(true));
    expect(screen.getByTestId("account-summary")).toBeTruthy();
    expect(cells()).toBe(4);
    expect(screen.queryByTestId("equity-fx-slot")).toBeNull();     // no FX line ever in account scope
  });

  it("partial-FX state (rate unavailable) keeps the reserved slot + 4 cells and shows the honest line", async () => {
    // The fx block is present but NOT available (stale/missing rate). The reserved slot must still render (so the
    // cell height is unchanged) and show the honest "rate unavailable" line — never a fabricated conversion.
    apiFetch.mockImplementation((p: string) => {
      if (p.startsWith("/api/analytics/portfolio/summary/")) {
        const s = summaryAll();
        s.fx = { enabled: true, pair: "GBPUSD", available: false, source: "BOE", source_series: "XUDLUSS", direction: "USD_PER_GBP", as_of: "", reason: "no_rate_within_staleness" };
        return Promise.resolve(s);
      }
      return Promise.resolve(defaultApiFetch(p));
    });
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByText("$2,831.07")).toBeTruthy());
    expect(cells()).toBe(4);
    const fx = screen.getByTestId("equity-fx-slot");
    expect(fx.textContent || "").toContain("GBP→USD rate unavailable");
  });

  it("single GBP account scope renders native GBP, omits the FX slot, keeps 4 stable cells", async () => {
    window.localStorage.setItem("guvfx.dashboard.scope", "49");
    apiFetch.mockImplementation((p: string) => {
      if (p.startsWith("/api/trading/accounts/")) return Promise.resolve([{ id: 49, name: "Pepperstone", broker_name: "Pepperstone", account_number: "71490049", is_active: false }]);
      if (p.startsWith("/api/analytics/trade-history/")) return Promise.resolve({ mt5_balance_current: 514.21, mt5_equity_current: 514.21, currency: "GBP", observed_stats: { total_trades: 0, win_rate_pct: 0, max_drawdown_pct: 0, net_pnl_total: 0 }, balance_series: [] });
      return Promise.resolve(defaultApiFetch(p));
    });
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByText(/GBP\s*514\.21/)).toBeTruthy());   // native currency, not forced to USD
    expect(cells()).toBe(4);
    expect(screen.queryByTestId("equity-fx-slot")).toBeNull();
  });

  it("row structure is invariant to value magnitude (no content-driven add/remove across refreshes)", async () => {
    // A background refresh can bring a very different equity magnitude. The card must keep the SAME four cells and
    // the reserved FX slot regardless of value width — so the fixed grid never gains/loses structure and cannot
    // reflow. (Pixel-level before/after-refresh stability is proven separately by the CSS-geometry browser harness.)
    const { unmount } = render(<DashboardPage />);
    await waitFor(() => expect(screen.getByText("$2,831.07")).toBeTruthy());
    expect(cells()).toBe(4);
    expect(screen.getByTestId("equity-fx-slot")).toBeTruthy();
    unmount();
    apiFetch.mockImplementation((p: string) => {
      if (p.startsWith("/api/analytics/portfolio/summary/")) {
        const s = summaryAll();
        s.aggregate.equity_usd = { ...s.aggregate.equity_usd, total_usd: 12345678.9 };
        return Promise.resolve(s);
      }
      return Promise.resolve(defaultApiFetch(p));
    });
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByText("$12,345,678.90")).toBeTruthy());
    expect(cells()).toBe(4);                                       // same structure under a far larger value
    expect(screen.getByTestId("equity-fx-slot")).toBeTruthy();
  });
});

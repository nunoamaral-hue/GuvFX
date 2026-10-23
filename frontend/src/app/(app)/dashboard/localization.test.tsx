import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

const { apiFetch, locale, defaultApiFetch } = vi.hoisted(() => {
  const locale = { value: "ja" as "en" | "ja" };
  const defaultApiFetch = async (path: string) => {
    if (path.startsWith("/api/reliability/trading-health")) return { ok: true, state: "HEALTHY", can_trade: true };
    if (path.startsWith("/api/strategies/strategies/")) return [{ id: 12, name: "Wayond WIM Strategy", symbol_universe: "XAUUSD", is_active: true }];
    if (path.startsWith("/api/trading/accounts/")) return [{ id: 28, name: "Hosted Workspace", broker_name: "Hosted Workspace", account_number: "1302575", is_active: true }];
    if (path.startsWith("/api/strategies/assignments/")) return [{ strategy_id: 12, account_id: 28, stage: "LIVE" }];
    if (path.startsWith("/api/backtests/feature-attribution/")) return { ok: true, normalisation_attribution: {} };
    if (path.startsWith("/api/auth/me/")) return { first_name: "Beta" };
    if (path.startsWith("/api/onboarding/account-status/")) return { overall: "HEALTHY", stages: [] };
    // The strategy proper-noun ("Wayond WIM Strategy") now appears as Open Trades per-position attribution (the old
    // "Your Strategies" card was replaced by the Open Trades panel). One open position keeps this test's proper-noun
    // passthrough coverage in the new UI: it must stay English even in the JA view.
    if (path.startsWith("/api/analytics/portfolio/open-trades/")) return {
      reporting_currency: "USD", scope: "28", generated_at: new Date().toISOString(), count: 1, truncated: false,
      open_pl_usd: { total_usd: 12.45, basis: "USD", converted_count: 1, unconverted: [] },
      stale_accounts: [],
      trades: [{
        position_id: "28:100", ticket: 100, account_id: 28, broker: "Taurex", account_masked: "••••2575",
        symbol: "XAUUSD", side: "BUY", volume: 0.01, open_price: 2400, current_price: 2412,
        pl_native: 12.45, pl_native_currency: "USD", pl_usd: 12.45, strategy: "Wayond WIM Strategy",
      }],
    };
    if (path.startsWith("/api/analytics/trade-history/")) return {
      mt5_balance_current: 49994.55,
      mt5_equity_current: 49994.55,
      currency: "USD",
      observed_stats: { total_trades: 18, win_rate_pct: 50, max_drawdown_pct: 0.1, net_pnl_total: -5.45, wins: 9, losses: 9 },
      balance_series: [{ balance_after_trade: 50000, net_pnl_money: 0 }, { balance_after_trade: 49994.55, net_pnl_money: -5.45 }],
    };
    if (path.startsWith("/api/analytics/daily-pnl/")) {
      if (path.includes("strategy_id=")) return { totals: { net_pnl: -5.45, win_rate: 50, trades: 18 } };
      return { series: [], totals: { net_pnl: -5.45, win_rate: 50, trades: 18 } };
    }
    if (path.startsWith("/api/backtests/strategy-selection/")) return {
      ok: true,
      market_state: {
        current_state: "RANGE_COMPRESSION",
        confidence: "MEDIUM",
        supporting_evidence: ["Uncatalogued backend evidence sentence"],
        context: { trend_state: "sideways", news_impact: "NONE" },
      },
      preferred_families: [{ family: "BREAKOUT", label: "Breakout", suitability: "HIGH" }],
      preferred_strategies: [{ name: "ATR Breakout", family: "BREAKOUT", suitability: "HIGH", kb_avg_quality: 63, kb_observations: 3 }],
      warnings: [],
    };
    return {};
  };
  return { locale, defaultApiFetch, apiFetch: vi.fn(defaultApiFetch) };
});

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/components/AppShell", () => ({ useLang: () => locale.value }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href, ...props }: any) => <a href={href} {...props}>{children}</a> }));

import DashboardPage from "./page";

describe("Dashboard authenticated EN/JA presentation", () => {
  beforeEach(() => {
    apiFetch.mockClear();
    apiFetch.mockImplementation(defaultApiFetch);   // restore the default so a per-test override never leaks
    window.localStorage.clear();
    window.localStorage.setItem("guvfx.focus.symbol", "XAUUSD");
    // View the specific account (not the default ALL-portfolio scope) so the per-account identity + metrics render;
    // the ALL-scope portfolio view is covered by analytics.tests_portfolio + OpenTradesPanel.test.tsx.
    window.localStorage.setItem("guvfx.dashboard.scope", "28");
  });
  afterEach(() => cleanup());

  it("renders structured research and customer account copy coherently in Japanese", async () => {
    locale.value = "ja";
    const { container } = render(<DashboardPage />);

    await screen.findByText("ホステッドワークスペース · 1302575");
    await screen.findByText(/注目: ATR Breakout/);
    await screen.findAllByText("中程度の信頼度");
    // Open Trades resolves asynchronously; wait for its strategy attribution before snapshotting the DOM.
    await waitFor(() => expect(container.textContent || "").toContain("Wayond WIM Strategy"));

    const text = container.textContent || "";
    expect(text).toContain("買い手と売り手が拮抗し、価格は明確に抜けるよりも既存のレンジ内で推移する時間が長くなっています。");
    expect(text).toContain("現時点では方向よりも確認を重視してください。");
    expect(text).toContain("類似する過去の観測3件に基づきます（過去データの平均品質 63%）。");
    expect(text).toContain("Wayond WIM Strategy");
    expect(text).toContain("ATR Breakout");
    expect(text).toContain("XAUUSD");

    for (const fragment of [
      "Hosted Workspace · Hosted Workspace",
      "Focus:",
      "Buyers and sellers are balanced",
      "Confirmation matters more than direction",
      "Based on 3 similar historical observations",
    ]) expect(text).not.toContain(fragment);

    const longEnglishPhrases = text.match(/\b[A-Za-z]{2,}(?:[\s,.—'/-]+[A-Za-z]{2,}){2,}\b/g) || [];
    expect(longEnglishPhrases.filter((phrase) => phrase.trim() !== "Wayond WIM Strategy")).toEqual([]);
  });

  it("retains detailed natural English and the same account identity", async () => {
    locale.value = "en";
    const { container } = render(<DashboardPage />);

    await screen.findByText("Hosted Workspace · 1302575");
    await screen.findByText(/Focus: ATR Breakout/);
    await waitFor(() => expect(container.textContent).toContain("Buyers and sellers are balanced and price is spending more time inside established ranges than breaking away from them."));
    expect(container.textContent).toContain("Based on 3 similar historical observations · average past quality 63%.");
    expect(apiFetch.mock.calls.some(([path]) => String(path).includes("account_id=28"))).toBe(true);
  });

  it("shows an honest 'Couldn't load' when the ALL-portfolio summary fails (never an indefinite blank —)", async () => {
    // Pre-existing defect the honest UI exposed: a failed/timed-out ALL summary left `portfolio` null — identical
    // to still-pending — so equity/daily/status sat on "—" forever. The summaryError flag distinguishes failed from
    // pending and renders an explicit "Couldn't load" (no fabricated zero).
    locale.value = "en";
    window.localStorage.setItem("guvfx.dashboard.scope", "ALL");
    apiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/analytics/portfolio/summary/")) throw new Error("network_unreachable");
      return defaultApiFetch(path);
    });
    const { container } = render(<DashboardPage />);
    await waitFor(() => expect(container.textContent || "").toContain("Couldn't load"));
  });

  it("settles the Performance panel to a failed state — never an indefinite 'Loading…' — when the ALL summary fails", async () => {
    // The Performance Trend panel had NO failed state: on a failed ALL summary its label fell to "Insufficient
    // data" and its subline to "Loading…" indefinitely (Sponsor: performance must never remain on Loading…). The
    // fix settles both to "Couldn't load". Discriminating: the pre-fix states below must be ABSENT after settle.
    locale.value = "en";
    window.localStorage.setItem("guvfx.dashboard.scope", "ALL");
    apiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/analytics/portfolio/summary/")) throw new Error("network_unreachable");
      return defaultApiFetch(path);
    });
    const { container } = render(<DashboardPage />);
    await waitFor(() => expect(container.textContent || "").toContain("Couldn't load"));
    const text = container.textContent || "";
    expect(text).not.toContain("Insufficient data");   // pre-fix trend label on summary failure
    expect(text).not.toContain("Loading…");             // pre-fix performance subline (indefinite)
  });

  it("renders healthy accounts from a VALID partial ALL summary (a partial is not a total failure)", async () => {
    // One unavailable account (46, stale) + two healthy (48 USD, 49 GBP): the summary returns a measured PARTIAL,
    // which must render the healthy portfolio (Monitoring only + account mix + a measured USD equity), NEVER the
    // blanket "Couldn't load" that hides healthy accounts.
    locale.value = "en";
    window.localStorage.setItem("guvfx.dashboard.scope", "ALL");
    apiFetch.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/analytics/portfolio/summary/")) return {
        reporting_currency: "USD", scope: "ALL", generated_at: new Date().toISOString(),
        account_count: 3, trading_count: 0, stale_accounts: [46],
        accounts: [
          { account_id: 48, broker: "IS6FX", account_masked: "••••", currency: "USD", is_active: true, trading_state: { state: "PREPARING", label: "Preparing" }, balance_native: 100, equity_native: 100, net_pnl: 0, open_count: 0, open_pl_usd: null, stale: false },
          { account_id: 49, broker: "Pepperstone", account_masked: "••••", currency: "GBP", is_active: true, trading_state: { state: "PREPARING", label: "Preparing" }, balance_native: 400, equity_native: 446.19, net_pnl: 0, open_count: 0, open_pl_usd: null, stale: false },
          { account_id: 46, broker: "TradersWay", account_masked: "••••", currency: "USD", is_active: true, trading_state: { state: "PREPARING", label: "Preparing" }, balance_native: null, equity_native: null, net_pnl: null, open_count: 0, open_pl_usd: null, stale: true },
        ],
        aggregate: {
          balance_usd: { total_usd: 100, state: "PARTIAL", basis: "PARTIAL", included_count: 1, excluded_count: 2, converted_count: 1, unconverted: [] },
          equity_usd: { total_usd: 100, state: "PARTIAL", basis: "PARTIAL", included_count: 1, excluded_count: 2, converted_count: 1, unconverted: [] },
          open_pl_usd: { total_usd: 0, state: "OK", basis: "USD", converted_count: 0, unconverted: [] },
          daily_realized_pnl_usd: 0, daily_realized_pnl_basis: "USD",
          metrics: { state: "OK", basis: "USD", net_pnl_total: 0, win_rate_pct: null, wins: null, losses: null, total_trades: 0, profit_factor: null, profit_factor_infinite: false, expectancy: null, max_drawdown_money: null, included_count: null, excluded_count: null },
        },
      };
      return defaultApiFetch(path);
    });
    const { container } = render(<DashboardPage />);
    // The portfolio mix + a MEASURED partial USD equity render from the valid partial (independent of status).
    await waitFor(() => expect(container.textContent || "").toContain("3 broker accounts"));
    const text = container.textContent || "";
    expect(text).not.toContain("Couldn't load");    // a valid partial is not a failure
    expect(text).toContain("$100.00");               // healthy USD account's equity contributes (PARTIAL, measured)
  });
});

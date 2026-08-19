import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

const { apiFetch, locale } = vi.hoisted(() => ({
  locale: { value: "ja" as "en" | "ja" },
  apiFetch: vi.fn(async (path: string) => {
    if (path.startsWith("/api/reliability/trading-health")) return { ok: true, state: "HEALTHY", can_trade: true };
    if (path.startsWith("/api/strategies/strategies/")) return [{ id: 12, name: "Wayond WIM Strategy", symbol_universe: "XAUUSD", is_active: true }];
    if (path.startsWith("/api/trading/accounts/")) return [{ id: 28, name: "Hosted Workspace", broker_name: "Hosted Workspace", account_number: "1302575", is_active: true }];
    if (path.startsWith("/api/strategies/assignments/")) return [{ strategy_id: 12, account_id: 28, stage: "LIVE" }];
    if (path.startsWith("/api/backtests/feature-attribution/")) return { ok: true, normalisation_attribution: {} };
    if (path.startsWith("/api/auth/me/")) return { first_name: "Beta" };
    if (path.startsWith("/api/onboarding/account-status/")) return { overall: "HEALTHY", stages: [] };
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
  }),
}));

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/components/AppShell", () => ({ useLang: () => locale.value }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href, ...props }: any) => <a href={href} {...props}>{children}</a> }));

import DashboardPage from "./page";

describe("Dashboard authenticated EN/JA presentation", () => {
  beforeEach(() => {
    apiFetch.mockClear();
    window.localStorage.clear();
    window.localStorage.setItem("guvfx.focus.symbol", "XAUUSD");
  });
  afterEach(() => cleanup());

  it("renders structured research and customer account copy coherently in Japanese", async () => {
    locale.value = "ja";
    const { container } = render(<DashboardPage />);

    await screen.findByText("ホステッドワークスペース · 1302575");
    await screen.findByText(/注目: ATR Breakout/);
    await screen.findAllByText("中程度の信頼度");

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
});

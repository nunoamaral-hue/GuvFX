import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

const { apiFetch, locale } = vi.hoisted(() => ({
  locale: { value: "ja" as "en" | "ja" },
  apiFetch: vi.fn(async (path: string) => {
    const url = String(path || "");
    if (url === "/api/trading/accounts/") return [{
      id: 28,
      name: "Hosted Workspace",
      broker_name: "Hosted Workspace",
      account_number: "1302575",
      is_demo: true,
      is_active: true,
      created_at: "2026-08-01T00:00:00Z",
      updated_at: "2026-08-01T00:00:00Z",
    }];
    if (url.startsWith("/api/analytics/strategy-metrics/?account=")) return {
      account_id: 28,
      account_number: "1302575",
      strategies: [{
        strategy_name: "Wayond WIM Strategy",
        trades: 0,
        net_pnl: 0,
        wins: 0,
        losses: 0,
        win_rate_pct: 0,
        assigned: true,
        has_attributed_trades: false,
      }],
    };
    return {};
  }),
}));

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/components/AppShell", () => ({ useLang: () => locale.value }));

import StrategyMetricsPage from "./page";

describe("Strategy Metrics authenticated EN/JA presentation", () => {
  beforeEach(() => apiFetch.mockClear());
  afterEach(() => cleanup());

  it("renders the owned account and assigned empty state without unexplained English in Japanese", async () => {
    locale.value = "ja";
    const { container } = render(<StrategyMetricsPage />);

    await screen.findByText("ホステッドワークスペース · 1302575");
    await screen.findByText("Wayond WIM Strategy");
    expect(screen.getByText("有効")).toBeInTheDocument();
    expect(screen.getByText("この戦略に紐づく取引はまだありません")).toBeInTheDocument();

    const text = container.textContent || "";
    for (const fragment of [
      "Performance by strategy for your connected trading account",
      "Trading account",
      "Strategies",
      "No attributed trades yet",
      "Enabled",
      "Hosted Workspace",
    ]) expect(text).not.toContain(fragment);
    expect(text).toContain("1302575");
    expect(text).toContain("Wayond WIM Strategy");
    const longEnglishPhrases = text.match(/\b[A-Za-z]{2,}(?:[\s,.—'/-]+[A-Za-z]{2,}){2,}\b/g) || [];
    expect(longEnglishPhrases.filter((phrase) => phrase.trim() !== "Wayond WIM Strategy")).toEqual([]);
  });

  it("keeps natural English and locale rerenders presentation-only for the same account", async () => {
    locale.value = "en";
    const view = render(<StrategyMetricsPage />);
    await screen.findByText("Hosted Workspace · 1302575");
    expect(await screen.findByText("No attributed trades yet")).toBeInTheDocument();

    const metricsCallsBefore = apiFetch.mock.calls.filter(([path]) => String(path).includes("strategy-metrics/?account=28")).length;
    locale.value = "ja";
    view.rerender(<StrategyMetricsPage />);
    await waitFor(() => expect(screen.getByText("ホステッドワークスペース · 1302575")).toBeInTheDocument());

    expect(apiFetch.mock.calls.filter(([path]) => String(path).includes("strategy-metrics/?account=28")).length).toBe(metricsCallsBefore);
    expect(apiFetch.mock.calls.every(([, options]) => !options || !(options as RequestInit).method || (options as RequestInit).method === "GET")).toBe(true);
  });
});

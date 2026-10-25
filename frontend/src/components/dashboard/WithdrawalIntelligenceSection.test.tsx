/** WithdrawalIntelligenceSection — the cohesive dashboard presentation of the withdrawal surface. Composition only:
 * it gates the whole section from ONE metrics probe (DARK 404 => render nothing, no empty frame), shows the summary +
 * recent history side by side when live, a single honest empty state at total 0, EN/JA parity, and a "View all" link
 * to the detail page. It must NOT re-fetch metrics for the embedded summary (it injects the already-fetched payload). */
import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, within } from "@testing-library/react";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a>,
}));

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { WithdrawalIntelligenceSection } from "@/components/dashboard/WithdrawalIntelligenceSection";
import type { WithdrawalMetrics, WithdrawalHistory } from "@/types/withdrawals";

const METRICS = "/api/broker-intelligence/withdrawals/metrics/";
const HISTORY = "/api/broker-intelligence/withdrawals/history/";

function metrics(over: Partial<WithdrawalMetrics> = {}): WithdrawalMetrics {
  return {
    total: 1, by_status: {}, pending_count: 0, completed_count: 1, failed_count: 0, unresolved_count: 0,
    completed_amount_by_currency: { USD: "24.12" }, completed_amount_known_count: 1,
    processing_duration: { count: 1, avg_seconds: 1981, median_seconds: 1981, min_seconds: 1981, max_seconds: 1981 },
    genuine_count: 1, sample_caveat: true, ...over,
  };
}
function history(over: Partial<WithdrawalHistory> = {}): WithdrawalHistory {
  return {
    withdrawals: [
      { id: 1, broker: "TradersWay", trading_account_id: 46, account_masked: "••••0123", state: "completed",
        status: "COMPLETED", amount: "24.12", currency: "USD", requested_at: "2026-10-19T07:55:04Z",
        completed_at: "2026-10-19T08:28:05Z", duration_seconds: 1981, broker_reference_id: "7654321",
        reference_correlated: true },
    ],
    brokers: ["TradersWay"], accounts: [{ id: 46, broker: "TradersWay", account_masked: "••••0123" }], ...over,
  };
}

// Route apiFetch by endpoint so the section (metrics) and the embedded history (history) each resolve independently.
function route(metricsRes: unknown, historyRes: unknown = history()) {
  api.apiFetch.mockImplementation((url: unknown) => {
    const u = String(url);
    if (u.startsWith(METRICS)) return metricsRes instanceof Error ? Promise.reject(metricsRes) : Promise.resolve(metricsRes);
    if (u.startsWith(HISTORY)) return historyRes instanceof Error ? Promise.reject(historyRes) : Promise.resolve(historyRes);
    return Promise.reject(new Error("unexpected url " + u));
  });
}

beforeEach(() => { api.apiFetch.mockReset(); });
afterEach(() => { cleanup(); });

describe("WithdrawalIntelligenceSection", () => {
  it("self-hides on the DARK 404 (renders nothing — no empty frame)", async () => {
    route(Object.assign(new Error("Not found."), { status: 404, httpStatus: 404 }));
    const { container } = render(<WithdrawalIntelligenceSection />);
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("withdrawal-intelligence-section")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("self-hides on a non-conforming metrics payload (never a broken frame)", async () => {
    route({} as unknown);
    const { container } = render(<WithdrawalIntelligenceSection />);
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("withdrawal-intelligence-section")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("renders ONE cohesive section: summary + recent history, with a View all link to the detail page", async () => {
    route(metrics(), history());
    render(<WithdrawalIntelligenceSection />);
    const section = await screen.findByTestId("withdrawal-intelligence-section");
    expect(within(section).getByRole("heading", { name: /Withdrawal Intelligence/ })).toBeTruthy();
    // summary facts come from the injected metrics (left column)
    expect(section.textContent).toMatch(/In progress/);
    expect(section.textContent).toMatch(/Completed/);
    expect(section.textContent).toMatch(/24\.12 USD/);
    // recent history row (right column)
    await waitFor(() => expect(within(section).getAllByTestId("history-row").length).toBeGreaterThan(0));
    // "View all" -> permanent detail page
    const link = within(section).getByRole("link", { name: /View all/ });
    expect(link.getAttribute("href")).toBe("/withdrawal-intelligence");
    // the metrics endpoint is fetched EXACTLY once (the summary reuses the section's payload; no duplicate request)
    const metricCalls = api.apiFetch.mock.calls.filter((c) => String(c[0]).startsWith(METRICS));
    expect(metricCalls.length).toBe(1);
  });

  it("keeps the processing-speed metric and the single-sample disclosure visible", async () => {
    route(metrics(), history());
    render(<WithdrawalIntelligenceSection />);
    const section = await screen.findByTestId("withdrawal-intelligence-section");
    expect(section.textContent).toMatch(/33 min/);                        // 1981s observed processing time
    expect(within(section).getByTestId("withdrawals-sample-caveat")).toBeTruthy();   // disclosure present, not hidden
  });

  it("shows a single honest empty state at total 0 (no columns, no duplicate messages)", async () => {
    route(metrics({ total: 0, pending_count: 0, completed_count: 0, failed_count: 0,
      completed_amount_by_currency: {}, processing_duration: null, genuine_count: 0, sample_caveat: false }),
      history({ withdrawals: [], brokers: [], accounts: [] }));
    render(<WithdrawalIntelligenceSection />);
    const section = await screen.findByTestId("withdrawal-intelligence-section");
    expect(within(section).getByTestId("wi-section-empty").textContent).toMatch(/No withdrawals observed yet/);
    expect(within(section).queryByTestId("history-row")).toBeNull();
    const matches = section.textContent?.match(/No withdrawals observed yet/g) || [];
    expect(matches.length).toBe(1);   // exactly one — never duplicated across panel + history
  });

  it("localizes the section (JA): title and View all", async () => {
    route(metrics(), history());
    render(<WithdrawalIntelligenceSection lang="ja" />);
    const section = await screen.findByTestId("withdrawal-intelligence-section");
    expect(within(section).getByRole("heading", { name: /出金インテリジェンス/ })).toBeTruthy();
    expect(within(section).getByRole("link", { name: /すべて表示/ }).getAttribute("href")).toBe("/withdrawal-intelligence");
  });
});

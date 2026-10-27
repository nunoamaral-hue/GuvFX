/** WithdrawalIntelligenceSection — the cohesive dashboard presentation of the withdrawal surface. Composition only:
 * it gates the whole section from ONE metrics probe (DARK 404 => render nothing, no empty frame), shows the summary +
 * recent history side by side when live, a single honest empty state at total 0, EN/JA parity, and a "View all" link
 * to the detail page. It must NOT re-fetch metrics for the embedded summary (it injects the already-fetched payload). */
import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, within, act } from "@testing-library/react";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a>,
}));

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { WithdrawalIntelligenceSection } from "@/components/dashboard/WithdrawalIntelligenceSection";
import type { WithdrawalMetrics, WithdrawalHistory, WithdrawalRow } from "@/types/withdrawals";

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

  // ── Account-scope correction: follow the dashboard selector ──
  function emptyMetrics() {
    return metrics({ total: 0, pending_count: 0, completed_count: 0, failed_count: 0,
      completed_amount_by_currency: {}, processing_duration: null, genuine_count: 0, sample_caveat: false });
  }
  function row(over: Partial<WithdrawalRow> = {}): WithdrawalRow {
    return { id: 1, broker: "TradersWay", trading_account_id: 46, account_masked: "••••0123", state: "completed",
      status: "COMPLETED", amount: "24.12", currency: "USD", requested_at: "2026-10-19T07:55:04Z",
      completed_at: "2026-10-19T08:28:04Z", duration_seconds: 1980, broker_reference_id: "ref", reference_correlated: true,
      ...over };
  }

  it("scopes BOTH metrics and history to the selected account id", async () => {
    const calls: string[] = [];
    api.apiFetch.mockImplementation((url: unknown) => {
      const u = String(url); calls.push(u);
      if (u.startsWith(METRICS)) return Promise.resolve(metrics());
      if (u.startsWith(HISTORY)) return Promise.resolve(history());
      return Promise.reject(new Error("x"));
    });
    render(<WithdrawalIntelligenceSection scope="49" />);
    await screen.findByTestId("withdrawal-intelligence-section");
    expect(calls.some((u) => u.startsWith(METRICS) && u.includes("account_id=49"))).toBe(true);
    expect(calls.some((u) => u.startsWith(HISTORY) && u.includes("account_id=49"))).toBe(true);
  });

  it("owner-wide (ALL) sends NO account_id", async () => {
    const calls: string[] = [];
    api.apiFetch.mockImplementation((url: unknown) => {
      const u = String(url); calls.push(u);
      if (u.startsWith(METRICS)) return Promise.resolve(metrics());
      if (u.startsWith(HISTORY)) return Promise.resolve(history());
      return Promise.reject(new Error("x"));
    });
    render(<WithdrawalIntelligenceSection scope="ALL" />);
    await screen.findByTestId("withdrawal-intelligence-section");
    expect(calls.every((u) => !u.includes("account_id="))).toBe(true);
  });

  it("selected account with no withdrawals shows the SCOPED empty message, not owner-wide data (EN)", async () => {
    route(emptyMetrics(), history({ withdrawals: [], brokers: [], accounts: [] }));
    render(<WithdrawalIntelligenceSection scope="49" />);
    const section = await screen.findByTestId("withdrawal-intelligence-section");
    expect(within(section).getByTestId("wi-section-empty").textContent).toMatch(/No withdrawals observed for this account\./);
    expect(within(section).queryByTestId("history-row")).toBeNull();
  });

  it("scoped empty message is localized (JA)", async () => {
    route(emptyMetrics(), history({ withdrawals: [], brokers: [], accounts: [] }));
    render(<WithdrawalIntelligenceSection scope="49" lang="ja" />);
    const section = await screen.findByTestId("withdrawal-intelligence-section");
    expect(within(section).getByTestId("wi-section-empty").textContent).toMatch(/この口座で観測された出金はありません。/);
  });

  it("owner-wide empty keeps the GENERIC message (distinct from the scoped one)", async () => {
    route(emptyMetrics(), history({ withdrawals: [], brokers: [], accounts: [] }));
    render(<WithdrawalIntelligenceSection scope="ALL" />);
    const section = await screen.findByTestId("withdrawal-intelligence-section");
    const t = within(section).getByTestId("wi-section-empty").textContent || "";
    expect(t).toMatch(/No withdrawals observed yet/);
    expect(t).not.toMatch(/for this account/);
  });

  it("recovers after a TRANSIENT error on one scope: a later successful scope still renders (hidden latch not global)", async () => {
    api.apiFetch.mockImplementation((url: unknown) => {
      const u = String(url);
      if (u.startsWith(METRICS)) {
        if (u.includes("account_id=46")) return Promise.reject(new Error("transient 500"));   // scope 46 errors
        return Promise.resolve(metrics());                                                     // ALL succeeds
      }
      if (u.startsWith(HISTORY)) return Promise.resolve(history());
      return Promise.reject(new Error("x"));
    });
    const { rerender } = render(<WithdrawalIntelligenceSection scope="46" />);   // transient error -> section hidden
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("withdrawal-intelligence-section")).toBeNull();
    rerender(<WithdrawalIntelligenceSection scope="ALL" />);                      // switch to a healthy scope
    // Must RECOVER — the DARK/error latch is per scope-load, so the earlier transient failure does not hide ALL.
    await screen.findByTestId("withdrawal-intelligence-section");
  });

  it("never renders a stale earlier-scope response after a rapid scope change (ALL -> 49)", async () => {
    let resolveAll: () => void = () => {};
    api.apiFetch.mockImplementation((url: unknown) => {
      const u = String(url);
      if (u.startsWith(METRICS)) {
        if (u.includes("account_id=49")) return Promise.resolve(metrics({ completed_amount_by_currency: { USD: "200.00" } }));
        return new Promise((res) => { resolveAll = () => res(metrics({ completed_amount_by_currency: { USD: "24.12" } })); });
      }
      if (u.startsWith(HISTORY)) {
        return Promise.resolve(u.includes("account_id=49")
          ? history({ withdrawals: [row({ amount: "200.00", broker_reference_id: "w49" })] })
          : history({ withdrawals: [row({ amount: "24.12", broker_reference_id: "all1" })] }));
      }
      return Promise.reject(new Error("x"));
    });
    const { rerender } = render(<WithdrawalIntelligenceSection scope="ALL" />);   // ALL metrics stays pending
    rerender(<WithdrawalIntelligenceSection scope="49" />);                        // switch before ALL resolves
    const section = await screen.findByTestId("withdrawal-intelligence-section");
    await waitFor(() => expect(section.textContent).toMatch(/200\.00 USD/));       // the latest scope (49) renders
    resolveAll();                                                                  // the stale ALL response arrives late
    await new Promise((r) => setTimeout(r, 25));
    expect(section.textContent).not.toMatch(/24\.12 USD/);                         // ...and is NEVER rendered
    expect(section.textContent).toMatch(/200\.00 USD/);
  });
});

// ── Automatic refresh: 15s visibility-gated polling, single loop, scope-retained, unmount-clean ──
describe("WithdrawalIntelligenceSection — auto-refresh", () => {
  beforeEach(() => { api.apiFetch.mockReset(); vi.useFakeTimers(); });
  afterEach(() => { cleanup(); vi.runOnlyPendingTimers(); vi.useRealTimers(); });

  const metricsCalls = () => api.apiFetch.mock.calls.filter((c) => String(c[0]).startsWith(METRICS)).length;
  const tick = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

  it("1: automatically refreshes metrics on the 15s interval", async () => {
    route(metrics(), history());
    render(<WithdrawalIntelligenceSection scope="ALL" />);
    await tick(0);
    expect(metricsCalls()).toBe(1);
    await tick(15_000);
    expect(metricsCalls()).toBe(2);
    await tick(15_000);
    expect(metricsCalls()).toBe(3);
  });

  it("2+3+9: exactly ONE polling loop; a rerender (same scope) adds no request and no second loop", async () => {
    route(metrics(), history());
    const { rerender } = render(<WithdrawalIntelligenceSection scope="ALL" />);
    await tick(0);
    rerender(<WithdrawalIntelligenceSection scope="ALL" />);   // #9: rerender alone must not fetch
    await tick(0);
    expect(metricsCalls()).toBe(1);
    await tick(15_000);                 // #3: one tick -> exactly one more (no overlap)
    expect(metricsCalls()).toBe(2);
  });

  it("2: stops polling after unmount (interval cleaned up — no fetch after unmount)", async () => {
    route(metrics(), history());
    const { unmount } = render(<WithdrawalIntelligenceSection scope="ALL" />);
    await tick(0);
    expect(metricsCalls()).toBe(1);
    unmount();
    await tick(60_000);
    expect(metricsCalls()).toBe(1);
  });

  it("4: retains the selected account scope on every refresh", async () => {
    const urls: string[] = [];
    api.apiFetch.mockImplementation((url: unknown) => {
      const u = String(url); urls.push(u);
      if (u.startsWith(METRICS)) return Promise.resolve(metrics());
      if (u.startsWith(HISTORY)) return Promise.resolve(history());
      return Promise.reject(new Error("x"));
    });
    render(<WithdrawalIntelligenceSection scope="49" />);
    await tick(0);
    await tick(15_000);
    const m = urls.filter((u) => u.startsWith(METRICS));
    expect(m.length).toBe(2);
    expect(m.every((u) => u.includes("account_id=49"))).toBe(true);
  });

  it("8: after a successful load, a later transient poll failure does NOT hide the card", async () => {
    let call = 0;
    api.apiFetch.mockImplementation((url: unknown) => {
      const u = String(url);
      if (u.startsWith(METRICS)) { call += 1; return call === 2 ? Promise.reject(new Error("transient")) : Promise.resolve(metrics()); }
      if (u.startsWith(HISTORY)) return Promise.resolve(history());
      return Promise.reject(new Error("x"));
    });
    render(<WithdrawalIntelligenceSection scope="ALL" />);
    await tick(0);
    expect(screen.queryByTestId("withdrawal-intelligence-section")).not.toBeNull();
    await tick(15_000);   // 2nd poll fails
    expect(screen.queryByTestId("withdrawal-intelligence-section")).not.toBeNull();   // still shown (latch per load)
    await tick(15_000);   // 3rd poll ok
    expect(screen.queryByTestId("withdrawal-intelligence-section")).not.toBeNull();
  });
});

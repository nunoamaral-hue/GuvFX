/** OpenTradesPanel — live open-trades monitoring: multi-account attribution, account-scoped ids, empty state,
 * fixed-height scroll, 30s read-only polling (single-flight, stale-scope race safety, transient-failure tolerance,
 * unmount cleanup), and no execution/mutation surface. */
import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, within, act } from "@testing-library/react";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { OpenTradesPanel } from "@/components/dashboard/OpenTradesPanel";
import type { OpenTrade, OpenTradesResult } from "@/types/portfolio";

function trade(over: Partial<OpenTrade> = {}): OpenTrade {
  return {
    position_id: "1:100", ticket: 100, account_id: 1, broker: "Taurex", account_masked: "••••7146",
    symbol: "XAUUSD", side: "BUY", volume: 0.01, open_price: 2400, current_price: 2412,
    pl_native: 12.45, pl_native_currency: "USD", pl_usd: 12.45, strategy: "Wayond WIM", ...over,
  };
}
function result(trades: OpenTrade[], over: Partial<OpenTradesResult> = {}): OpenTradesResult {
  return {
    reporting_currency: "USD", scope: "ALL", generated_at: new Date().toISOString(), count: trades.length,
    open_pl_usd: { total_usd: trades.reduce((a, t) => a + (t.pl_usd || 0), 0), basis: "USD",
                   converted_count: trades.length, unconverted: [] },
    stale_accounts: [], trades, ...over,
  };
}

beforeEach(() => { api.apiFetch.mockReset(); });
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("OpenTradesPanel", () => {
  it("renders multi-account positions with attribution and account-scoped ids", async () => {
    api.apiFetch.mockResolvedValue(result([
      trade({ position_id: "1:123", ticket: 123, account_id: 1, broker: "Taurex", symbol: "XAUUSD", side: "BUY", pl_usd: 12.45 }),
      trade({ position_id: "2:123", ticket: 123, account_id: 2, broker: "Pepperstone", account_masked: "••••5672", symbol: "EURUSD", side: "SELL", pl_usd: -3.14 }),
    ]));
    render(<OpenTradesPanel scope="ALL" />);
    await waitFor(() => expect(screen.getByText(/Taurex/)).toBeTruthy());
    expect(screen.getByText(/Pepperstone/)).toBeTruthy();          // both brokers attributed
    expect(screen.getAllByText(/XAUUSD/).length).toBeGreaterThan(0);
    expect(screen.getByText(/EURUSD/)).toBeTruthy();               // same ticket 123, distinct rows (scoped ids)
    expect(screen.getByText(/2 open/)).toBeTruthy();
  });

  it("shows a calm empty state when there are no open trades", async () => {
    api.apiFetch.mockResolvedValue(result([]));
    render(<OpenTradesPanel scope="ALL" />);
    await waitFor(() => expect(screen.getByText("No open trades")).toBeTruthy());
    expect(screen.getByText(/0 open/)).toBeTruthy();
  });

  it("keeps a fixed-height scrollable list for many positions (no page jump)", async () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      trade({ position_id: `1:${i}`, ticket: i, symbol: `SYM${i}` }));
    api.apiFetch.mockResolvedValue(result(many));
    render(<OpenTradesPanel scope="ALL" />);
    await waitFor(() => expect(screen.getByText(/12 open/)).toBeTruthy());
    const list = screen.getByTestId("open-trades-list");
    expect(list.style.overflowY).toBe("auto");
    expect(list.style.maxHeight).toBeTruthy();                     // bounded height -> internal scroll, not page growth
    expect(within(list).getAllByText(/SYM/).length).toBe(12);      // all rows present, just scrollable
  });

  it("polls every 30s (read-only) and updates in place without a blank flash", async () => {
    vi.useFakeTimers();
    api.apiFetch.mockResolvedValue(result([trade()]));
    render(<OpenTradesPanel scope="ALL" />);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.apiFetch).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(api.apiFetch).toHaveBeenCalledTimes(2);                 // refetched at 30s
    // every call is the read-only open-trades endpoint (no execution/mutation surface)
    for (const call of api.apiFetch.mock.calls) {
      expect(String(call[0])).toContain("/api/analytics/portfolio/open-trades/");
    }
  });

  it("stops polling on unmount", async () => {
    vi.useFakeTimers();
    api.apiFetch.mockResolvedValue(result([trade()]));
    const { unmount } = render(<OpenTradesPanel scope="ALL" />);
    await vi.advanceTimersByTimeAsync(1);
    expect(api.apiFetch).toHaveBeenCalledTimes(1);
    unmount();
    await vi.advanceTimersByTimeAsync(90_000);
    expect(api.apiFetch).toHaveBeenCalledTimes(1);                 // no calls after unmount
  });

  it("retains last-known data and marks stale on a transient failure", async () => {
    vi.useFakeTimers();
    api.apiFetch.mockResolvedValueOnce(result([trade({ symbol: "XAUUSD" })]));
    render(<OpenTradesPanel scope="ALL" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText(/XAUUSD/)).toBeTruthy();
    api.apiFetch.mockRejectedValueOnce(new Error("network"));
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    expect(screen.getByText(/XAUUSD/)).toBeTruthy();               // last-known data NOT erased
    expect(screen.getByText(/reconnecting/)).toBeTruthy();         // restrained stale indicator
  });

  it("distinguishes loading from a confirmed-empty result (M4)", async () => {
    let resolve: (v: OpenTradesResult) => void = () => {};
    api.apiFetch.mockImplementation(() => new Promise<OpenTradesResult>((r) => { resolve = r; }));
    render(<OpenTradesPanel scope="ALL" />);
    await waitFor(() => expect(screen.getByText("Loading positions…")).toBeTruthy());
    expect(screen.queryByText("No open trades")).toBeNull();      // never assert empty before the fetch resolves
    resolve(result([]));                                          // confirmed empty
    await waitFor(() => expect(screen.getByText("No open trades")).toBeTruthy());
    expect(screen.queryByText("Loading positions…")).toBeNull();
  });

  it("surfaces unreachable accounts and a capped row list (M1/M3)", async () => {
    const many = Array.from({ length: 300 }, (_, i) => trade({ position_id: `1:${i}`, ticket: i }));
    api.apiFetch.mockResolvedValue(result(many, { count: 400, truncated: true, stale_accounts: [7, 9] }));
    render(<OpenTradesPanel scope="ALL" />);
    await waitFor(() => expect(screen.getByText(/400 open/)).toBeTruthy());   // full count, not the capped 300
    expect(screen.getByText(/2 accounts: live positions unavailable/)).toBeTruthy();
    expect(screen.getByText(/Showing 300 of 400/)).toBeTruthy();
  });

  it("ignores a stale-scope response after the scope changes", async () => {
    let resolveAll: (v: OpenTradesResult) => void = () => {};
    api.apiFetch.mockImplementation((path: string) => {
      if (path.includes("scope=ALL")) return new Promise<OpenTradesResult>((res) => { resolveAll = res; });
      return Promise.resolve(result([trade({ position_id: "5:1", broker: "IS6", symbol: "GBPUSD" })], { scope: "5" }));
    });
    const { rerender } = render(<OpenTradesPanel scope="ALL" />);
    rerender(<OpenTradesPanel scope="5" />);                       // switch scope before ALL resolves
    await waitFor(() => expect(screen.getByText(/GBPUSD/)).toBeTruthy());
    resolveAll(result([trade({ symbol: "XAUUSD" })], { scope: "ALL" }));  // late ALL response
    await Promise.resolve();
    expect(screen.queryByText(/XAUUSD/)).toBeNull();              // stale-scope response did NOT overwrite scope=5
    expect(screen.getByText(/GBPUSD/)).toBeTruthy();
  });
});

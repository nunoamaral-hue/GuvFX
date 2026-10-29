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

  it("keeps a single internally-scrolling body for many positions (no page jump)", async () => {
    const many = Array.from({ length: 12 }, (_, i) =>
      trade({ position_id: `1:${i}`, ticket: i, symbol: `SYM${i}` }));
    api.apiFetch.mockResolvedValue(result(many));
    render(<OpenTradesPanel scope="ALL" />);
    await waitFor(() => expect(screen.getByText(/12 open/)).toBeTruthy());
    const list = screen.getByTestId("open-trades-list");
    // Height + scroll are now governed by the .open-trades-body CSS (fixed-height flex body that matches the Market
    // Focus row and scrolls internally), not an inline maxHeight that could drift; jsdom can't compute CSS so we
    // assert the layout hook + that every row is present (the actual height-invariance is proven in the browser).
    expect(list.className).toContain("open-trades-body");
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

  it("settles to 'Positions unavailable' when the first fetch fails (e.g. client timeout), never stuck loading", async () => {
    api.apiFetch.mockRejectedValue(new Error("network_unreachable"));   // what a deadline-aborted apiFetch throws
    render(<OpenTradesPanel scope="ALL" />);
    await waitFor(() => expect(screen.getByText("Positions unavailable")).toBeTruthy());
    expect(screen.queryByText("Loading positions…")).toBeNull();        // exited loading on failure, not stuck
    // the panel must pass a bounded client deadline so apiFetch can abort a hanging upstream
    expect(api.apiFetch).toHaveBeenCalledWith(expect.stringContaining("/open-trades/"), {}, 18_000);
  });

  it("renders a single GBP account's floating total in GBP, never '$0.00 USD' (flat account)", async () => {
    api.apiFetch.mockResolvedValue(result([], {
      scope: "49", reporting_currency: "GBP",
      open_pl_usd: { total_usd: 0.0, basis: "USD", converted_count: 0, unconverted: [], state: "EMPTY" },
    }));
    render(<OpenTradesPanel scope="49" />);
    await waitFor(() => expect(screen.getByText(/0\.00 GBP/)).toBeTruthy());   // native GBP, incl. flat
    expect(screen.queryByText(/\$0\.00 USD/)).toBeNull();                      // the pre-existing bug
    expect(screen.queryByText(/USD/)).toBeNull();                             // no USD label on a GBP account
  });

  it("sums a single GBP account's open positions in GBP (never forced through USD)", async () => {
    api.apiFetch.mockResolvedValue(result([
      trade({ position_id: "49:1", account_id: 49, broker: "Pepperstone", account_masked: "••••5983",
              symbol: "XAUUSD", pl_native: 12.5, pl_native_currency: "GBP", pl_usd: null }),
      trade({ position_id: "49:2", account_id: 49, broker: "Pepperstone", account_masked: "••••5983",
              symbol: "XAUUSD", side: "SELL", pl_native: -2.5, pl_native_currency: "GBP", pl_usd: null }),
    ], {
      scope: "49", reporting_currency: "GBP",
      open_pl_usd: { total_usd: null, basis: "PARTIAL", converted_count: 0, state: "UNAVAILABLE",
                     unconverted: [{ account_id: 49, native: null, native_currency: "GBP", reason: "currency_unconvertible" }] },
    }));
    render(<OpenTradesPanel scope="49" />);
    await waitFor(() => expect(screen.getByText(/\+10\.00 GBP/)).toBeTruthy());   // 12.5 + (−2.5) = 10.00, in GBP
    expect(screen.queryByText(/\d\.\d\d USD/)).toBeNull();                        // no USD money label (not XAUUSD)
    expect(screen.queryByText(/\$\d/)).toBeNull();                               // no $ figure anywhere
  });

  it("All-Accounts: a GBP position shows in GBP and is excluded from the USD total — never mixed as +$1.50", async () => {
    // The exact prod contamination: USD +13.83 and GBP −12.24/−0.09 must NOT be summed as a single "$1.50" USD.
    // The honest total is the USD-only +13.83 marked PARTIAL, with the GBP position rendered in its own currency.
    api.apiFetch.mockResolvedValue(result([
      trade({ position_id: "46:1", account_id: 46, broker: "TradersWay", account_masked: "••••5442",
              symbol: "XAUUSD", pl_native: 13.83, pl_native_currency: "USD", pl_usd: 13.83 }),
      trade({ position_id: "49:1", account_id: 49, broker: "Pepperstone", account_masked: "••••5983",
              symbol: "XAUUSD_SB", pl_native: -12.24, pl_native_currency: "GBP", pl_usd: null }),
    ], {
      scope: "ALL",
      open_pl_usd: { total_usd: 13.83, basis: "PARTIAL", state: "PARTIAL", converted_count: 1,
                     unconverted: [{ account_id: 49, native: -12.24, native_currency: "GBP",
                                     reason: "currency_unconvertible", category: "currency_unconvertible" }] },
    }));
    render(<OpenTradesPanel scope="ALL" />);
    await waitFor(() => expect(screen.getAllByText(/\$13\.83/).length).toBeGreaterThan(0));  // USD-only total (not $1.50)
    expect(screen.getByText(/12\.24 GBP/)).toBeTruthy();                          // GBP position in GBP, no $
    expect(screen.queryByText(/1\.50/)).toBeNull();                              // contamination value NEVER shown
    expect(screen.queryByText(/\$12\.24/)).toBeNull();                           // GBP figure never carries a $
  });
});

describe("OpenTradesPanel — layout stability & accessibility (Dashboard P0 UX)", () => {
  it("renders a single open trade inside the tile", async () => {
    api.apiFetch.mockResolvedValue(result([trade({ symbol: "XAUUSD" })]));
    render(<OpenTradesPanel scope="ALL" />);
    await waitFor(() => expect(screen.getByText(/1 open/)).toBeTruthy());
    expect(screen.getByText(/XAUUSD/)).toBeTruthy();
  });

  it("carries the fixed-height layout hooks (tile + body classes)", async () => {
    api.apiFetch.mockResolvedValue(result([]));
    render(<OpenTradesPanel scope="ALL" />);
    const panel = await screen.findByTestId("open-trades-panel");
    const list = screen.getByTestId("open-trades-list");
    expect(panel.className).toContain("open-trades-tile");
    expect(list.className).toContain("open-trades-body");
  });

  it("exposes the list as a keyboard-accessible, named scroll region", async () => {
    api.apiFetch.mockResolvedValue(result([trade()]));
    render(<OpenTradesPanel scope="ALL" />);
    const list = await screen.findByTestId("open-trades-list");
    expect(list.getAttribute("role")).toBe("region");
    expect(list.getAttribute("tabindex")).toBe("0");                // reachable + scrollable by keyboard
    expect(list.getAttribute("aria-label")).toBe("Open trades list");
  });

  it("names the scroll region in Japanese", async () => {
    api.apiFetch.mockResolvedValue(result([trade()]));
    render(<OpenTradesPanel scope="ALL" lang="ja" />);
    const list = await screen.findByTestId("open-trades-list");
    expect(list.getAttribute("aria-label")).toBe("オープン取引リスト");
    expect(screen.getByText("オープン取引")).toBeTruthy();           // JA title
  });

  it("truncates long broker/account/symbol values so a row cannot widen the tile", async () => {
    api.apiFetch.mockResolvedValue(result([trade({
      broker: "A-Very-Long-Broker-Name-That-Would-Otherwise-Overflow", account_masked: "••••999999",
      symbol: "SUPERLONGSYMBOLNAME", strategy: "A very long strategy label that keeps going and going",
    })]));
    render(<OpenTradesPanel scope="ALL" />);
    const brokerLine = await screen.findByText(/A-Very-Long-Broker-Name/);
    expect(brokerLine.style.overflow).toBe("hidden");
    expect(brokerLine.style.textOverflow).toBe("ellipsis");
    expect(brokerLine.style.whiteSpace).toBe("nowrap");
  });

  it("centers the empty state within the body", async () => {
    api.apiFetch.mockResolvedValue(result([]));
    render(<OpenTradesPanel scope="ALL" />);
    const empty = await screen.findByText("No open trades");
    expect(empty.style.margin).toBe("auto");                        // centered in the flex body
  });

  it("keeps header, open-count and floating P/L visible with an overflowing list", async () => {
    vi.useFakeTimers();
    const many = Array.from({ length: 20 }, (_, i) => trade({ position_id: `1:${i}`, ticket: i }));
    api.apiFetch.mockResolvedValue(result(many));
    render(<OpenTradesPanel scope="ALL" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByText("Open Trades")).toBeTruthy();           // fixed header
    expect(screen.getByText(/20 open/)).toBeTruthy();               // fixed open-count
    expect(screen.getByText(/Floating P\/L/)).toBeTruthy();         // fixed floating P/L label
  });
});

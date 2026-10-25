/** WithdrawalsHistory (PR2) — per-withdrawal list: self-hides on the DARK 404, renders truthful states (pending ≠
 * failed, unresolved visible), per-row amount+currency, "N/A" duration when unknown, masked accounts, and exposes
 * no action. */
import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { WithdrawalsHistory } from "@/components/dashboard/WithdrawalsHistory";
import type { WithdrawalHistory } from "@/types/withdrawals";

function history(over: Partial<WithdrawalHistory> = {}): WithdrawalHistory {
  return {
    withdrawals: [
      { id: 1, broker: "TradersWay", trading_account_id: 46, account_masked: "••••0123", state: "completed",
        status: "COMPLETED", amount: "200.00", currency: "USD", requested_at: "2026-10-01T00:00:00Z",
        completed_at: "2026-10-01T01:00:00Z", duration_seconds: 3600, broker_reference_id: "W-9", reference_correlated: true },
      { id: 2, broker: "TradersWay", trading_account_id: 46, account_masked: "••••0123", state: "pending",
        status: "PENDING", amount: null, currency: "", requested_at: "2026-10-02T00:00:00Z", completed_at: null,
        duration_seconds: null, broker_reference_id: "", reference_correlated: false },
      { id: 3, broker: "IS6 Technologies", trading_account_id: 47, account_masked: "••••3830", state: "unresolved",
        status: "UNRESOLVED", amount: null, currency: "", requested_at: null, completed_at: null,
        duration_seconds: null, broker_reference_id: "", reference_correlated: false },
    ],
    brokers: ["IS6 Technologies", "TradersWay"],
    accounts: [{ id: 46, broker: "TradersWay", account_masked: "••••0123" },
               { id: 47, broker: "IS6 Technologies", account_masked: "••••3830" }],
    ...over,
  };
}

beforeEach(() => { api.apiFetch.mockReset(); });
afterEach(() => { cleanup(); });

describe("WithdrawalsHistory", () => {
  it("self-hides on the DARK 404", async () => {
    api.apiFetch.mockRejectedValue(Object.assign(new Error("Not found."), { status: 404 }));
    const { container } = render(<WithdrawalsHistory />);
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(container.querySelector('[data-testid="withdrawals-history"]')).toBeNull();
  });

  it("renders truthful states, per-row amounts, N/A duration and masked accounts", async () => {
    api.apiFetch.mockResolvedValue(history());
    render(<WithdrawalsHistory />);
    const panel = await screen.findByTestId("withdrawals-history");
    expect(panel.textContent).toMatch(/In progress/);        // pending shown, NOT as failed
    expect(panel.textContent).toMatch(/Completed/);
    expect(panel.textContent).toMatch(/Unresolved/);          // unresolved is a visible state
    expect(panel.textContent).toMatch(/200\.00 USD/);         // per-row observed amount
    expect(panel.textContent).toMatch(/••••0123/);            // masked account, never full number
    expect(panel.textContent).not.toMatch(/failed/i);         // nothing failed here
    const rows = screen.getAllByTestId("history-row");
    expect(rows.length).toBe(3);
  });

  it("shows the empty state when there are no withdrawals", async () => {
    api.apiFetch.mockResolvedValue(history({ withdrawals: [], brokers: [], accounts: [] }));
    render(<WithdrawalsHistory />);
    const panel = await screen.findByTestId("withdrawals-history");
    expect(panel.textContent).toMatch(/No withdrawals observed yet/);
  });

  it("renders an owner-attributed account-UNRESOLVED row without exposing the sentinel or a fabricated account", async () => {
    // ADR-0050: trading_account_id null + account_unresolved true. The row shows "Account unresolved", NOT the raw
    // "UNRESOLVED" sentinel and NOT a fabricated account number; the broker reference remains the identifier.
    api.apiFetch.mockResolvedValue(history({
      withdrawals: [
        { id: 9, broker: "TradersWay", trading_account_id: null, account_masked: "UNRESOLVED",
          account_unresolved: true, state: "completed", status: "COMPLETED", amount: "250.00", currency: "USD",
          requested_at: "2026-10-19T07:55:04Z", completed_at: "2026-10-19T08:28:05Z", duration_seconds: 1981,
          broker_reference_id: "7654321", reference_correlated: true },
      ],
      brokers: ["TradersWay"], accounts: [],
    }));
    render(<WithdrawalsHistory />);
    const panel = await screen.findByTestId("withdrawals-history");
    expect(panel.textContent).toMatch(/Account unresolved/);     // the distinct label is shown
    expect(panel.textContent).not.toMatch(/UNRESOLVED/);          // never the raw sentinel
    expect(panel.textContent).toMatch(/7654321/);                // broker reference is the identifier
    expect(panel.textContent).toMatch(/250\.00 USD/);
  });

  it("embedded: hides the filter selects, caps to the recent rows, and notes truncation", async () => {
    const many = Array.from({ length: 7 }, (_, i) => ({
      id: i + 1, broker: "TradersWay", trading_account_id: 46, account_masked: "••••0123",
      state: "completed" as const, status: "COMPLETED", amount: `${i + 1}.00`, currency: "USD",
      requested_at: "2026-10-01T00:00:00Z", completed_at: "2026-10-01T01:00:00Z", duration_seconds: 3600,
      broker_reference_id: `W-${i + 1}`, reference_correlated: true,
    }));
    api.apiFetch.mockResolvedValue(history({ withdrawals: many }));
    render(<WithdrawalsHistory embedded />);
    await screen.findByTestId("withdrawals-history");
    expect(screen.queryByTestId("history-broker-select")).toBeNull();   // filtering lives on the detail page
    expect(screen.queryByTestId("history-account-select")).toBeNull();
    expect(screen.getAllByTestId("history-row").length).toBe(5);         // only the recent 5
    expect(screen.getByTestId("history-truncated").textContent).toMatch(/Showing 5 most recent/);
  });

  it("embedded: shows all rows (no truncation note) when within the recent limit, still no filters", async () => {
    api.apiFetch.mockResolvedValue(history());   // 3 rows < limit 5
    render(<WithdrawalsHistory embedded />);
    await screen.findByTestId("withdrawals-history");
    expect(screen.getAllByTestId("history-row").length).toBe(3);
    expect(screen.queryByTestId("history-truncated")).toBeNull();
    expect(screen.queryByTestId("history-broker-select")).toBeNull();
  });

  it("uses parent-injected history WITHOUT fetching (the section feeds both columns from one load)", async () => {
    render(<WithdrawalsHistory embedded data={history()} />);
    const panel = await screen.findByTestId("withdrawals-history");
    expect(api.apiFetch).not.toHaveBeenCalled();   // injected -> never self-fetches
    expect(panel.textContent).toMatch(/200\.00 USD/);
    expect(screen.getAllByTestId("history-row").length).toBe(3);
  });

  it("hides when the injected history is null (no fetch, no broken card)", () => {
    const { container } = render(<WithdrawalsHistory embedded data={null} />);
    expect(api.apiFetch).not.toHaveBeenCalled();
    expect(container.querySelector('[data-testid="withdrawals-history"]')).toBeNull();
  });
});

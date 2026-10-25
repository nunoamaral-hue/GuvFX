/** WithdrawalsPanel (WP6) — read-only member withdrawal summary: renders metrics truthfully (PENDING distinct from
 * failed, per-currency totals, N/A duration when unknown), self-hides on the DARK 404, and exposes no action. */
import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { WithdrawalsPanel } from "@/components/dashboard/WithdrawalsPanel";
import type { WithdrawalMetrics } from "@/types/withdrawals";

function metrics(over: Partial<WithdrawalMetrics> = {}): WithdrawalMetrics {
  return {
    total: 3, by_status: {}, pending_count: 1, completed_count: 1, failed_count: 1, unresolved_count: 0,
    completed_amount_by_currency: { USD: "200.00" }, completed_amount_known_count: 1,
    processing_duration: { count: 1, avg_seconds: 3600, median_seconds: 3600, min_seconds: 3600, max_seconds: 3600 },
    ...over,
  };
}

beforeEach(() => { api.apiFetch.mockReset(); });
afterEach(() => { cleanup(); });

describe("WithdrawalsPanel", () => {
  it("renders pending / completed / failed distinctly and a per-currency total", async () => {
    api.apiFetch.mockResolvedValue(metrics());
    render(<WithdrawalsPanel />);
    await waitFor(() => expect(screen.getByTestId("withdrawals-panel")).toBeTruthy());
    const panel = screen.getByTestId("withdrawals-panel");
    expect(panel.textContent).toMatch(/In progress/);
    expect(panel.textContent).toMatch(/Completed/);
    expect(panel.textContent).toMatch(/Failed/);
    expect(panel.textContent).toMatch(/200\.00 USD/);
    expect(panel.textContent).toMatch(/1 h|60 min/);   // 3600s median
  });

  it("self-hides on the DARK 404 (renders nothing)", async () => {
    const err = Object.assign(new Error("Not found."), { status: 404, httpStatus: 404 });
    api.apiFetch.mockRejectedValue(err);
    const { container } = render(<WithdrawalsPanel />);
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("withdrawals-panel")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("self-hides on a non-conforming response (never crashes the dashboard)", async () => {
    // Regression: a catch-all/stub response like {} (no `total`) must hide the panel, not throw
    // Object.entries(undefined) and break the surrounding dashboard render.
    api.apiFetch.mockResolvedValue({} as unknown);
    const { container } = render(<WithdrawalsPanel />);
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(screen.queryByTestId("withdrawals-panel")).toBeNull();
    expect(container.textContent).toBe("");
  });

  it("shows N/A processing time when duration is unknown (never a fabricated 0)", async () => {
    api.apiFetch.mockResolvedValue(metrics({ processing_duration: null, completed_count: 0, pending_count: 1,
      completed_amount_by_currency: {}, total: 1 }));
    render(<WithdrawalsPanel />);
    await waitFor(() => expect(screen.getByTestId("withdrawals-panel")).toBeTruthy());
    expect(screen.getByTestId("withdrawals-panel").textContent).toMatch(/N\/A/);
  });

  it("shows an honest empty state when there are no withdrawals", async () => {
    api.apiFetch.mockResolvedValue(metrics({ total: 0, pending_count: 0, completed_count: 0, failed_count: 0,
      completed_amount_by_currency: {}, processing_duration: null }));
    render(<WithdrawalsPanel />);
    await waitFor(() => expect(screen.getByTestId("withdrawals-panel")).toBeTruthy());
    expect(screen.getByTestId("withdrawals-panel").textContent).toMatch(/No withdrawals observed yet/);
  });

  it("surfaces the single-sample caveat and the genuine-sample count (§10)", async () => {
    api.apiFetch.mockResolvedValue(metrics({ sample_caveat: true, genuine_count: 1 }));
    render(<WithdrawalsPanel />);
    await waitFor(() => expect(screen.getByTestId("withdrawals-panel")).toBeTruthy());
    expect(screen.getByTestId("withdrawals-sample-caveat").textContent).toMatch(/single observed withdrawal/i);
    expect(screen.getByTestId("withdrawals-genuine-count").textContent).toMatch(/Genuine samples: 1/);
  });

  it("omits the caveat when a figure does not rest on a single sample", async () => {
    api.apiFetch.mockResolvedValue(metrics({ sample_caveat: false, genuine_count: 4,
      processing_duration: { count: 4, avg_seconds: 3600, median_seconds: 3600, min_seconds: 60, max_seconds: 7200 } }));
    render(<WithdrawalsPanel />);
    await waitFor(() => expect(screen.getByTestId("withdrawals-panel")).toBeTruthy());
    expect(screen.queryByTestId("withdrawals-sample-caveat")).toBeNull();
    expect(screen.getByTestId("withdrawals-genuine-count").textContent).toMatch(/Genuine samples: 4/);
  });

  it("embedded: uses injected metrics WITHOUT fetching, and renders the same facts under a Summary heading", async () => {
    render(<WithdrawalsPanel embedded metrics={metrics()} />);
    const panel = await screen.findByTestId("withdrawals-panel");
    expect(api.apiFetch).not.toHaveBeenCalled();     // injected payload -> never self-fetches
    expect(panel.textContent).toMatch(/Summary/);     // compact column heading (the section supplies the main title)
    expect(panel.textContent).toMatch(/In progress/);
    expect(panel.textContent).toMatch(/200\.00 USD/);
    expect(panel.textContent).toMatch(/1 h|60 min/);
  });

  it("embedded: hides when the injected metrics is null (no fetch, no broken frame)", () => {
    const { container } = render(<WithdrawalsPanel embedded metrics={null} />);
    expect(api.apiFetch).not.toHaveBeenCalled();
    expect(screen.queryByTestId("withdrawals-panel")).toBeNull();
    expect(container.textContent).toBe("");
  });
});

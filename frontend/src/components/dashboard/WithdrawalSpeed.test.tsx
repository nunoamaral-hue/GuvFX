/** WithdrawalSpeed (WS3) — self-hides on the DARK 404; renders the honest low-data / insufficient / representative
 * states; shows the accessible trend chart + equivalent data table; per-broker comparison with a low-data label;
 * in-progress live ages separate from completed; quarantine note; EN/JA; exposes no action. */
import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { WithdrawalSpeed } from "@/components/dashboard/WithdrawalSpeed";
import type { WithdrawalSpeed as Speed, SpeedSeriesPoint } from "@/types/withdrawals";

function point(over: Partial<SpeedSeriesPoint> = {}): SpeedSeriesPoint {
  return {
    withdrawal_id: 1, broker: "TradersWay", account_masked: "••••0123", account_unresolved: false,
    amount: "100.00", currency: "USD", requested_at: "2026-10-01T00:00:00Z", completed_at: "2026-10-01T01:00:00Z",
    duration_seconds: 3600, duration_human: "1 h", match_basis: "reference", ...over,
  };
}

function speed(over: Partial<Speed> = {}): Speed {
  return {
    metric: { key: "withdrawal_processing_time", unit: "seconds", domain: "withdrawal",
              direction: "lower_is_better", label: "Withdrawal processing time" },
    headline: { state: "ok", median_seconds: 3600, median_human: "1 h", average_seconds: 4200, average_human: "70 min",
                sample_count: 3, min_sample: 3, qualifier: "observed_from_notification_emails",
                reference_count: 2, heuristic_count: 1 },
    summary: { total: 4, in_progress_count: 1, completed_count: 3, completed_with_duration_count: 3, failed_count: 0,
               unresolved_count: 0, median_seconds: 3600, average_seconds: 4200, fastest_seconds: 1800,
               slowest_seconds: 7200, coverage: { measured: 3, completed: 3 }, quarantined_impossible_count: 0,
               observed_timing_count: 0, untimed_count: 0 },
    series: [point({ withdrawal_id: 1, duration_seconds: 1800 }),
             point({ withdrawal_id: 2, duration_seconds: 3600 }),
             point({ withdrawal_id: 3, duration_seconds: 7200, broker: "IS6", match_basis: "heuristic" })],
    brokers: [
      { broker: "TradersWay", sample_count: 3, median_seconds: 3000, median_human: "50 min", average_seconds: 3200,
        fastest_seconds: 1800, slowest_seconds: 7200, data_quality: "sufficient", min_sample: 3 },
      { broker: "IS6", sample_count: 1, median_seconds: 7200, median_human: "2 h", average_seconds: 7200,
        fastest_seconds: 7200, slowest_seconds: 7200, data_quality: "insufficient", min_sample: 3 },
    ],
    in_progress: [{ withdrawal_id: 9, broker: "TradersWay", account_masked: "••••0123", account_unresolved: false,
      amount: "24.12", currency: "USD", requested_at: "2026-10-02T00:00:00Z", elapsed_seconds: 1980,
      elapsed_human: "33 min", status: "REQUESTED" }],
    currencies: ["USD"], excluded_non_genuine_count: 0, demonstration: false, generated_at: "2026-10-02T00:33:00Z",
    ...over,
  };
}

beforeEach(() => { api.apiFetch.mockReset(); });
afterEach(() => { cleanup(); });

describe("WithdrawalSpeed", () => {
  it("self-hides on the DARK 404", async () => {
    api.apiFetch.mockRejectedValue(Object.assign(new Error("Not found."), { status: 404 }));
    const { container } = render(<WithdrawalSpeed />);
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(container.querySelector('[data-testid="withdrawal-speed"]')).toBeNull();
  });

  it("renders the representative headline (median primary, average secondary, sample count)", async () => {
    api.apiFetch.mockResolvedValue(speed());
    render(<WithdrawalSpeed />);
    expect(await screen.findByTestId("speed-median")).toHaveTextContent("60 min");
    expect(screen.getByTestId("speed-average")).toHaveTextContent("70 min");
    expect(screen.getByTestId("speed-sample-count")).toHaveTextContent("3");
    expect(screen.queryByTestId("speed-insufficient")).toBeNull();
  });

  it("shows the honest empty state when no completed withdrawal exists", async () => {
    api.apiFetch.mockResolvedValue(speed({
      headline: { state: "empty", median_seconds: null, median_human: null, average_seconds: null,
        average_human: null, sample_count: 0, min_sample: 3, qualifier: "observed_from_notification_emails",
        reference_count: 0, heuristic_count: 0 },
      summary: { ...speed().summary, completed_count: 0, completed_with_duration_count: 0 },
      series: [], brokers: [],
    }));
    render(<WithdrawalSpeed />);
    expect(await screen.findByTestId("speed-empty")).toBeInTheDocument();
    expect(screen.getByTestId("speed-trend-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("speed-median")).toBeNull();   // never a fabricated 0
  });

  it("shows the low-data caveat with 1–2 observations (not yet typical) but still shows the figure", async () => {
    api.apiFetch.mockResolvedValue(speed({
      headline: { state: "insufficient", median_seconds: 2520, median_human: "42 min", average_seconds: 2520,
        average_human: "42 min", sample_count: 1, min_sample: 3, qualifier: "observed_from_notification_emails",
        reference_count: 1, heuristic_count: 0 },
      series: [point({ duration_seconds: 2520 })],
      brokers: [{ broker: "TradersWay", sample_count: 1, median_seconds: 2520, median_human: "42 min",
        average_seconds: 2520, fastest_seconds: 2520, slowest_seconds: 2520, data_quality: "insufficient", min_sample: 3 }],
    }));
    render(<WithdrawalSpeed />);
    expect(await screen.findByTestId("speed-insufficient")).toBeInTheDocument();
    expect(screen.getByTestId("speed-median")).toHaveTextContent("42 min");   // shown, not hidden
  });

  it("renders an accessible chart and an equivalent toggled data table", async () => {
    api.apiFetch.mockResolvedValue(speed());
    render(<WithdrawalSpeed />);
    const svg = await screen.findByTestId("speed-trend-svg");
    expect(svg).toHaveAttribute("role", "img");
    expect(svg).toHaveAttribute("aria-label");
    expect(screen.getAllByTestId("speed-bar").length).toBe(3);        // one per completed withdrawal
    expect(screen.queryByTestId("speed-table")).toBeNull();
    fireEvent.click(screen.getByTestId("speed-table-toggle"));
    expect(screen.getByTestId("speed-table")).toBeInTheDocument();    // colour-independent equivalent
  });

  it("per-broker comparison shows a low-data label for an insufficient broker", async () => {
    api.apiFetch.mockResolvedValue(speed());
    render(<WithdrawalSpeed />);
    await screen.findByTestId("speed-brokers");
    expect(screen.getAllByTestId("speed-broker-row").length).toBe(2);
    expect(screen.getByTestId("speed-broker-lowdata")).toBeInTheDocument();   // IS6, 1 sample
  });

  it("renders in-progress live ages separately from completed figures", async () => {
    api.apiFetch.mockResolvedValue(speed());
    render(<WithdrawalSpeed />);
    const rows = await screen.findAllByTestId("speed-inprogress-row");
    expect(rows.length).toBe(1);
    expect(rows[0]).toHaveTextContent("33 min");
    expect(rows[0]).toHaveTextContent("24.12");
  });

  it("surfaces the impossible-duration quarantine note", async () => {
    api.apiFetch.mockResolvedValue(speed({ summary: { ...speed().summary, quarantined_impossible_count: 2 } }));
    render(<WithdrawalSpeed />);
    expect(await screen.findByTestId("speed-quarantined")).toHaveTextContent("2");
  });

  it("discloses the heuristic-match contribution to the headline", async () => {
    api.apiFetch.mockResolvedValue(speed());   // reference_count 2, heuristic_count 1
    render(<WithdrawalSpeed />);
    expect(await screen.findByTestId("speed-heuristic")).toHaveTextContent("1");
  });

  it("discloses observed-timing and untimed completed withdrawals (not charted, not zeroed)", async () => {
    api.apiFetch.mockResolvedValue(speed({
      summary: { ...speed().summary, observed_timing_count: 2, untimed_count: 3 },
    }));
    render(<WithdrawalSpeed />);
    expect(await screen.findByTestId("speed-observed-timing")).toHaveTextContent("2");
    expect(screen.getByTestId("speed-untimed")).toHaveTextContent("3");
  });

  it("renders completion timestamps in UTC (not viewer-local)", async () => {
    api.apiFetch.mockResolvedValue(speed());
    render(<WithdrawalSpeed />);
    fireEvent.click(await screen.findByTestId("speed-table-toggle"));
    const table = screen.getByTestId("speed-table");
    // completed_at "2026-10-01T01:00:00Z" → the UTC hour (01) and a "UTC" marker, regardless of the runner's TZ.
    expect(table).toHaveTextContent("01:00 UTC");
  });

  it("renders Japanese strings without English leakage in the headline", async () => {
    api.apiFetch.mockResolvedValue(speed());
    render(<WithdrawalSpeed lang="ja" />);
    expect(await screen.findByTestId("speed-headline")).toHaveTextContent("通常の出金処理時間");
    expect(screen.getByTestId("speed-headline")).not.toHaveTextContent("Typical withdrawal");
  });
});

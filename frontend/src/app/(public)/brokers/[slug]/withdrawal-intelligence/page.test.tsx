/** Broker withdrawal-intelligence page — DARK-graceful + aggregate-only, reason-coded rendering (ADR-0080). */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

vi.mock("next/navigation", () => ({ useParams: () => ({ slug: "pepperstone" }) }));

import WithdrawalIntelligencePage from "./page";

const PUBLISHED = {
  category: "WITHDRAWAL_INTELLIGENCE",
  methodology_version: 1,
  broker: { slug: "pepperstone", display_name: "Pepperstone" },
  evidence_status: { state: "PUBLISHED", reason: "OK" },
  methodology: "Aggregate-only broker withdrawal evidence across all contributing members.",
  limitations: ["Observational evidence, not a broker audit."],
  observation_window: { start: null, end: "2026-11-06" },
  data_freshness: "2026-11-05",
  sample_sufficiency: { contributors: 3, observations: 5 },
  withdrawal_speed: {
    request_to_confirmation: { state: "PUBLISHED", count: 5, median_seconds: 3600 },
    confirmation_to_completion: { state: "UNAVAILABLE", reason: "STAGE_ANCHORS_MISSING" },
    total_request_to_completion: { state: "PUBLISHED", count: 5, median_seconds: 7200 },
  },
  lifecycle_outcomes: {
    counts: { broker_reported_completed: 5, receipt_confirmed_if_ever_available: 0 },
    rates: { denominator: 5, completion_pct: 100, failed_pct: 0, expired_pct: 0 },
    confirmation_required_flow: { required_confirmation_step: 0, no_confirmation_step: 5 },
  },
  per_currency_counts: { USD: 5 },
};

const SUPPRESSED = {
  category: "WITHDRAWAL_INTELLIGENCE",
  methodology_version: 1,
  broker: { slug: "pepperstone", display_name: "Pepperstone" },
  evidence_status: { state: "UNAVAILABLE", reason: "NOT_CERTIFIED" },
  methodology: "…",
  limitations: [],
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
beforeEach(() => { vi.stubGlobal("fetch", vi.fn()); });

describe("PublicBrokerWithdrawalIntelligencePage", () => {
  it("shows the calm unavailable state on the DARK 404", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: false, status: 404 });
    render(<WithdrawalIntelligencePage />);
    await waitFor(() => expect(screen.getByText(/not available in this environment/i)).toBeTruthy());
  });

  it("renders the aggregate figures on the PUBLISHED path", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, status: 200, json: async () => PUBLISHED });
    render(<WithdrawalIntelligencePage />);
    await waitFor(() => expect(screen.getAllByText("Pepperstone").length).toBeGreaterThan(0));
    expect(screen.getByText("100%")).toBeTruthy();                 // completion rate
    expect(screen.getByText(/2h 0m \(n=5\)/)).toBeTruthy();        // total stage median, coarsened, with count
    expect(screen.getByText(/2026-11-05/)).toBeTruthy();           // date-granular freshness (no exact time)
  });

  it("shows a reason-coded withheld panel when not published, with no fabricated numbers", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, status: 200, json: async () => SUPPRESSED });
    render(<WithdrawalIntelligencePage />);
    await waitFor(() => expect(screen.getByText(/NOT_CERTIFIED/)).toBeTruthy());
    expect(screen.queryByText("100%")).toBeNull();
  });
});

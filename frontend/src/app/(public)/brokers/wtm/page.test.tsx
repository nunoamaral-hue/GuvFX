/** WTM preview page — DARK-graceful + renders the clearly-labelled SIMULATED scorecard. */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

import WtmPreviewPage from "./page";

const PAYLOAD = {
  preview: true, not_a_deployed_integration: true, title: "WAYOND Trader Match — Broker Performance (Preview)",
  authentic_state: "INSUFFICIENT_VERIFIED_DATA", authentic_note: "No broker yet has enough verified samples.",
  simulated_brokers: [{
    slug: "tradersway", display_name: "TradersWay", simulated: true, sample_size: 22, contributor_count: 11,
    methodology_version: 1,
    withdrawal_processing: {
      median_minutes: 44, avg_minutes: 58, p90_minutes: 120, min_minutes: 12, max_minutes: 240,
      histogram: [{ bucket: "0–30m", count: 7 }, { bucket: "30–60m", count: 8 }, { bucket: "1–2h", count: 5 }, { bucket: "2h+", count: 2 }],
      lifecycle_pct: { completed: 86, pending: 9, rejected: 5 },
    },
  }],
  methodology: "Median request→completion across winsorised, k-anonymised samples.",
  privacy: "Aggregate-only; no member identity.", source: "GuvFX authenticated broker-email evidence.",
  flow: ["GuvFX evidence", "Correlated observations", "Privacy-safe aggregate", "WAYOND Trader Match"],
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
beforeEach(() => { vi.stubGlobal("fetch", vi.fn()); });

describe("WtmPreviewPage", () => {
  it("shows the calm unavailable state on the DARK 404", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: false, status: 404 });
    render(<WtmPreviewPage />);
    await waitFor(() => expect(screen.getByText(/not available in this environment/i)).toBeTruthy());
  });

  it("renders the SIMULATED scorecard with metrics and labels", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, status: 200, json: async () => PAYLOAD });
    render(<WtmPreviewPage />);
    await waitFor(() => expect(screen.getByTestId("wtm-scorecard")).toBeTruthy());
    expect(screen.getByText("INSUFFICIENT_VERIFIED_DATA")).toBeTruthy();   // authentic state shown
    expect(screen.getAllByText(/SIMULATED/i).length).toBeGreaterThan(0);    // clearly labelled synthetic
    expect(screen.getAllByText("TradersWay").length).toBeGreaterThan(0);    // option + scorecard heading
    expect(screen.getByText(/Illustration only/i)).toBeTruthy();            // not-a-deployed-integration notice
  });
});

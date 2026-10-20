/** Genuine evidence case study — DARK-graceful; renders the real redacted facts from the gated endpoint only. */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup } from "@testing-library/react";

import EvidenceCaseStudyPage from "./page";

const bi = (s: string) => ({ en: s, ja: s });
const PAYLOAD = {
  real: true, authenticated_on_production: true,
  title: bi("Genuine Withdrawal — Private Case Study"), badge: bi("REAL · AUTHENTICATED ON PRODUCTION"),
  lede: bi("The single genuine pilot withdrawal, redacted and separate from synthetic data."),
  facts: [
    { k: bi("Broker"), v: "TradersWay" }, { k: bi("External method"), v: "Ethereum" },
    { k: bi("Amount"), v: "24.12 USD" }, { k: bi("Email-to-email interval"), v: "33m01s (1981s)" },
  ],
  authentication: { en: ["DKIM pass + SPF pass."], ja: ["DKIM pass + SPF pass."] },
  attribution: { en: ["Owner-attributed; account UNRESOLVED."], ja: ["Owner-attributed; account UNRESOLVED."] },
  limits: { en: ["GuvFX never initiates/confirms/cancels."], ja: ["GuvFX never initiates/confirms/cancels."] },
  redaction: bi("No raw email, account number, confirmation link or wallet address is shown."),
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
beforeEach(() => vi.stubGlobal("fetch", vi.fn()));

describe("EvidenceCaseStudyPage", () => {
  it("shows the unavailable state on the DARK 404 (facts not served)", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: false, status: 404 });
    render(<EvidenceCaseStudyPage />);
    await waitFor(() => expect(screen.getByText(/not available in this environment/i)).toBeTruthy());
    expect(screen.queryByText("24.12 USD")).toBeNull();        // no real facts rendered when DARK
  });

  it("renders the genuine facts + REAL badge + redaction note when armed", async () => {
    (fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: true, status: 200, json: async () => PAYLOAD });
    render(<EvidenceCaseStudyPage />);
    await waitFor(() => expect(screen.getByText(/REAL · AUTHENTICATED ON PRODUCTION/i)).toBeTruthy());
    expect(screen.getByText("24.12 USD")).toBeTruthy();        // the genuine amount (not the synthetic $250)
    expect(screen.getByText(/33m01s/)).toBeTruthy();
    expect(screen.getByText("Ethereum")).toBeTruthy();
    expect(screen.getByText(/No raw email, account number/i)).toBeTruthy();
  });
});

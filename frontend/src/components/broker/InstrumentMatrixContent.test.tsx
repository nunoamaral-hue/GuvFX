import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { InstrumentMatrix } from "@/types/copy-trading";

/** Read-only matrix UX: quiet observation status (NO red banner), coverage-is-not-readiness note, ordered
 * pair counts + coverage, per-pair drill-down with native symbols, classification and material differences,
 * owner-scoped accounts, EN/JA and accessible controls. */

const { getInstrumentMatrix } = vi.hoisted(() => ({ getInstrumentMatrix: vi.fn() }));
vi.mock("@/lib/copy-trading-api", () => ({ getInstrumentMatrix }));

import { InstrumentMatrixContent } from "./InstrumentMatrixContent";

const es = { mode: "observation", execution: "off", label: "Observation mode", detail: "Copy execution off",
             note: "Catalogue coverage is not execution readiness." };

const matrix = (): InstrumentMatrix => ({
  enabled: true,
  execution_status: es,
  note: "Catalogue coverage is not execution readiness.",
  accounts: [
    { account_id: 46, broker: "Tradersway-Live", catalogue: { state: "synced", symbol_count: 84, enabled_count: 84, synced_at: "2026-10-25T07:22:00Z", age_seconds: 100 } },
    { account_id: 48, broker: "IS6Technologies-Live", catalogue: { state: "synced", symbol_count: 92, enabled_count: 92, synced_at: "2026-10-25T07:22:00Z", age_seconds: 100 } },
    { account_id: 49, broker: "PepperstoneUK-Live", catalogue: { state: "empty", symbol_count: 0, enabled_count: 0, synced_at: null, age_seconds: null } },
  ],
  directions: [
    {
      master_account_id: 46, master_broker: "Tradersway-Live",
      follower_account_id: 48, follower_broker: "IS6Technologies-Live",
      eligible_source: 2, counts: { VERIFIED: 1, UNVERIFIED: 1, AMBIGUOUS: 0, STALE: 0, INELIGIBLE: 0, ABSENT: 0 },
      coverage_pct: 50.0,
      candidates: [
        { master_symbol: "XAUUSD", follower_symbol: "XAUUSD.std", canonical: { slug: "METAL:XAU:USD", display: "Gold / USD" },
          classification: "VERIFIED", reason: "verified_symbol_map", confidence: "high",
          material_differences: {}, verified_at: "2026-10-24T16:19:37Z", verified_source: "29" },
        { master_symbol: "EURUSD", follower_symbol: "EURUSD.std", canonical: { slug: "FX:EUR:USD", display: "EUR / USD" },
          classification: "UNVERIFIED", reason: "needs_explicit_verification", confidence: "high",
          material_differences: {}, verified_at: null, verified_source: null },
      ],
    },
    {
      master_account_id: 46, master_broker: "Tradersway-Live",
      follower_account_id: 49, follower_broker: "PepperstoneUK-Live",
      eligible_source: 2, counts: { VERIFIED: 0, UNVERIFIED: 1, AMBIGUOUS: 1, STALE: 0, INELIGIBLE: 0, ABSENT: 0 },
      coverage_pct: 0.0,
      candidates: [
        { master_symbol: "XAUUSD", follower_symbol: "XAUUSD_SB", canonical: { slug: "METAL:XAU:USD", display: "Gold / USD" },
          classification: "AMBIGUOUS", reason: "material_incompatibility", confidence: "low",
          material_differences: {
            contract_size: { master: "100", follower: "1", differs: true },
            settlement_currency: { master: "USD", follower: "GBP", differs: true },
            contract_type: { master: "SPOT", follower: "SPREADBET", differs: true },
          }, verified_at: null, verified_source: null },
        { master_symbol: "EURUSD", follower_symbol: "EURUSD", canonical: { slug: "FX:EUR:USD", display: "EUR / USD" },
          classification: "UNVERIFIED", reason: "needs_explicit_verification", confidence: "high",
          material_differences: {}, verified_at: null, verified_source: null },
      ],
    },
  ],
});

beforeEach(() => getInstrumentMatrix.mockReset());

describe("InstrumentMatrixContent", () => {
  it("shows a calm not-available state for a non-cohort user", async () => {
    getInstrumentMatrix.mockResolvedValue({ enabled: false, accounts: [], directions: [], note: "" });
    render(<InstrumentMatrixContent lang="en" />);
    expect(await screen.findByTestId("matrix-not-available")).toBeTruthy();
    expect(screen.queryByTestId("matrix-pair-46-48")).toBeNull();
  });

  it("renders the quiet observation status and NO red SHADOW banner", async () => {
    getInstrumentMatrix.mockResolvedValue(matrix());
    render(<InstrumentMatrixContent lang="en" />);
    const status = await screen.findByTestId("matrix-status");
    expect(status.getAttribute("role")).toBe("status");
    expect(status.textContent).toMatch(/Observation mode/);
    expect(status.textContent).toMatch(/Copy execution off/);
    expect(document.body.textContent).not.toMatch(/SHADOW — EXECUTION OFF/);
  });

  it("states that catalogue coverage is not execution readiness", async () => {
    getInstrumentMatrix.mockResolvedValue(matrix());
    render(<InstrumentMatrixContent lang="en" />);
    expect((await screen.findByTestId("matrix-coverage-note")).textContent).toMatch(/not execution readiness/i);
  });

  it("shows ordered-pair rows with coverage, including the empty #49 catalogue", async () => {
    getInstrumentMatrix.mockResolvedValue(matrix());
    render(<InstrumentMatrixContent lang="en" />);
    expect((await screen.findByTestId("matrix-coverage-46-48")).textContent).toBe("50%");
    expect(screen.getByTestId("matrix-coverage-46-49").textContent).toBe("0%");
    expect(screen.getByTestId("matrix-catalogue-49").textContent).toMatch(/not synced/i);
  });

  it("drills into a pair: native symbols, VERIFIED chip and verification date", async () => {
    getInstrumentMatrix.mockResolvedValue(matrix());
    render(<InstrumentMatrixContent lang="en" />);
    fireEvent.click(await screen.findByTestId("matrix-inspect-46-48"));
    const row = screen.getByTestId("matrix-candidate-46-48-XAUUSD");
    expect(row.textContent).toMatch(/XAUUSD/);
    expect(row.textContent).toMatch(/XAUUSD\.std/);
    expect(row.textContent).toMatch(/Verified/);
    expect(row.textContent).toMatch(/2026-10-24/);
  });

  it("drills into 46→49: XAUUSD is AMBIGUOUS and shows the material differences (fail closed)", async () => {
    getInstrumentMatrix.mockResolvedValue(matrix());
    render(<InstrumentMatrixContent lang="en" />);
    fireEvent.click(await screen.findByTestId("matrix-inspect-46-49"));
    const row = screen.getByTestId("matrix-candidate-46-49-XAUUSD");
    expect(row.textContent).toMatch(/Ambiguous/);
    expect(row.textContent).toMatch(/USD→GBP/);     // P&L currency difference surfaced
    expect(row.textContent).toMatch(/100→1/);       // contract-size difference surfaced
  });

  it("renders Japanese labels", async () => {
    getInstrumentMatrix.mockResolvedValue(matrix());
    render(<InstrumentMatrixContent lang="ja" />);
    expect(await screen.findByText(/銘柄マッピング マトリクス/)).toBeTruthy();
    fireEvent.click(screen.getByTestId("matrix-inspect-46-49"));
    expect(screen.getByTestId("matrix-candidate-46-49-XAUUSD").textContent).toMatch(/曖昧/);
  });

  it("inspect control is an accessible toggle", async () => {
    getInstrumentMatrix.mockResolvedValue(matrix());
    render(<InstrumentMatrixContent lang="en" />);
    const btn = await screen.findByTestId("matrix-inspect-46-48");
    expect(btn.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(btn);
    expect(btn.getAttribute("aria-expanded")).toBe("true");
  });
});

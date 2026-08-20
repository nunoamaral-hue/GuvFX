import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

/** BETA MARKETPLACE CURATION — Wayond is featured + first, and non-usable research/prototype cards are
 * withheld from the beta catalogue. Presentation only: no execution/routing/sizing change. */

const { apiFetch, push } = vi.hoisted(() => {
  const push = vi.fn();
  const apiFetch = vi.fn(async (path: string) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.startsWith("/api/trading/accounts")) return [{ id: 5, name: "Demo A", is_demo: true, is_active: true }];
    if (path.includes("/signal-copy/status")) return { armed: false, enabled: false };
    return {};
  });
  return { apiFetch, push };
});

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/lib/hosted-journey", () => ({ fetchJourney: vi.fn(async () => ({ ok: false, unavailable: true })), authorizeExecution: vi.fn() }));
vi.mock("@/components/AppShell", () => ({ useLang: () => "en" }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: vi.fn() }) }));

import Marketplace from "./page";

const HIDDEN = [
  "London Session Box Breakout", "Trend EMA Crossover (HTF filter)", "Bollinger Mean Reversion",
  "Head & Shoulders Reversal", "Trendline Break Pocket", "Adaptive Liquidity Trap Scalper",
  "Structural Continuation Engine", "TBP V3 Hybrid Sleeve v1",
];

describe("beta marketplace curation", () => {
  beforeEach(() => { apiFetch.mockClear(); push.mockClear(); });

  it("shows ONLY Wayond and withholds every research/prototype card", async () => {
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByText("Wayond WIM Strategy")).toBeInTheDocument());
    for (const name of HIDDEN) {
      expect(screen.queryByText(name)).not.toBeInTheDocument();
    }
    // Exactly one strategy heading (Wayond) is rendered.
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["Wayond WIM Strategy"]);
  });

  it("Wayond is the FIRST card and carries the Featured badge (deterministic, not PK/date/alpha)", async () => {
    render(<Marketplace />);
    const first = await screen.findByRole("heading", { level: 3 });
    expect(first.textContent).toBe("Wayond WIM Strategy");
    // Only Wayond is shown, so the single Featured badge unambiguously marks it (badge text is "★ Featured").
    expect(screen.getByText(/Featured/)).toBeInTheDocument();
  });

  it("renders a truthful 'more coming soon' note (no fake cards, no promises)", async () => {
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByText(/more strategies coming soon/i)).toBeInTheDocument());
  });
});

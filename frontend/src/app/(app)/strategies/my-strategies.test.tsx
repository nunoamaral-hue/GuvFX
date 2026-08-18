import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

/** AJ#7.2 — My Strategies dedup. A signal-copy product the customer owns (Wayond today) must render EXACTLY
 * ONCE, in the managed "Automated strategies" section, with a customer-facing lifecycle chip. Its backing
 * generic Strategy row ("Wayond WIM Strategy") must NOT also appear in the generic list, and its
 * lifecycle must come from the enable-state — never from the backing Strategy's is_active "Active" badge. */

type Status = { armed: boolean; enabled: boolean; ambiguous?: boolean; strategy_id?: number | null };
type StrategyRow = { id: number; name: string; is_active: boolean; is_signal_copy_backed?: boolean };

const { state, apiFetch, fetchJourney } = vi.hoisted(() => {
  const state = {
    status: { armed: true, enabled: false, strategy_id: 10 } as Status,
    // The backing Wayond Strategy row (id 10) — server-flagged is_signal_copy_backed so the generic list hides
    // it — plus a genuinely-generic strategy that is not flagged.
    strategies: [
      { id: 10, name: "Wayond WIM Strategy", is_active: true, is_signal_copy_backed: true },
      { id: 3, name: "London Session Box", is_active: false, is_signal_copy_backed: false },
    ] as StrategyRow[],
    journey: { ok: true, journey: { execution_authorized: false, can_enable_automated_trading: false } } as {
      ok: boolean;
      journey: { execution_authorized?: boolean; can_enable_automated_trading?: boolean } | null;
    },
  };
  const apiFetch = vi.fn(async (path: string) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.includes("/signal-copy/status")) return { ...state.status };
    if (path.startsWith("/api/strategies/strategies/")) return [...state.strategies];
    return {};
  });
  const fetchJourney = vi.fn(async () => state.journey);
  return { state, apiFetch, fetchJourney };
});

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/lib/hosted-journey", () => ({ fetchJourney, authorizeExecution: vi.fn() }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));

import MyStrategies from "./page";

const setToken = () => {
  const store: Record<string, string> = { guvfx_access_token: "t" };
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store[k] ?? null,
    setItem: (k: string, v: string) => { store[k] = v; },
    removeItem: (k: string) => { delete store[k]; },
    clear: () => { for (const k of Object.keys(store)) delete store[k]; },
  });
};

describe("My Strategies — signal-copy dedup", () => {
  beforeEach(() => {
    apiFetch.mockClear(); fetchJourney.mockClear();
    state.status = { armed: true, enabled: false, strategy_id: 10 };
    state.strategies = [
      { id: 10, name: "Wayond WIM Strategy", is_active: true, is_signal_copy_backed: true },
      { id: 3, name: "London Session Box", is_active: false, is_signal_copy_backed: false },
    ];
    state.journey = { ok: true, journey: { execution_authorized: false, can_enable_automated_trading: false } };
    setToken();
  });

  it("owned-not-enabled: Wayond renders ONCE (managed section), backing generic row is hidden", async () => {
    render(<MyStrategies />);
    // Managed section shows the customer-facing name once.
    await waitFor(() => expect(screen.getByText("Automated strategies")).toBeTruthy());
    await waitFor(() => expect(screen.getByText("Wayond WIM")).toBeTruthy());
    // The backing generic row must NOT double-render.
    expect(screen.queryByText("Wayond WIM Strategy")).toBeNull();
    // Exactly one Wayond mention on the whole page.
    expect(screen.getAllByText(/Wayond/i)).toHaveLength(1);
    // Lifecycle chip is customer-facing (not the backing Strategy's "Active" badge).
    expect(screen.getByText("Setup required")).toBeTruthy();
  });

  it("owned-not-enabled + ready: chip is 'Ready to enable' with an Enable action, still one entry", async () => {
    state.journey = { ok: true, journey: { execution_authorized: true, can_enable_automated_trading: true } };
    render(<MyStrategies />);
    await waitFor(() => expect(screen.getByText("Ready to enable")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Enable" })).toBeTruthy();
    expect(screen.queryByText("Wayond WIM Strategy")).toBeNull();
    expect(screen.getAllByText(/Wayond/i)).toHaveLength(1);
  });

  it("enabled: chip is 'Enabled' with a Manage action, backing row still hidden", async () => {
    state.status = { armed: true, enabled: true, strategy_id: 10 };
    state.journey = { ok: true, journey: { execution_authorized: true, can_enable_automated_trading: true } };
    render(<MyStrategies />);
    await waitFor(() => expect(screen.getByText("Enabled")).toBeTruthy());
    expect(screen.getByRole("button", { name: "Manage" })).toBeTruthy();
    expect(screen.queryByText("Wayond WIM Strategy")).toBeNull();
    expect(screen.getAllByText(/Wayond/i)).toHaveLength(1);
  });

  it("generic strategies still render normally alongside the managed Wayond product", async () => {
    render(<MyStrategies />);
    // Generic strategy is shown with its own Active/Inactive badge.
    await waitFor(() => expect(screen.getByText("London Session Box")).toBeTruthy());
    expect(screen.getByText("Inactive")).toBeTruthy();
    // Wayond appears once, in the managed section; its backing generic row is hidden.
    expect(screen.getByText("Wayond WIM")).toBeTruthy();
    expect(screen.queryByText("Wayond WIM Strategy")).toBeNull();
  });

  it("status comes from lifecycle, not the backing Strategy.is_active: no 'Active' badge for Wayond", async () => {
    // Only the (active, server-flagged) backing Wayond strategy exists; a naive render would show "Active".
    state.strategies = [{ id: 10, name: "Wayond WIM Strategy", is_active: true, is_signal_copy_backed: true }];
    render(<MyStrategies />);
    await waitFor(() => expect(screen.getByText("Wayond WIM")).toBeTruthy());
    // Deduped out of the generic list → no "Active" badge anywhere, only the lifecycle chip.
    expect(screen.queryByText("Active")).toBeNull();
    expect(screen.getByText("Setup required")).toBeTruthy();
  });

  it("ambiguous ownership (server fails open — not flagged): nothing is deduped, product surfaces for attention", async () => {
    // Ambiguous config → the backend does NOT flag the backing row (is_signal_copy_backed false), so the
    // generic list is NOT silently hidden — the row stays visible for attention.
    state.status = { armed: true, enabled: false, ambiguous: true, strategy_id: null };
    state.strategies = [
      { id: 10, name: "Wayond WIM Strategy", is_active: true, is_signal_copy_backed: false },
      { id: 3, name: "London Session Box", is_active: false, is_signal_copy_backed: false },
    ];
    render(<MyStrategies />);
    await waitFor(() => expect(screen.getByText("Needs attention")).toBeTruthy());
    // With no resolvable strategy_id we do not hide the backing row (fail-open to visibility, not silence).
    expect(screen.getByText("Wayond WIM Strategy")).toBeTruthy();
  });
});

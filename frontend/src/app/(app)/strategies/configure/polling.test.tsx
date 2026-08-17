import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";

/** AJ#7.2 Phase 5 — Configure self-updating readiness. While the customer waits, the page polls readiness and
 * transitions from "getting ready" to "Ready to enable" IN PLACE — no navigation away and back. A transient
 * fetch miss must never downgrade a good UI, and polling must stop once the Enable action is shown. */

type Status = { armed: boolean; enabled: boolean; ambiguous?: boolean; account_id?: number | null };
const { state, apiFetch, push, fetchJourney, authorizeExecution } = vi.hoisted(() => {
  const state = {
    search: "mp=mp-010&account=5",
    status: { armed: true, enabled: false, account_id: 5 } as Status,
    journey: null as Record<string, unknown> | null,   // start NOT ready (getting ready)
    accounts: [{ id: 5, name: "Demo A", account_number: "1302587", is_demo: true, is_active: true }],
  };
  const push = vi.fn();
  const authorizeExecution = vi.fn(async () => ({}));
  const fetchJourney = vi.fn(async () => (state.journey ? { ok: true, journey: state.journey } : { ok: false, unavailable: true }));
  const apiFetch = vi.fn(async (path: string) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.startsWith("/api/trading/accounts")) return state.accounts;
    if (path.includes("/signal-copy/status")) return { ...state.status };
    return {};
  });
  return { state, apiFetch, push, fetchJourney, authorizeExecution };
});

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/lib/hosted-journey", () => ({ fetchJourney, authorizeExecution }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(state.search),
}));

import ConfigurePage from "./page";

const armCalls = () => apiFetch.mock.calls.filter((c) => String(c[0]).includes("/signal-copy/arm")).length;

describe("Configure page — Phase 5 self-updating readiness", () => {
  beforeEach(() => {
    // shouldAdvanceTime lets the initial async load settle via findBy/waitFor while still allowing us to
    // advance the poll interval explicitly and deterministically.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    apiFetch.mockClear(); push.mockClear(); authorizeExecution.mockClear(); fetchJourney.mockClear();
    state.search = "mp=mp-010&account=5";
    state.status = { armed: true, enabled: false, account_id: 5 };  // owned, not enabled
    state.journey = null;                                            // not ready yet → getting ready
  });
  afterEach(() => { vi.runOnlyPendingTimers(); vi.useRealTimers(); });

  it("transitions getting-ready → Ready to enable IN PLACE (no navigation), then stops polling", async () => {
    render(<ConfigurePage />);
    // Getting-ready state: no Enable button, an auto-update reassurance, MetaTrader forward action.
    await screen.findByText(/getting ready/i);
    expect(screen.queryByRole("button", { name: "Enable Strategy" })).toBeNull();
    expect(screen.getByText(/updates automatically/i)).toBeTruthy();

    // The workspace becomes ready between polls.
    state.journey = { execution_authorized: false, can_enable_automated_trading: true };
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });   // one poll cycle

    // Transitioned in place: Enable now offered, still on the same Configure page — never navigated.
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable Strategy" })).toBeTruthy());
    expect(screen.queryByText(/getting ready/i)).toBeNull();
    expect(push).not.toHaveBeenCalled();
    expect(armCalls()).toBe(0);            // polling never arms

    // Polling stops once ready: further time advances trigger no more journey fetches.
    const callsAfterReady = fetchJourney.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    expect(fetchJourney.mock.calls.length).toBe(callsAfterReady);
  });

  it("a transient fetch miss during polling never downgrades the UI back from ready", async () => {
    render(<ConfigurePage />);
    await screen.findByText(/getting ready/i);
    // Become ready.
    state.journey = { execution_authorized: false, can_enable_automated_trading: true };
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable Strategy" })).toBeTruthy());
    // Now simulate a transient outage; because we're no longer polling (ready), and even if a stale poll
    // resolved null, the fail-safe only applies non-null results — the UI stays on Ready to enable.
    state.journey = null;
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(screen.getByRole("button", { name: "Enable Strategy" })).toBeTruthy();
  });
});

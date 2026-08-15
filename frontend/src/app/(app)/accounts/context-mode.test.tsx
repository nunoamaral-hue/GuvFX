import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

/**
 * Product Consistency Pass (P0.1 / P1.3): the Accounts page ADAPTS to the customer's account model.
 * A Hosted Workspace customer (hosted journey open) sees a read-only status experience and NEVER the manual
 * "Add Trading Account" broker form; a Traditional customer (hosted journey 404) sees the manual form.
 * The two mental models are never shown together.
 */

const { apiFetch, fetchJourney } = vi.hoisted(() => ({
  apiFetch: vi.fn(async (path: string) => {
    if (path.startsWith("/api/trading/accounts")) return [];
    if (path.startsWith("/api/onboarding/setup-status")) return { stage: "unknown" }; // PostOnboardingSetupPanel → null
    return {};
  }),
  fetchJourney: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/lib/flags", () => ({ brokerConnectivityEnabled: () => false })); // DARK → legacy AccountsContent
vi.mock("@/components/AppShell", () => ({ useLang: () => "en" }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));
// Mock only the journey I/O; keep the real describeJourney the status panel depends on.
vi.mock("@/lib/hosted-journey", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hosted-journey")>();
  return { ...actual, fetchJourney };
});

import AccountsPage from "./page";
import type { HostedJourney } from "@/lib/hosted-journey";

const readyJourney: HostedJourney = {
  phase: "WORKSPACE_READY", next_action: "assign_strategy", confirmed: true,
  strategy_eligible: true, delivery: "DELIVERY_READY", active_login_masked: "***561",
};

describe("Accounts page — context-aware model (P0.1 / P1.3)", () => {
  beforeEach(() => { apiFetch.mockClear(); fetchJourney.mockReset(); });
  afterEach(() => cleanup());

  it("HOSTED customer → read-only status experience, and the manual broker form is NEVER shown", async () => {
    fetchJourney.mockResolvedValue({ ok: true, journey: readyJourney });
    const { container } = render(<AccountsPage />);
    // The hosted status panel renders...
    expect(await screen.findByRole("heading", { name: /hosted workspace/i })).toBeInTheDocument();
    // ...the page title reads hosted-consistent (P2: "Trading Workspace", not "Broker Accounts")...
    expect(screen.getByRole("heading", { name: "Trading Workspace" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Broker Accounts" })).toBeNull();
    // ...and the legacy "Add Trading Account" manual form is absent for hosted customers.
    expect(screen.queryByRole("button", { name: /^add account$/i })).toBeNull();
    expect(container.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByText(/two ways to connect a broker/i)).toBeNull();
  });

  it("TRADITIONAL customer (hosted journey unavailable) → the manual MT5 connection form IS shown", async () => {
    fetchJourney.mockResolvedValue({ ok: false, unavailable: true });
    const { container } = render(<AccountsPage />);
    // The manual form renders (password field + Add account button)...
    await waitFor(() => expect(container.querySelector('input[type="password"]')).not.toBeNull());
    expect(screen.getByRole("button", { name: /^add account$/i })).toBeInTheDocument();
    // ...and the hosted status panel is NOT shown (models are never mixed).
    expect(screen.queryByRole("heading", { name: /hosted workspace/i })).toBeNull();
  });

  it("on a transient (non-404) probe error → a neutral retry state, NEVER the credential form or hosted panel", async () => {
    fetchJourney.mockRejectedValue(new Error("boom")); // non-404 → fetchJourney rethrows
    const { container } = render(<AccountsPage />);
    await waitFor(() => expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument());
    // The manual credential form must NEVER appear for a customer whose model couldn't be determined.
    expect(container.querySelector('input[type="password"]')).toBeNull();
    expect(screen.queryByRole("button", { name: /^add account$/i })).toBeNull();
    // ...and the hosted status panel is not shown either (we don't have a journey).
    expect(screen.queryByRole("heading", { name: /hosted workspace/i })).toBeNull();
  });
});

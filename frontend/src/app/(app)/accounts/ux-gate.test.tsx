import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

/** Phase 9 — per-user Broker Accounts UX gate at the PAGE level. Proves the headline properties the hosted UX
 * depends on: (a) a NEUTRAL loader shows while the capability resolves — the wrong experience never flashes;
 * (b) a granted user gets the NEW experience; (c) an ungranted user AND any error fail closed to LEGACY. The
 * new body is stubbed so the test isolates the gate decision. */

const { apiFetch, getBrokerAccountsUxEnabled, fetchJourney } = vi.hoisted(() => ({
  apiFetch: vi.fn(async () => ([] as unknown)),
  getBrokerAccountsUxEnabled: vi.fn(),
  fetchJourney: vi.fn(async () => ({ ok: false, unavailable: true })),
}));

vi.mock("@/lib/flags", () => ({ brokerConnectivityEnabled: () => false })); // per-user path (global override OFF)
vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/lib/broker-api", () => ({
  getBrokerAccountsUxEnabled,
  listAccounts: vi.fn(async () => []), getBrokerStatus: vi.fn(async () => null),
  getEntitlementSummary: vi.fn(async () => null), openMt5Desktop: vi.fn(), setAccountActive: vi.fn(),
  getAccount: vi.fn(), getValidationHistory: vi.fn(async () => []),
}));
vi.mock("@/lib/hosted-journey", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hosted-journey")>();
  return { ...actual, fetchJourney };
});
vi.mock("@/components/AppShell", () => ({ useLang: () => "en" }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));
// Stub the NEW body so the gate decision is isolated from BrokerAccountsContent internals.
vi.mock("@/components/broker/BrokerAccountsContent", () => ({
  BrokerAccountsContent: () => <div data-testid="new-ux">NEW</div>,
}));

import AccountsPage from "./page";

describe("Broker Accounts per-user UX gate (Phase 9)", () => {
  beforeEach(() => { apiFetch.mockClear(); getBrokerAccountsUxEnabled.mockReset(); fetchJourney.mockClear(); });
  afterEach(() => cleanup());

  it("shows a NEUTRAL loader while the capability resolves — no wrong-experience flash", () => {
    // Capability never resolves within this tick → the page must show the neutral gate loader and NEITHER body.
    getBrokerAccountsUxEnabled.mockImplementation(() => new Promise<boolean>(() => {}));
    render(<AccountsPage />);
    expect(screen.getByTestId("ux-gate-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("new-ux")).not.toBeInTheDocument();   // new UX never flashes
    expect(screen.queryByText(/add trading account/i)).not.toBeInTheDocument(); // legacy body not shown yet
  });

  it("granted user → NEW experience (flag OFF, capability true)", async () => {
    getBrokerAccountsUxEnabled.mockResolvedValue(true);
    render(<AccountsPage />);
    await waitFor(() => expect(screen.getByTestId("new-ux")).toBeInTheDocument());
    expect(screen.queryByTestId("ux-gate-loading")).not.toBeInTheDocument();
  });

  it("ungranted user → LEGACY experience (capability false), never the new UX", async () => {
    getBrokerAccountsUxEnabled.mockResolvedValue(false);
    render(<AccountsPage />);
    await waitFor(() => expect(screen.queryByTestId("ux-gate-loading")).not.toBeInTheDocument());
    expect(screen.queryByTestId("new-ux")).not.toBeInTheDocument();   // fail-closed to legacy
  });

  it("capability lookup error → fail closed to LEGACY, never the new UX", async () => {
    getBrokerAccountsUxEnabled.mockRejectedValue(new Error("me unavailable"));
    render(<AccountsPage />);
    await waitFor(() => expect(screen.queryByTestId("ux-gate-loading")).not.toBeInTheDocument());
    expect(screen.queryByTestId("new-ux")).not.toBeInTheDocument();
  });
});

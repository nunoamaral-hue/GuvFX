import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";

/** AREA C (ADR-0031) — when the broker-connectivity journey is armed at build time, /accounts must
 * redirect to /broker-accounts DURING RENDER, before the legacy page fetches anything. With the flag
 * OFF (default) the legacy page renders and fetches exactly as before — no redirect. */
const { redirect, apiFetch } = vi.hoisted(() => ({
  redirect: vi.fn(() => { throw new Error("NEXT_REDIRECT"); }),
  apiFetch: vi.fn().mockResolvedValue([]),
}));
vi.mock("next/navigation", () => ({
  redirect,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
}));
vi.mock("next/link", () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/components/AppShell", () => ({ useLang: () => "en" }));

let enabled = false;
vi.mock("@/lib/flags", () => ({ brokerConnectivityEnabled: () => enabled }));

import AccountsPage from "./page";

describe("/accounts → /broker-accounts redirect (AREA C)", () => {
  beforeEach(() => { redirect.mockClear(); apiFetch.mockClear(); });

  it("redirects to /broker-accounts and makes NO fetch when the flag is ON", () => {
    enabled = true;
    expect(() => render(<AccountsPage />)).toThrow(/NEXT_REDIRECT/);
    expect(redirect).toHaveBeenCalledWith("/broker-accounts");
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("renders the legacy page and does NOT redirect when the flag is OFF", async () => {
    enabled = false;
    render(<AccountsPage />);
    expect(redirect).not.toHaveBeenCalled();
    // Legacy content mounted → its account-list load effect ran.
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
  });
});

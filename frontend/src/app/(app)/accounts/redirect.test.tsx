import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";

/** WS-A (packet: Customer Journey Consolidation) — /accounts is the SINGLE canonical broker-account page.
 * This asserts the consolidated routing and that it is loop-safe:
 *   • /accounts renders IN PLACE for both flag states (broker journey ON, legacy content OFF) — it never
 *     redirects, so /broker-accounts can safely redirect here.
 *   • /broker-accounts and /broker-accounts/[id] permanently redirect to the canonical /accounts tree.
 *   • /accounts/[id] renders the detail when the journey is built, and redirects to /accounts when OFF
 *     (the legacy page has no per-account detail) — never a dead end. */
const { redirect, apiFetch, broker } = vi.hoisted(() => ({
  redirect: vi.fn(() => { throw new Error("NEXT_REDIRECT"); }),
  apiFetch: vi.fn().mockResolvedValue([]),
  broker: {
    listAccounts: vi.fn().mockResolvedValue([]),
    getBrokerStatus: vi.fn().mockResolvedValue(null),
    getAccount: vi.fn().mockResolvedValue({ id: 123, name: "Demo", broker_name: "DemoBroker", account_number: "9001", is_active: true }),
    getValidationHistory: vi.fn().mockResolvedValue([]),
    retryValidation: vi.fn(), testConnection: vi.fn(),
  },
}));

let params: Record<string, string> = { id: "123" };
vi.mock("next/navigation", () => ({
  redirect,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  useParams: () => params,
}));
vi.mock("next/link", () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/lib/broker-api", () => broker);
vi.mock("@/components/AppShell", () => ({ useLang: () => "en" }));

let enabled = false;
vi.mock("@/lib/flags", () => ({ brokerConnectivityEnabled: () => enabled }));

import AccountsPage from "./page";
import AccountDetailPage from "./[id]/page";
import BrokerAccountsListRedirect from "../broker-accounts/page";
import BrokerAccountDetailRedirect from "../broker-accounts/[id]/page";

describe("canonical /accounts routing (WS-A)", () => {
  beforeEach(() => {
    redirect.mockClear(); apiFetch.mockClear();
    broker.listAccounts.mockClear(); broker.getAccount.mockClear();
    params = { id: "123" };
  });

  it("/accounts OFF renders the legacy page and does NOT redirect", async () => {
    enabled = false;
    render(<AccountsPage />);
    expect(redirect).not.toHaveBeenCalled();
    await waitFor(() => expect(apiFetch).toHaveBeenCalled()); // legacy load effect ran
    expect(broker.listAccounts).not.toHaveBeenCalled();
  });

  it("/accounts ON renders the broker journey IN PLACE and does NOT redirect", async () => {
    enabled = true;
    render(<AccountsPage />);
    expect(redirect).not.toHaveBeenCalled();
    await waitFor(() => expect(broker.listAccounts).toHaveBeenCalled()); // broker list effect ran
  });

  it("/broker-accounts permanently redirects to /accounts (no loop)", () => {
    expect(() => render(<BrokerAccountsListRedirect />)).toThrow(/NEXT_REDIRECT/);
    expect(redirect).toHaveBeenCalledWith("/accounts");
  });

  it("/broker-accounts/[id] permanently redirects to /accounts/[id]", () => {
    expect(() => render(<BrokerAccountDetailRedirect />)).toThrow(/NEXT_REDIRECT/);
    expect(redirect).toHaveBeenCalledWith("/accounts/123");
  });

  it("/accounts/[id] OFF redirects to /accounts (no dead end, legacy has no detail)", () => {
    enabled = false;
    expect(() => render(<AccountDetailPage />)).toThrow(/NEXT_REDIRECT/);
    expect(redirect).toHaveBeenCalledWith("/accounts");
  });

  it("/accounts/[id] ON renders the detail and does NOT redirect", async () => {
    enabled = true;
    render(<AccountDetailPage />);
    expect(redirect).not.toHaveBeenCalled();
    await waitFor(() => expect(broker.getAccount).toHaveBeenCalledWith(123));
  });
});

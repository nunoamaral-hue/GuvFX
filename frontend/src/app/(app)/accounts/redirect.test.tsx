import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";

/** WS-A (packet: Customer Journey Consolidation) — /accounts is the SINGLE canonical broker-account page.
 * This asserts the consolidated routing and that it is loop-safe:
 *   • /accounts renders IN PLACE for both flag states (broker journey ON, legacy content OFF) — it never
 *     redirects, so /broker-accounts can safely redirect here.
 *   • /broker-accounts and /broker-accounts/[id] permanently redirect to the canonical /accounts tree.
 *   • /accounts/[id] renders the account-management page (AccountManageContent) gated PER USER on the
 *     broker_accounts_ux capability — a non-granted user is routed to /accounts (never a dead end), and
 *     it no longer bounces on the legacy build flag (Phase 2, WAYOND account-scoped strategy packet). */
const { redirect, replace, apiFetch, broker } = vi.hoisted(() => ({
  redirect: vi.fn(() => { throw new Error("NEXT_REDIRECT"); }),
  replace: vi.fn(),
  apiFetch: vi.fn().mockResolvedValue([]),
  broker: {
    listAccounts: vi.fn().mockResolvedValue([]),
    getBrokerStatus: vi.fn().mockResolvedValue(null),
    getAccount: vi.fn().mockResolvedValue({ id: 123, name: "Demo", broker_name: "DemoBroker", account_number: "9001", is_active: true }),
    getValidationHistory: vi.fn().mockResolvedValue([]),
    retryValidation: vi.fn(), testConnection: vi.fn(),
    getEntitlementSummary: vi.fn().mockResolvedValue(null),
    getDeliveryState: vi.fn().mockResolvedValue(null),
    openMt5Desktop: vi.fn(), setAccountActive: vi.fn(),
    getBrokerAccountsUxEnabled: vi.fn().mockResolvedValue(false), // per-user UX gate → legacy by default
  },
}));

let params: Record<string, string> = { id: "123" };
vi.mock("next/navigation", () => ({
  redirect,
  useRouter: () => ({ push: vi.fn(), replace, refresh: vi.fn() }),
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
    redirect.mockClear(); replace.mockClear(); apiFetch.mockClear();
    broker.listAccounts.mockClear(); broker.getAccount.mockClear();
    broker.getBrokerAccountsUxEnabled.mockClear().mockResolvedValue(false);
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

  it("/accounts/[id] renders the account-management page (no page-level flag redirect); gates PER USER", async () => {
    // Phase 2 — the page no longer bounces on the legacy build flag; it renders AccountManageContent, which
    // gates on the per-user broker_accounts_ux capability. A non-granted user is sent to /accounts via the
    // router (not a page-level redirect() dead end).
    broker.getBrokerAccountsUxEnabled.mockResolvedValue(false);
    render(<AccountDetailPage />);
    expect(redirect).not.toHaveBeenCalled();                       // no page-level flag redirect any more
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/accounts"));
    expect(broker.getAccount).not.toHaveBeenCalled();              // legacy gate → no account load
  });

  it("/accounts/[id] with broker UX granted renders the detail and does NOT redirect", async () => {
    broker.getBrokerAccountsUxEnabled.mockResolvedValue(true);
    render(<AccountDetailPage />);
    expect(redirect).not.toHaveBeenCalled();
    await waitFor(() => expect(broker.getAccount).toHaveBeenCalledWith(123));
    expect(replace).not.toHaveBeenCalled();
  });
});

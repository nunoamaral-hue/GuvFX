import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render } from "@testing-library/react";

/** WS-A (packet: Customer Journey Consolidation) — /broker-accounts is deprecated: it no longer renders
 * broker content or fetches anything, it PERMANENTLY redirects into the canonical /accounts tree. This is
 * the successor to the old flag-gate security test (which asserted a 404 when OFF): the load-bearing
 * property is now "the deprecated route serves NO broker content and makes NO broker API call — it only
 * redirects", regardless of the build flag. The canonical page's own flag behaviour is covered in
 * ../accounts/redirect.test.tsx. */
const { redirect, listAccounts, getAccount } = vi.hoisted(() => ({
  redirect: vi.fn(() => { throw new Error("NEXT_REDIRECT"); }),
  listAccounts: vi.fn().mockResolvedValue([]),
  getAccount: vi.fn().mockResolvedValue({}),
}));
vi.mock("next/navigation", () => ({ redirect, useParams: () => ({ id: "1" }) }));
vi.mock("next/link", () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/lib/broker-api", () => ({
  listAccounts, getAccount, getBrokerStatus: vi.fn(), getValidationHistory: vi.fn(),
  testConnection: vi.fn(), retryValidation: vi.fn(), replaceCredentials: vi.fn(),
  disconnectAccount: vi.fn(), createAccount: vi.fn(),
}));

import BrokerAccountsPage from "./page";
import BrokerAccountDetailPage from "./[id]/page";

describe("/broker-accounts is a deprecated redirect (WS-A)", () => {
  beforeEach(() => { redirect.mockClear(); listAccounts.mockClear(); getAccount.mockClear(); });

  it("list route redirects to /accounts and makes NO broker API call", () => {
    expect(() => render(<BrokerAccountsPage />)).toThrow(/NEXT_REDIRECT/);
    expect(redirect).toHaveBeenCalledWith("/accounts");
    expect(listAccounts).not.toHaveBeenCalled();
  });

  it("detail route redirects to /accounts/[id] and makes NO broker API call", () => {
    expect(() => render(<BrokerAccountDetailPage />)).toThrow(/NEXT_REDIRECT/);
    expect(redirect).toHaveBeenCalledWith("/accounts/1");
    expect(getAccount).not.toHaveBeenCalled();
  });
});

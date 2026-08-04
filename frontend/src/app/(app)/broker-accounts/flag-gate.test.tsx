import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";

/** WP4.2 — the load-bearing security test: with the flag OFF the page must 404 BEFORE any API call, and
 * with it ON the page must render and fetch. */
const { notFound, listAccounts } = vi.hoisted(() => ({
  notFound: vi.fn(() => { throw new Error("NEXT_NOT_FOUND"); }),
  listAccounts: vi.fn().mockResolvedValue([]),
}));
vi.mock("next/navigation", () => ({ notFound, useParams: () => ({ id: "1" }) }));
vi.mock("next/link", () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/lib/broker-api", () => ({
  listAccounts, getBrokerStatus: vi.fn(), getAccount: vi.fn(), getValidationHistory: vi.fn(),
  testConnection: vi.fn(), retryValidation: vi.fn(), replaceCredentials: vi.fn(),
  disconnectAccount: vi.fn(), createAccount: vi.fn(),
}));

let enabled = false;
vi.mock("@/lib/flags", () => ({ brokerConnectivityEnabled: () => enabled }));

import BrokerAccountsPage from "./page";
import BrokerAccountDetailPage from "./[id]/page";

describe("broker-accounts flag gate (list)", () => {
  beforeEach(() => { notFound.mockClear(); listAccounts.mockClear(); });

  it("404s and makes NO API call when the flag is OFF", () => {
    enabled = false;
    expect(() => render(<BrokerAccountsPage />)).toThrow(/NEXT_NOT_FOUND/);
    expect(notFound).toHaveBeenCalled();
    expect(listAccounts).not.toHaveBeenCalled();
  });

  it("renders and fetches when the flag is ON", async () => {
    enabled = true;
    render(<BrokerAccountsPage />);
    await waitFor(() => expect(listAccounts).toHaveBeenCalled());
    expect(notFound).not.toHaveBeenCalled();
  });
});

describe("broker-accounts flag gate (detail)", () => {
  beforeEach(() => { notFound.mockClear(); });
  it("404s when the flag is OFF", () => {
    enabled = false;
    expect(() => render(<BrokerAccountDetailPage />)).toThrow(/NEXT_NOT_FOUND/);
    expect(notFound).toHaveBeenCalled();
  });
});

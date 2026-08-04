import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

/** WP5.3 — the load-bearing security test for the Operations & Support surface. Two independent gates:
 *   (1) build flag OFF  → notFound() BEFORE any hook/API call (no route, no fetch, no leak);
 *   (2) flag ON but non-operator → "Restricted", and STILL no API call (owner data never fetched).
 * The backend independently enforces owner-scoping; this proves the frontend never bypasses either gate. */
const { notFound, listAccounts, getAccountEvents, admin } = vi.hoisted(() => ({
  notFound: vi.fn(() => { throw new Error("NEXT_NOT_FOUND"); }),
  listAccounts: vi.fn().mockResolvedValue([]),
  getAccountEvents: vi.fn(),
  admin: { loading: false, authorized: true },
}));

const SUMMARY = {
  account_id: 7, generated_at: "2026-08-01T00:00:00Z",
  validation_state: { status: "VALIDATED", validated_at: null },
  health_state: { state: "HEALTHY", available: true, eligible: true },
  runtime_pause: { paused: false, live_paused: false },
  credential_status: { present: true, state: "ACTIVE" },
  disconnect_state: { disconnected: false, disconnected_at: null },
  latest_validation: null, latest_error: null, latest_warning: null,
  event_counts: { total: 0, open: 0, by_severity: {}, by_category: {} },
  last_update: null,
};

vi.mock("next/navigation", () => ({ notFound, useParams: () => ({ id: "7" }) }));
vi.mock("next/link", () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/components/admin/useAdminRole", () => ({ useAdminRole: () => admin }));
vi.mock("@/lib/broker-api", () => ({ listAccounts }));
vi.mock("@/lib/operations-api", () => ({ getAccountEvents }));

let enabled = false;
vi.mock("@/lib/flags", () => ({ operationsEnabled: () => enabled }));

import OperationsAccountsPage from "./page";
import OperationsAccountDetailPage from "./[id]/page";

beforeEach(() => {
  notFound.mockClear(); listAccounts.mockClear(); getAccountEvents.mockClear();
  getAccountEvents.mockResolvedValue({ summary: SUMMARY, timeline: [] });
  admin.loading = false; admin.authorized = true;
});

describe("operations accounts list — gates", () => {
  it("flag OFF → 404 and NO API call", () => {
    enabled = false;
    expect(() => render(<OperationsAccountsPage />)).toThrow(/NEXT_NOT_FOUND/);
    expect(notFound).toHaveBeenCalled();
    expect(listAccounts).not.toHaveBeenCalled();
  });

  it("flag ON + operator → renders and fetches", async () => {
    enabled = true; admin.authorized = true;
    render(<OperationsAccountsPage />);
    await waitFor(() => expect(listAccounts).toHaveBeenCalled());
    expect(notFound).not.toHaveBeenCalled();
  });

  it("flag ON + NON-operator → Restricted and NO API call", () => {
    enabled = true; admin.authorized = false;
    render(<OperationsAccountsPage />);
    expect(screen.getByText("Restricted")).toBeInTheDocument();
    expect(listAccounts).not.toHaveBeenCalled();
  });
});

describe("operations account detail — gates", () => {
  it("flag OFF → 404 and NO API call", () => {
    enabled = false;
    expect(() => render(<OperationsAccountDetailPage />)).toThrow(/NEXT_NOT_FOUND/);
    expect(notFound).toHaveBeenCalled();
    expect(getAccountEvents).not.toHaveBeenCalled();
  });

  it("flag ON + operator → fetches the account's events", async () => {
    enabled = true; admin.authorized = true;
    render(<OperationsAccountDetailPage />);
    await waitFor(() => expect(getAccountEvents).toHaveBeenCalled());
    expect(getAccountEvents.mock.calls[0][0]).toBe(7);
  });

  it("flag ON + NON-operator → Restricted and NO API call", () => {
    enabled = true; admin.authorized = false;
    render(<OperationsAccountDetailPage />);
    expect(screen.getByText("Restricted")).toBeInTheDocument();
    expect(getAccountEvents).not.toHaveBeenCalled();
  });
});

import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

/** WP5.3 — pagination boundary. The detail page peeks PAGE+1 rows: it shows only PAGE and enables "Next"
 * iff a (PAGE+1)-th row exists — so an exactly-full final page never leaves "Next" enabled onto an empty
 * page. */
const PAGE = 50;
const { notFound, getAccountEvents, admin } = vi.hoisted(() => ({
  notFound: vi.fn(() => { throw new Error("NEXT_NOT_FOUND"); }),
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
const mkEvents = (n: number) => Array.from({ length: n }, (_, i) => ({
  id: i + 1, timestamp: "2026-08-01T10:00:00Z", account_id: 7, runtime_uuid: "rt",
  category: "HEALTH", event_type: "e", severity: "INFO", status: "OPEN", reason_code: "r",
  summary: `ev ${i + 1}`, source: "s", correlation_id: "c", state_version: 1, actor: "system",
  customer_visible: true, resolved: false, resolved_at: null, metadata: {},
}));

vi.mock("next/navigation", () => ({ notFound, useParams: () => ({ id: "7" }) }));
vi.mock("next/link", () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));
vi.mock("@/components/admin/useAdminRole", () => ({ useAdminRole: () => admin }));
vi.mock("@/lib/operations-api", () => ({ getAccountEvents }));
vi.mock("@/lib/flags", () => ({ operationsEnabled: () => true }));

import OperationsAccountDetailPage from "./[id]/page";

beforeEach(() => { getAccountEvents.mockReset(); });

describe("detail pagination boundary", () => {
  it("requests PAGE+1 and disables Next when the peek row is absent (exactly-full final page)", async () => {
    // API returns exactly PAGE rows for a PAGE+1 request -> no more data.
    getAccountEvents.mockResolvedValue({ summary: SUMMARY, timeline: mkEvents(PAGE) });
    render(<OperationsAccountDetailPage />);
    await waitFor(() => expect(screen.getByText(`Showing ${PAGE} of ${PAGE} on this page`)).toBeInTheDocument());
    expect(getAccountEvents.mock.calls[0][1]).toMatchObject({ limit: PAGE + 1, offset: 0 });
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  });

  it("shows only PAGE rows and enables Next when a peek row exists", async () => {
    getAccountEvents.mockResolvedValue({ summary: SUMMARY, timeline: mkEvents(PAGE + 1) });
    render(<OperationsAccountDetailPage />);
    await waitFor(() => expect(screen.getByText(`Showing ${PAGE} of ${PAGE} on this page`)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Next" })).not.toBeDisabled();
    // the (PAGE+1)-th row is not rendered
    expect(screen.queryByText(`ev ${PAGE + 1}`)).toBeNull();
    expect(screen.getByText(`ev ${PAGE}`)).toBeInTheDocument();
  });
});

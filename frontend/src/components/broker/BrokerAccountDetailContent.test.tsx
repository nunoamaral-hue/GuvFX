import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

/** Internal-pilot broker-validation remediation (packet WS-G/H/K).
 *
 * The beta account's validation fails server-side (reason_code=validation_unconfigured: the validation
 * service isn't provisioned yet). This asserts the DETAIL view is honest about that:
 *   - status is shown as clearly-labelled, non-overlapping concepts (no three bare overlapping badges);
 *   - after failed attempts it reads "No successful validation yet", never "Never validated";
 *   - clicking Test connection creates a fresh attempt and renders a SERVICE-SIDE message — never the
 *     accusatory "check your details" — for a validation_unconfigured outcome.
 */
const api = vi.hoisted(() => ({
  getAccount: vi.fn(), getBrokerStatus: vi.fn(), getValidationHistory: vi.fn(),
  testConnection: vi.fn(), retryValidation: vi.fn(),
}));
vi.mock("@/lib/broker-api", () => api);
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "12" }),
  notFound: vi.fn(),
}));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));

import { BrokerAccountDetailContent } from "./BrokerAccountDetailContent";

const ACCOUNT = {
  id: 12, name: "IS6 Technologies LTD", broker_name: "IS6Technologies-Demo",
  server_name: "IS6Technologies-Demo", account_number: "1302575", is_active: false,
};
function attempt(over: Record<string, unknown> = {}) {
  return {
    id: 4, trigger: "test", status: "UNAVAILABLE", reason_code: "validation_unconfigured",
    retryable: true, is_demo: null, server: "IS6Technologies-Demo", login_masked: "***575",
    correlation_id: "validate-acct-12-abc", created_at: "2026-08-05T10:09:54Z", ...over,
  };
}
const STATUS = {
  validation_status: "TECHNICAL_ERROR", validated_at: null, is_active: false,
  disconnected_at: null, latest_attempt: attempt(),
};

describe("BrokerAccountDetailContent — honest validation status (WS-G/H/K)", () => {
  beforeEach(() => {
    Object.values(api).forEach((f) => f.mockReset());
    api.getAccount.mockResolvedValue(ACCOUNT);
    api.getBrokerStatus.mockResolvedValue(STATUS);
    api.getValidationHistory.mockResolvedValue([attempt()]);
  });

  it("shows clearly-labelled Broker connection + Trading account concepts", async () => {
    render(<BrokerAccountDetailContent />);
    await waitFor(() => expect(screen.getByText("Broker connection")).toBeInTheDocument());
    expect(screen.getByText("Trading account")).toBeInTheDocument();
  });

  it("reads 'No successful validation yet' after failed attempts, never 'Never validated'", async () => {
    render(<BrokerAccountDetailContent />);
    await waitFor(() => expect(screen.getByText(/No successful validation yet/i)).toBeInTheDocument());
    expect(screen.queryByText(/Never validated/i)).toBeNull();
  });

  it("Test connection opens a modal with a spinner, then shows a SERVICE-SIDE result (not 'check your details')", async () => {
    // Hold the request open so the spinner (running phase) is observable before the result renders.
    let resolveTest: (v: unknown) => void = () => {};
    api.testConnection.mockReturnValue(new Promise((r) => { resolveTest = r; }));
    render(<BrokerAccountDetailContent />);
    const btn = await screen.findByRole("button", { name: /Test connection/i });
    fireEvent.click(btn);
    // modal opens immediately with a spinner — the page never hangs inline
    expect(await screen.findByRole("status", { name: /Testing/i })).toBeInTheDocument();
    resolveTest(attempt());                                        // fresh validation_unconfigured outcome
    await waitFor(() => expect(api.testConnection).toHaveBeenCalledWith(12));
    // status + history are re-fetched after the attempt (fresh, not cached)
    await waitFor(() => expect(api.getValidationHistory).toHaveBeenCalledTimes(2));
    await waitFor(() =>
      expect(document.body.textContent).toMatch(/broker validation isn't available for your account/i));
    expect(document.body.textContent).toMatch(/weren't changed/i);
    expect(document.body.textContent).not.toMatch(/check your details/i);
  });

  it("shows a customer-safe message on a transport failure — NEVER 'Failed to fetch'", async () => {
    // The gunicorn-killed request surfaces as a tagged network error; the modal must stay customer-safe.
    api.testConnection.mockRejectedValue(Object.assign(new Error("network_unreachable"), { kind: "network" }));
    render(<BrokerAccountDetailContent />);
    fireEvent.click(await screen.findByRole("button", { name: /Test connection/i }));
    await waitFor(() =>
      expect(document.body.textContent).toMatch(/couldn't reach the validation service/i));
    expect(document.body.textContent).not.toMatch(/Failed to fetch|TypeError|network_unreachable/i);
  });
});

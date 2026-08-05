import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";

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
  testConnection: vi.fn(), retryValidation: vi.fn(), recoverAttemptAfterTransportFailure: vi.fn(),
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
    // Support-only outcome: guidance is in the message; NO misleading in-modal action, and never a dead end.
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).queryByRole("button", { name: /Try again/i })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /Replace credentials/i })).toBeNull();
    // The footer Close button (distinct from the header "×", whose accessible name is also "Close").
    expect(within(dialog).getByText("Close")).toBeInTheDocument();
  });

  it("recovers-or-safe-messages on transport failure — NEVER 'Failed to fetch', offers Try again", async () => {
    // The gunicorn-killed / dropped request surfaces as a tagged network error. Here the backend committed
    // no newer attempt, so recovery finds nothing and the modal shows a customer-safe transient message with
    // a retry affordance — never the raw transport text.
    api.testConnection.mockRejectedValue(Object.assign(new Error("network_unreachable"), { kind: "network" }));
    api.recoverAttemptAfterTransportFailure.mockResolvedValue(null);
    render(<BrokerAccountDetailContent />);
    fireEvent.click(await screen.findByRole("button", { name: /Test connection/i }));
    await waitFor(
      () => expect(document.body.textContent).toMatch(/couldn't reach the validation service/i),
      { timeout: 3000 });
    expect(document.body.textContent).not.toMatch(/Failed to fetch|TypeError|network_unreachable/i);
    expect(within(screen.getByRole("dialog")).getByRole("button", { name: /Try again/i })).toBeInTheDocument();
  });

  it("graceful reconnect: if the backend completed despite a dropped connection, shows the REAL result", async () => {
    // Transport failure on the POST, but the backend committed a fresh HEALTHY attempt. Recovery returns it,
    // and the modal presents the completed success — not a transport error. (The recovery function's own
    // history-polling logic is unit-tested in broker-api.test.ts.)
    api.testConnection.mockRejectedValue(Object.assign(new Error("network_unreachable"), { kind: "network" }));
    api.recoverAttemptAfterTransportFailure.mockResolvedValue(
      attempt({ id: 9, status: "HEALTHY", reason_code: "demo_ok" }));
    render(<BrokerAccountDetailContent />);
    fireEvent.click(await screen.findByRole("button", { name: /Test connection/i }));
    await waitFor(() => expect(document.body.textContent).toMatch(/Your broker connection is verified/i));
    expect(document.body.textContent).not.toMatch(/Failed to fetch|couldn't reach the validation service/i);
  });

  it("credential failure offers Replace credentials (not Try again) and the action fires", async () => {
    api.testConnection.mockResolvedValue(attempt({ status: "UNAVAILABLE", reason_code: "invalid_password" }));
    render(<BrokerAccountDetailContent />);
    fireEvent.click(await screen.findByRole("button", { name: /Test connection/i }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: /Replace credentials/i })).toBeInTheDocument());
    expect(within(dialog).queryByRole("button", { name: /Try again/i })).toBeNull();
    // The next-step action closes the result modal (opening the replace flow).
    fireEvent.click(within(dialog).getByRole("button", { name: /Replace credentials/i }));
    await waitFor(() => expect(screen.queryByText(/password was not accepted/i)).toBeNull());
  });

  it("transient failure offers Try again, which runs another validation", async () => {
    api.testConnection.mockResolvedValue(attempt({ status: "UNAVAILABLE", reason_code: "login_timeout" }));
    render(<BrokerAccountDetailContent />);
    fireEvent.click(await screen.findByRole("button", { name: /Test connection/i }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(within(dialog).getByRole("button", { name: /Try again/i })).toBeInTheDocument());
    expect(within(dialog).queryByRole("button", { name: /Replace credentials/i })).toBeNull();
    fireEvent.click(within(dialog).getByRole("button", { name: /Try again/i }));
    await waitFor(() => expect(api.testConnection).toHaveBeenCalledTimes(2));
  });

  it("duplicate clicks create only ONE validation attempt", async () => {
    let resolveTest: (v: unknown) => void = () => {};
    api.testConnection.mockReturnValue(new Promise((r) => { resolveTest = r; }));
    render(<BrokerAccountDetailContent />);
    const btn = await screen.findByRole("button", { name: /Test connection/i });
    fireEvent.click(btn);
    fireEvent.click(btn);                       // second click: in-flight guard + disabled button
    expect(await screen.findByRole("status", { name: /Testing/i })).toBeInTheDocument();
    expect(btn).toBeDisabled();
    expect(api.testConnection).toHaveBeenCalledTimes(1);
    resolveTest(attempt());
  });

  it("validation-host/IPC failure: safe message, NO broker-outage claim, NO Try again (no retry storm)", async () => {
    api.testConnection.mockResolvedValue(attempt({ status: "UNAVAILABLE", reason_code: "validation_ipc_unavailable" }));
    render(<BrokerAccountDetailContent />);
    fireEvent.click(await screen.findByRole("button", { name: /Test connection/i }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() =>
      expect(within(dialog).getByText(/couldn't start the secure broker-validation session/i)).toBeInTheDocument());
    // never a broker-outage claim, never a raw internal, and no immediate-retry affordance
    expect(dialog.textContent).not.toMatch(/unavailable|\bIPC\b|Session\s*0|\bMT5\b|10004/i);
    expect(within(dialog).queryByRole("button", { name: /Try again/i })).toBeNull();
    expect(within(dialog).queryByRole("button", { name: /Replace credentials/i })).toBeNull();
    expect(within(dialog).getByText("Close")).toBeInTheDocument();
  });
});

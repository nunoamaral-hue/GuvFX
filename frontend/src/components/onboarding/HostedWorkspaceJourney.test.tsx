import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock only the journey I/O; keep the real (unit-tested) describeJourney/STEPS state machine.
const jm = vi.hoisted(() => ({
  fetchJourney: vi.fn(),
  requestWorkspace: vi.fn(),
  bindExpectedAccount: vi.fn(),
  confirmAccount: vi.fn(),
}));
vi.mock("@/lib/hosted-journey", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hosted-journey")>();
  return { ...actual, ...jm };
});

import { HostedWorkspaceJourney } from "@/components/onboarding/HostedWorkspaceJourney";
import type { HostedJourney } from "@/lib/hosted-journey";

function journey(over: Partial<HostedJourney> = {}): HostedJourney {
  return {
    phase: "WORKSPACE_READY", next_action: "assign_strategy", confirmed: true,
    strategy_eligible: true, delivery: "DELIVERY_READY", active_login_masked: "***561", ...over,
  };
}

// Any legacy account-creation affordance that must NOT appear on the hosted journey.
const LEGACY = [/create account/i, /add account/i, /new account/i, /connect a broker account/i];

beforeEach(() => { jm.fetchJourney.mockReset(); jm.requestWorkspace.mockReset(); jm.bindExpectedAccount.mockReset(); jm.confirmAccount.mockReset(); });
afterEach(() => cleanup());

describe("HostedWorkspaceJourney", () => {
  it("renders the ready state with a live Launch link to the existing terminal", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_READY" }) });
    render(<HostedWorkspaceJourney />);
    const link = await screen.findByRole("link", { name: /open mt5/i });
    expect(link).toHaveAttribute("href", "/trading/terminal-access");
    expect(link.className).not.toContain("pointer-events-none");
  });

  it("gates the Launch link to 'preparing' when delivery is not ready", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_PREPARING" }) });
    render(<HostedWorkspaceJourney />);
    const link = await screen.findByRole("link", { name: /preparing your terminal/i });
    expect(link.className).toContain("pointer-events-none");
  });

  it("fails closed to a neutral 'not available' when the journey is dark (404)", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: false, unavailable: true });
    render(<HostedWorkspaceJourney />);
    expect(await screen.findByText(/isn't available yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
  });

  it("fails closed to a retryable error state when the load throws", async () => {
    jm.fetchJourney.mockRejectedValueOnce(new Error("boom"));
    render(<HostedWorkspaceJourney />);
    expect(await screen.findByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("requests a workspace with NO broker details (deferred bind) — bare button, no inputs", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({ phase: "NO_WORKSPACE", next_action: "request_workspace", delivery: "DELIVERY_NOT_AVAILABLE" }) });
    jm.requestWorkspace.mockResolvedValue(journey({ phase: "WORKSPACE_REQUESTED", next_action: "wait" }));
    const { container } = render(<HostedWorkspaceJourney />);
    const button = await screen.findByRole("button", { name: /request workspace/i });
    // No broker inputs and certainly no password field at the request step — identity is declared later.
    expect(container.querySelector("input")).toBeNull();
    fireEvent.click(button);
    await waitFor(() => expect(jm.requestWorkspace).toHaveBeenCalledTimes(1));
    expect(jm.requestWorkspace.mock.calls[0].length).toBe(0);   // called with NO arguments
  });

  it("declares the broker identity at the connect step (deferred bind) — never a password", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_READY" }) });
    jm.bindExpectedAccount.mockResolvedValue(journey({ phase: "BROKER_CONNECTED", next_action: "open_mt5_and_log_in" }));
    const { container } = render(<HostedWorkspaceJourney />);
    const input = await screen.findByPlaceholderText(/broker account number/i);
    expect(container.querySelector('input[type="password"]')).toBeNull();
    fireEvent.change(input, { target: { value: "700900" } });
    fireEvent.click(screen.getByRole("button", { name: /save my broker details/i }));
    await waitFor(() => expect(jm.bindExpectedAccount).toHaveBeenCalledTimes(1));
    const arg = jm.bindExpectedAccount.mock.calls[0][0];
    expect(arg.expected_login).toBe("700900");
    expect(JSON.stringify(arg).toLowerCase()).not.toContain("password");
  });

  it("never renders a legacy account-creation path or leaks internal identifiers", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({ phase: "ACCOUNT_CONFIRMATION_REQUIRED", next_action: "confirm_broker_account", delivery: "DELIVERY_READY" }) });
    const { container } = render(<HostedWorkspaceJourney />);
    await screen.findByRole("heading", { name: /confirm your account/i });
    for (const rx of LEGACY) expect(screen.queryByText(rx)).toBeNull();
    // No link back into the legacy /accounts creation surface.
    const hrefs = Array.from(container.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain("/accounts");
    const blob = (container.textContent || "").toLowerCase();
    for (const bad of ["canonical", "provisioning", "execution_node", "rdp_host", "guvfx_u_", "auto_shadow"]) {
      expect(blob).not.toContain(bad);
    }
  });
});

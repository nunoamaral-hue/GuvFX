import React from "react";
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));

// ADR-0047 — stub only the authorize call; keep the real describeJourney the component also imports.
const { authorizeExecution } = vi.hoisted(() => ({ authorizeExecution: vi.fn(async () => ({})) }));
vi.mock("@/lib/hosted-journey", async (importOriginal) => {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const actual = await importOriginal<any>();
  return { ...actual, authorizeExecution };
});

import { HostedWorkspaceStatus } from "./HostedWorkspaceStatus";
import type { HostedJourney } from "@/lib/hosted-journey";

function journey(over: Partial<HostedJourney> = {}): HostedJourney {
  return {
    phase: "WORKSPACE_READY",
    next_action: "assign_strategy",
    confirmed: true,
    strategy_eligible: true,
    delivery: "DELIVERY_READY",
    active_login_masked: "***561",
    identity_declared: true,
    ...over,
  };
}

const demoAccount = { id: 19, name: "My Demo Account", account_number: "700900", is_active: true, is_demo: true };

describe("HostedWorkspaceStatus (P0.1)", () => {
  it("renders a read-only status experience — never a broker-credential form", () => {
    const { container } = render(<HostedWorkspaceStatus journey={journey()} accounts={[demoAccount]} />);
    expect(screen.getByRole("heading", { name: /hosted workspace/i })).toBeInTheDocument();
    // Status rows are present.
    expect(screen.getByText("Workspace status")).toBeInTheDocument();
    expect(screen.getByText("MetaTrader terminal")).toBeInTheDocument();
    expect(screen.getByText("Broker account")).toBeInTheDocument();
    expect(screen.getByText("Trading readiness")).toBeInTheDocument();
    // A hosted customer NEVER enters a broker server / login / password here.
    expect(container.querySelector("input")).toBeNull();
    expect(container.querySelector('input[type="password"]')).toBeNull();
    const blob = (container.textContent || "").toLowerCase();
    expect(blob).not.toContain("add trading account");
    expect(blob).not.toContain("platform password");
  });

  it("ready state → primary CTA opens MetaTrader and offers choosing a strategy", () => {
    render(<HostedWorkspaceStatus journey={journey()} accounts={[demoAccount]} />);
    const open = screen.getByRole("link", { name: /open metatrader/i });
    expect(open).toHaveAttribute("href", "/trading/terminal-access");
    expect(screen.getByRole("link", { name: /choose a strategy/i })).toHaveAttribute("href", "/strategies/marketplace");
    // Shows the detected broker login (masked) and demo classification from the active account.
    expect(screen.getByText("***561")).toBeInTheDocument();
    expect(screen.getByText("Demo")).toBeInTheDocument();
  });

  it("in-progress state → primary CTA continues the hosted journey (never a dead end)", () => {
    render(
      <HostedWorkspaceStatus
        journey={journey({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", strategy_eligible: false, active_login_masked: "" })}
        accounts={[]}
      />,
    );
    const cta = screen.getByRole("link", { name: /continue setup/i });
    expect(cta).toHaveAttribute("href", "/onboarding/hosted");
    // No terminal-ready CTA yet.
    expect(screen.queryByRole("link", { name: /open metatrader/i })).toBeNull();
    expect(screen.getByText("Not yet")).toBeInTheDocument(); // broker not detected yet
    // This read-only page must NOT render the interactive journey's imperative instruction (there is no
    // broker-details field here) — it shows a status-oriented sentence that points at the CTA instead.
    expect(screen.queryByText(/enter your broker account number/i)).toBeNull();
    expect(screen.getByText(/continue setup to point it at your broker account/i)).toBeInTheDocument();
  });

  // ---- ADR-0047: explicit "Enable automated trading" authorization (capability != consent) ----------------
  it("shows the explicit Enable-automated-trading control ONLY when the server says can_enable", async () => {
    const onAuthorized = vi.fn();
    const { container } = render(
      <HostedWorkspaceStatus
        journey={journey({ execution_ready: true, can_enable_automated_trading: true })}
        accounts={[demoAccount]}
        onAuthorized={onAuthorized}
      />,
    );
    const btn = screen.getByRole("button", { name: /enable automated trading/i });
    // Truthful resting state — capability is NOT presented as consent. ("Automated trading" appears as both
    // the status-row label and the control heading, so assert at least one.)
    expect(screen.getAllByText("Automated trading").length).toBeGreaterThan(0);
    expect(screen.getByText("Ready — not yet enabled")).toBeInTheDocument();
    // Copy states the distinction and uses NO internal terminology.
    const blob = (container.textContent || "").toLowerCase();
    expect(blob).toContain("ready for automated trading");
    expect(blob).toContain("enable automated trading when you want");
    expect(blob).not.toContain("execution_ready");
    expect(blob).not.toContain("trade_allowed");
    expect(blob).not.toContain("autotrading");
    expect(blob).not.toContain("arming");
    // Click authorizes, then re-reads authoritative state (never trusts the click).
    fireEvent.click(btn);
    await waitFor(() => expect(authorizeExecution).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(onAuthorized).toHaveBeenCalledTimes(1));
  });

  it("armed → shows the enabled confirmation and NO enable button", () => {
    render(
      <HostedWorkspaceStatus
        journey={journey({ execution_ready: true, execution_authorized: true, execution_armed: true })}
        accounts={[demoAccount]}
      />,
    );
    expect(screen.getByText(/automated trading is enabled/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /enable automated trading/i })).toBeNull();
  });

  it("ready but NOT yet enable-able → no automated-trading control at all (capability is not consent)", () => {
    render(
      <HostedWorkspaceStatus journey={journey({ execution_ready: true })} accounts={[demoAccount]} />,
    );
    expect(screen.queryByRole("button", { name: /enable automated trading/i })).toBeNull();
    expect(screen.queryByText(/automated trading is enabled/i)).toBeNull();
  });
});

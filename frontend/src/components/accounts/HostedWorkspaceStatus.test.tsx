import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));

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
});

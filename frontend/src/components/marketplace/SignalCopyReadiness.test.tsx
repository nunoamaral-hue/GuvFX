import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

/** WS-D — the readiness panel that replaces the opaque "not armed" hint. It must render the backend's
 * ✓/✕ checklist + one customer-safe next action, gate the Enable-Trading button on the backend's can_arm
 * (never on frontend visibility alone), and degrade to a neutral "unavailable" — never a false "not
 * ready" — when the read-only status fetch fails. */
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));

import { SignalCopyReadiness } from "./SignalCopyReadiness";

const READY = {
  state: "READY", armed: false, enabled: false, can_arm: true, account_id: 5,
  next_action: "ready_enable",
  checklist: [
    { key: "demo", ok: true }, { key: "active", ok: true }, { key: "credentials", ok: true },
    { key: "runtime_ready", ok: true }, { key: "pilot_access", ok: true },
  ],
};

function props(over: Record<string, unknown> = {}) {
  return {
    lang: "en" as const,
    marketplaceStrategyId: "mp-010",
    accounts: [{ id: 5, name: "Demo", is_demo: true }],
    selectedAccountId: 5 as number | "" | undefined,
    onSelectAccount: vi.fn(),
    armUiEnabled: false,
    isAuthed: true,
    arming: false,
    onArm: vi.fn(),
    ...over,
  };
}

describe("SignalCopyReadiness (WS-D)", () => {
  beforeEach(() => { apiFetch.mockReset(); });

  it("shows an add-account prompt (and fetches nothing) when there are no demo accounts", () => {
    render(<SignalCopyReadiness {...props({
      accounts: [{ id: 9, name: "Live", is_demo: false }], selectedAccountId: undefined,
    })} />);
    expect(screen.getByText(/demo account first/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Go to Broker Accounts/i })).toHaveAttribute("href", "/accounts");
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("renders the checklist + next action; no Enable button when the arm UI is not built", async () => {
    apiFetch.mockResolvedValue(READY);
    render(<SignalCopyReadiness {...props({ armUiEnabled: false })} />);
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(screen.getByText("Trading access enabled")).toBeInTheDocument();
    expect(screen.getByText(/ready\. Enable trading/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Enable Trading/i })).toBeNull();
  });

  it("enables the Enable-Trading button only when can_arm, and calls onArm on click", async () => {
    apiFetch.mockResolvedValue(READY);
    const onArm = vi.fn();
    render(<SignalCopyReadiness {...props({ armUiEnabled: true, onArm })} />);
    const btn = await screen.findByRole("button", { name: /Enable Trading/i });
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onArm).toHaveBeenCalledWith(5);
  });

  it("disables Enable-Trading (and shows a contact-support next action) when can_arm is false", async () => {
    apiFetch.mockResolvedValue({
      ...READY, can_arm: false, next_action: "request_access",
      checklist: READY.checklist.map((c) => (c.key === "pilot_access" ? { ...c, ok: false } : c)),
    });
    const onArm = vi.fn();
    render(<SignalCopyReadiness {...props({ armUiEnabled: true, onArm })} />);
    const btn = await screen.findByRole("button", { name: /Enable Trading/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/contact support to request access/i)).toBeInTheDocument();
    fireEvent.click(btn);
    expect(onArm).not.toHaveBeenCalled();
  });

  it("shows a neutral 'unavailable' (not a false 'not ready') when the status fetch fails", async () => {
    apiFetch.mockRejectedValue(new Error("boom"));
    render(<SignalCopyReadiness {...props({ armUiEnabled: true })} />);
    await waitFor(() =>
      expect(screen.getByText(/couldn't check your account status/i)).toBeInTheDocument());
  });
});

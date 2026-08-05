import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

/** WS-D — the marketplace signal-copy card shows the readiness PANEL (checklist + one next action) for the
 * customer's demo account, backed by the read-only /signal-copy/readiness endpoint. The Enable-Trading
 * button appears only when the broker-connectivity journey is built AND the backend reports can_arm; on
 * click it arms via /signal-copy/arm, then reflects ARMED/RUNNING only from the authoritative status
 * refresh (never the optimistic arm reply), and renders customer-safe wording per the arm status slug. */

type Status = { armed: boolean; enabled: boolean; ambiguous?: boolean };
const state = vi.hoisted(() => ({
  status: { armed: false, enabled: false } as Status,
  flagOn: true,          // NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED — gates the Enable button (arm UI)
  canArm: true,          // backend readiness.can_arm for the selected account
  nextAction: "ready_enable",
}));

function readinessResponse() {
  const ok = state.canArm;
  return {
    state: ok ? "READY" : "PREPARING",
    armed: state.status.armed, enabled: state.status.enabled, can_arm: ok,
    next_action: state.nextAction,
    checklist: [
      { key: "demo", ok: true }, { key: "active", ok: true }, { key: "credentials", ok: true },
      { key: "runtime_ready", ok }, { key: "pilot_access", ok },
    ],
  };
}

const { arm, toggle, apiFetch } = vi.hoisted(() => {
  const arm = vi.fn();
  const toggle = vi.fn(() => ({ status: "enabled", enabled: true }));
  const apiFetch = vi.fn(async (path: string, opts?: RequestInit) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.startsWith("/api/trading/accounts")) {
      return [{ id: 5, name: "Demo A", is_demo: true, is_active: true }];
    }
    if (path.startsWith("/api/strategies/strategies/signal-copy/readiness")) return readinessResponse();
    if (path.startsWith("/api/strategies/strategies/signal-copy/arm")) return arm(opts);
    if (path.startsWith("/api/strategies/strategies/signal-copy/toggle")) return toggle(opts);
    if (path.startsWith("/api/strategies/strategies/signal-copy/status")) return state.status;
    return {};
  });
  return { arm, toggle, apiFetch };
});

const statusCalls = () =>
  apiFetch.mock.calls.filter((c) => String(c[0]).includes("/signal-copy/status")).length;

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/components/AppShell", () => ({ useLang: () => "en" }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/flags", () => ({ brokerConnectivityEnabled: () => state.flagOn }));

import Marketplace from "./page";

const enableBtn = () => screen.getByRole("button", { name: "Enable Trading" });

describe("marketplace signal-copy Enable Trading (arm)", () => {
  beforeEach(() => {
    arm.mockReset();
    toggle.mockClear();
    apiFetch.mockClear();
    state.status = { armed: false, enabled: false };
    state.flagOn = true;
    state.canArm = true;
    state.nextAction = "ready_enable";
    const store: Record<string, string> = {};
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    });
  });

  it("shows the readiness panel + Enable Trading when ready, and arms the auto-selected account", async () => {
    arm.mockImplementation(() => {
      state.status = { armed: true, enabled: true }; // backend now reports armed+enabled
      return { status: "armed", enabled: true };
    });
    render(<Marketplace />);

    // Readiness panel renders a checklist (not a dead disabled toggle) and, when ready, the Enable action.
    await waitFor(() => expect(screen.getByText("Trading access enabled")).toBeTruthy());
    const btn = await screen.findByRole("button", { name: "Enable Trading" });
    const statusCallsBeforeArm = statusCalls();
    fireEvent.click(btn);

    await waitFor(() =>
      expect(apiFetch).toHaveBeenCalledWith(
        "/api/strategies/strategies/signal-copy/arm/",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    const body = JSON.parse((arm.mock.calls[0][0] as RequestInit).body as string);
    expect(body).toEqual({ marketplace_strategy_id: "mp-010", account_id: 5 });
    // ARMED only after the authoritative status refresh — success wording shown.
    await waitFor(() => expect(screen.getByText("Trading enabled for this account.")).toBeTruthy());
    // Property (Gate 1): `toggle` is NEVER called as part of arming.
    expect(toggle).not.toHaveBeenCalled();
    // Property (Gate 1): state is REFRESHED — a status GET fires AFTER the arm POST.
    expect(statusCalls()).toBeGreaterThan(statusCallsBeforeArm);
  });

  it("shows RUNNING only from the backend-confirmed status — arm's own enabled:true is ignored", async () => {
    arm.mockImplementation(() => {
      state.status = { armed: true, enabled: false }; // backend: armed but NOT enabled
      return { status: "armed", enabled: true };       // arm optimistically claims enabled
    });
    render(<Marketplace />);
    const btn = await screen.findByRole("button", { name: "Enable Trading" });
    fireEvent.click(btn);

    // After the refresh the card is armed-but-not-enabled: the toggle reads "Enable", the badge "Disabled".
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable" })).toBeTruthy());
    expect(screen.getByText("Disabled")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Disable" })).toBeNull();
    expect(screen.queryByText("Enabled")).toBeNull();
    expect(toggle).not.toHaveBeenCalled();
  });

  it("renders customer-safe wording for the runtime_not_ready arm status (never the raw slug/detail)", async () => {
    arm.mockImplementation(() => {
      const err = new Error("Account runtime is not ready to trade yet.") as Error & {
        httpStatus?: number; body?: { status?: string };
      };
      err.httpStatus = 409;
      err.body = { status: "runtime_not_ready" };
      throw err;
    });
    render(<Marketplace />);
    const btn = await screen.findByRole("button", { name: "Enable Trading" });
    fireEvent.click(btn);

    await waitFor(() =>
      expect(screen.getByText("Your account is still getting ready to trade. Try again shortly.")).toBeTruthy());
    expect(screen.queryByText(/runtime_not_ready/)).toBeNull();
  });

  it("DARK: no Enable button when the broker-connectivity build flag is OFF (panel still guides)", async () => {
    // Load-bearing DARK proof: with NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED OFF, no Enable-Trading (arm)
    // control appears — even if the backend would allow it — so a DARK build never surfaces a live arm
    // path. The readiness panel still renders (customer-safe guidance), and the old operator-jargon hint
    // ("Arming (auto-demo) is a separate, gated step.") is gone.
    state.flagOn = false;
    arm.mockImplementation(() => ({ status: "armed", enabled: true }));
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByText("Trading access enabled")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "Enable Trading" })).toBeNull();
    expect(screen.queryByText("Arming (auto-demo) is a separate, gated step.")).toBeNull();
    expect(arm).not.toHaveBeenCalled();
  });

  it("gates the arm button on backend can_arm (disabled + next-action shown when not ready)", async () => {
    state.canArm = false;         // backend readiness says the account can't arm yet
    state.nextAction = "preparing";
    render(<Marketplace />);
    const btn = await screen.findByRole("button", { name: "Enable Trading" });
    expect((btn as HTMLButtonElement).disabled).toBe(true);
    expect(
      screen.getByText("Your account is still getting ready to trade. Try again shortly."),
    ).toBeTruthy();
    fireEvent.click(btn);
    expect(arm).not.toHaveBeenCalled();
  });
});

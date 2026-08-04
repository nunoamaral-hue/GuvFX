import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

/** IPR Area D — the marketplace signal-copy card must let a customer choose a demo account and
 * Enable Trading (arm), then reflect ARMED only from the backend-confirmed status, and render
 * customer-safe wording per the arm response's status slug. */

type Status = { armed: boolean; enabled: boolean; ambiguous?: boolean };
const state = vi.hoisted(() => ({ status: { armed: false, enabled: false } as Status, statusCalls: 0 }));
const { arm, toggle, apiFetch } = vi.hoisted(() => {
  const arm = vi.fn();
  const toggle = vi.fn(() => ({ status: "enabled", enabled: true }));
  const apiFetch = vi.fn(async (path: string, opts?: RequestInit) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.startsWith("/api/trading/accounts")) {
      return [{ id: 5, name: "Demo A", is_demo: true, is_active: true }];
    }
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
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));

import Marketplace from "./page";

function armCard() {
  const btn = screen.getByRole("button", { name: "Enable Trading" });
  const col = btn.closest("div")!.parentElement!; // buttons grid → card flex column
  return { btn, select: within(col).getByRole("combobox") as HTMLSelectElement };
}

describe("marketplace signal-copy Enable Trading (arm)", () => {
  beforeEach(() => {
    arm.mockReset();
    toggle.mockClear();
    apiFetch.mockClear();
    state.status = { armed: false, enabled: false };
    const store: Record<string, string> = {};
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    });
  });

  it("shows the account selector + Enable Trading when not armed, and arms the chosen account", async () => {
    arm.mockImplementation(() => {
      state.status = { armed: true, enabled: true }; // backend now reports armed+enabled
      return { status: "armed", enabled: true };
    });
    render(<Marketplace />);

    // Not-armed card exposes the Enable Trading action (not a dead disabled toggle).
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable Trading" })).toBeTruthy());
    await waitFor(() => expect(screen.getAllByRole("option", { name: /Demo A/ }).length).toBeGreaterThan(0));

    const { btn, select } = armCard();
    fireEvent.change(select, { target: { value: "5" } });
    expect(select.value).toBe("5");
    const statusCallsBeforeArm = statusCalls(); // count the on-mount prefetch(es) before we arm
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
    // The arm response CLAIMS enabled:true, but the authoritative refresh reports enabled:false. The
    // card must reflect the backend refresh (Disabled / Enable), never the optimistic arm response —
    // proving RUNNING/ON is never displayed from a click or the arm reply alone.
    arm.mockImplementation(() => {
      state.status = { armed: true, enabled: false }; // backend: armed but NOT enabled
      return { status: "armed", enabled: true }; // arm optimistically claims enabled
    });
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable Trading" })).toBeTruthy());
    await waitFor(() => expect(screen.getAllByRole("option", { name: /Demo A/ }).length).toBeGreaterThan(0));

    const { btn, select } = armCard();
    fireEvent.change(select, { target: { value: "5" } });
    fireEvent.click(btn);

    // After the refresh the card is armed-but-not-enabled: the toggle reads "Enable" and the badge
    // reads "Disabled". The ON labels ("Disable" toggle / "Enabled" badge) must NOT appear.
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable" })).toBeTruthy());
    expect(screen.getByText("Disabled")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Disable" })).toBeNull();
    expect(screen.queryByText("Enabled")).toBeNull();
    // And arming still never invoked toggle.
    expect(toggle).not.toHaveBeenCalled();
  });

  it("renders customer-safe wording for the runtime_not_ready status (never the raw slug/detail)", async () => {
    arm.mockImplementation(() => {
      const err = new Error("Account runtime is not ready to trade yet.") as Error & {
        httpStatus?: number;
        body?: { status?: string };
      };
      err.httpStatus = 409;
      err.body = { status: "runtime_not_ready" };
      throw err;
    });
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable Trading" })).toBeTruthy());
    await waitFor(() => expect(screen.getAllByRole("option", { name: /Demo A/ }).length).toBeGreaterThan(0));

    const { btn, select } = armCard();
    fireEvent.change(select, { target: { value: "5" } });
    fireEvent.click(btn);

    await waitFor(() =>
      expect(
        screen.getByText("Your trading terminal is still starting up. Try again once it's ready."),
      ).toBeTruthy(),
    );
    // The raw slug is never surfaced.
    expect(screen.queryByText(/runtime_not_ready/)).toBeNull();
  });
});

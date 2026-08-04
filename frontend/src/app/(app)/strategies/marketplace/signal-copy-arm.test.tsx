import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

/** IPR Area D — the marketplace signal-copy card must let a customer choose a demo account and
 * Enable Trading (arm), then reflect ARMED only from the backend-confirmed status, and render
 * customer-safe wording per the arm response's status slug. */

type Status = { armed: boolean; enabled: boolean; ambiguous?: boolean };
const state = vi.hoisted(() => ({ status: { armed: false, enabled: false } as Status }));
const { arm, apiFetch } = vi.hoisted(() => {
  const arm = vi.fn();
  const apiFetch = vi.fn(async (path: string, opts?: RequestInit) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.startsWith("/api/trading/accounts")) {
      return [{ id: 5, name: "Demo A", is_demo: true, is_active: true }];
    }
    if (path.startsWith("/api/strategies/strategies/signal-copy/status")) return state.status;
    if (path.startsWith("/api/strategies/strategies/signal-copy/arm")) return arm(opts);
    return {};
  });
  return { arm, apiFetch };
});

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

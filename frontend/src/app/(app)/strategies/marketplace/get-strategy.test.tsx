import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

/** AJ#7 marketplace acquisition — "Get Strategy" acquires WITHOUT enabling execution and hands off to
 * Configure. Proves: signal-copy Get calls /signal-copy/get (never arm/authorize) then routes to Configure;
 * generic Get uses the marketplace assign then routes to Configure; every card shows a Free price; an
 * already-owned signal-copy card offers Configure/Manage (no re-acquire). */

const { state, apiFetch, push } = vi.hoisted(() => {
  const state = {
    status: { armed: false, enabled: false } as { armed: boolean; enabled: boolean; ambiguous?: boolean },
    accounts: [{ id: 5, name: "Demo A", is_demo: true, is_active: true }],
  };
  const push = vi.fn();
  const apiFetch = vi.fn(async (path: string) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.startsWith("/api/trading/accounts")) return state.accounts;
    if (path.includes("/signal-copy/status")) return { ...state.status };
    if (path.includes("/signal-copy/get")) return { status: "owned", assignment_id: 9, enabled: false };
    if (path.includes("/marketplace/assign")) return { status: "assigned" };
    return {};
  });
  return { state, apiFetch, push };
});

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/lib/hosted-journey", () => ({ fetchJourney: vi.fn(async () => ({ ok: false, unavailable: true })), authorizeExecution: vi.fn() }));
vi.mock("@/components/AppShell", () => ({ useLang: () => "en" }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push, replace: vi.fn() }) }));

import Marketplace from "./page";

const armOrAuth = () =>
  apiFetch.mock.calls.some((c) => String(c[0]).includes("/signal-copy/arm") || String(c[0]).includes("authorize-execution"));

describe("marketplace — Get Strategy acquisition", () => {
  beforeEach(() => {
    apiFetch.mockClear(); push.mockClear();
    state.status = { armed: false, enabled: false };
    // Owned account #5 preselected via the per-browser default so the Get button is enabled.
    const store: Record<string, string> = { guvfx_marketplace_default_account_id: "5" };
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    });
  });

  it("shows a Free price on the cards", async () => {
    render(<Marketplace />);
    await waitFor(() => expect(screen.getAllByText("Free").length).toBeGreaterThan(0));
  });

  it("Get on the Wayond card acquires via /signal-copy/get (never arms/authorizes) then routes to Configure", async () => {
    render(<Marketplace />);
    // Narrow to Wayond so there's a single Get button.
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "Wayond" } });
    const getBtn = await screen.findByRole("button", { name: "Get Strategy" });
    await waitFor(() => expect((getBtn as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(getBtn);
    await waitFor(() =>
      expect(apiFetch.mock.calls.some((c) => String(c[0]).includes("/signal-copy/get"))).toBe(true));
    expect(push).toHaveBeenCalledWith("/strategies/configure?mp=mp-010&account=5");
    // The one thing Get must NEVER do: arm or authorize execution.
    expect(armOrAuth()).toBe(false);
  });

  it("generic/prototype template cards are WITHHELD from the beta catalogue (no card, no acquisition path)", async () => {
    // Beta curation: only strategies with a proven customer path are shown. A research template like
    // "London Session Box Breakout" (mp-001) must not appear and must not be acquirable via marketplace/assign.
    render(<Marketplace />);
    await waitFor(() => expect(screen.getByText("Wayond WIM Strategy")).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "London Session" } });
    await waitFor(() => expect(screen.queryByText(/London Session/i)).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Get Strategy" })).toBeNull();
    expect(apiFetch.mock.calls.some((c) => String(c[0]).includes("/marketplace/assign"))).toBe(false);
  });

  it("an already-owned signal-copy card offers Configure (routes to Configure, never re-acquires)", async () => {
    state.status = { armed: true, enabled: false };
    render(<Marketplace />);
    fireEvent.change(screen.getByPlaceholderText(/search/i), { target: { value: "Wayond" } });
    const cfg = await screen.findByRole("button", { name: "Configure" });
    fireEvent.click(cfg);
    expect(push).toHaveBeenCalledWith("/strategies/configure?mp=mp-010");
    // Owned card never re-acquires or arms.
    expect(apiFetch.mock.calls.some((c) => String(c[0]).includes("/signal-copy/get"))).toBe(false);
    expect(armOrAuth()).toBe(false);
  });
});

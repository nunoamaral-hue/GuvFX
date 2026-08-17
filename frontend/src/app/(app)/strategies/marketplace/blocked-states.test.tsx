import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

/** WS-G (Nuno-directed marketplace redesign) — every blocked state must explain what's missing and the
 * next action, and the dead affordances (Preview no-op, "Preview metrics unavailable" strip, empty
 * "Structure" filter) must be gone. Rendered in the DARK build with NO trading accounts. */
const { apiFetch, state } = vi.hoisted(() => {
  const state = { accountsPending: false, unauth: false };
  const apiFetch = vi.fn(async (path: string) => {
    if (path.startsWith("/api/auth/me")) {
      if (state.unauth) throw Object.assign(new Error("unauthorized"), { status: 401 });
      return {};
    }
    if (path.startsWith("/api/trading/accounts")) {
      return state.accountsPending ? new Promise(() => {}) : [];       // pending vs. loaded-empty
    }
    if (path.startsWith("/api/strategies/strategies/signal-copy/status")) return { armed: false, enabled: false };
    if (path.startsWith("/api/strategies/strategies/signal-copy/readiness")) {
      return { state: "SETUP_INCOMPLETE", armed: false, enabled: false, can_arm: false, next_action: "add_demo_account", checklist: [] };
    }
    return {};
  });
  return { apiFetch, state };
});
vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/components/AppShell", () => ({ useLang: () => "en" }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/flags", () => ({ brokerConnectivityEnabled: () => false }));

import Marketplace from "./page";

describe("marketplace blocked states + dead-affordance cleanup (WS-G)", () => {
  beforeEach(() => {
    apiFetch.mockClear();
    state.accountsPending = false;
    state.unauth = false;
    const store: Record<string, string> = {};
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    });
  });

  it("a generic card with no eligible account explains what's missing + links to Broker Accounts", async () => {
    render(<Marketplace />);
    await waitFor(() =>
      expect(screen.getAllByText(/You'll need a broker account first/i).length).toBeGreaterThan(0));
    const link = screen.getAllByRole("link", { name: /Go to Broker Accounts/i })[0];
    expect(link).toHaveAttribute("href", "/accounts");
  });

  it("does NOT flash the blocked message while accounts are still loading", async () => {
    // Guards the loading-guard (`!loadingAccounts && …`): with the accounts fetch pending, the card must
    // show the (disabled) selector, NOT a false 'no account' message.
    state.accountsPending = true;
    render(<Marketplace />);
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());   // effects ran; accounts fetch is pending
    expect(screen.queryByText(/You'll need a broker account first/i)).toBeNull();
  });

  it("a logged-out card carries its OWN sign-in next action (P0.2 — not just the page-top banner)", async () => {
    state.unauth = true;
    render(<Marketplace />);
    // AJ#7 — acquisition vocabulary is "get this strategy" (was "assign"); the card still carries its own action.
    await waitFor(() => expect(screen.getAllByText(/sign in to get this strategy/i).length).toBeGreaterThan(0));
    const signIn = screen.getAllByRole("link").find((l) => l.getAttribute("href") === "/login?reason=unauthenticated");
    expect(signIn).toBeTruthy();
  });

  it("has no dead Preview affordance and no empty 'Structure' filter", async () => {
    render(<Marketplace />);
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(screen.queryByText(/Preview metrics unavailable/i)).toBeNull();
    expect(screen.queryByRole("button", { name: "Preview" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Structure" })).toBeNull();
  });
});

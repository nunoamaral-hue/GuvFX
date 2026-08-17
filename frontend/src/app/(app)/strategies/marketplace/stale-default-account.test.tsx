import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

/** Beta account product parity — regression guard for the cross-user stale-localStorage leak.
 *
 * `guvfx_marketplace_default_account_id` is per-browser, not per-user. On a shared machine it can hold an
 * account id (e.g. #1) that belongs to a PREVIOUS user. Seeding that id into the marketplace would aim the
 * "Get Strategy" acquisition at a foreign account. The saved default must be applied only when the CURRENT
 * user actually owns it; otherwise no account is preselected and each card prompts for a real owned account. */
const { apiFetch } = vi.hoisted(() => {
  const apiFetch = vi.fn(async (path: string) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.startsWith("/api/trading/accounts")) {
      // The current user owns ONLY account #12 (a demo account). Account #1 is a foreign account.
      return [{ id: 12, name: "IS6 Technologies LTD", is_demo: true, is_active: false }];
    }
    if (path.startsWith("/api/strategies/strategies/signal-copy/status")) {
      return { armed: false, enabled: false };
    }
    return {};
  });
  return { apiFetch };
});
vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/components/AppShell", () => ({ useLang: () => "en" }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn(), replace: vi.fn() }) }));
vi.mock("@/lib/flags", () => ({ brokerConnectivityEnabled: () => true }));

import Marketplace from "./page";

describe("marketplace stale-default-account reconciliation", () => {
  beforeEach(() => {
    apiFetch.mockClear();
    // A default persisted by a PREVIOUS user/session — account #1, which the current user does NOT own.
    const store: Record<string, string> = { guvfx_marketplace_default_account_id: "1" };
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    });
  });

  it("never preselects the stale/foreign account; the owned account is the only selectable one", async () => {
    render(<Marketplace />);
    // The owned demo account (#12) is the option offered on the acquisition selector.
    await waitFor(() =>
      expect(screen.getAllByRole("option", { name: "IS6 Technologies LTD" }).length).toBeGreaterThan(0));
    // The stale localStorage default (#1) is NOT owned → it must never be applied to any card's selector.
    for (const sel of screen.getAllByRole("combobox")) {
      expect((sel as HTMLSelectElement).value).not.toBe("1");
    }
  });
});

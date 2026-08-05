import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, waitFor } from "@testing-library/react";

/** Beta account product parity — regression guard for the cross-user stale-localStorage leak.
 *
 * `guvfx_marketplace_default_account_id` is per-browser, not per-user. On a shared machine it can hold an
 * account id (e.g. #1) that belongs to a PREVIOUS user. Seeding that id into the marketplace made the
 * ownership-scoped signal-copy readiness endpoint 404 ("We couldn't check your account status right now")
 * and would have aimed Assign at a foreign account. The saved default must be applied only when the
 * CURRENT user actually owns it; otherwise each card picks one of the user's own accounts. */
const { apiFetch, readinessAccountIds } = vi.hoisted(() => {
  const readinessAccountIds: string[] = [];
  const apiFetch = vi.fn(async (path: string) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.startsWith("/api/trading/accounts")) {
      // The current user owns ONLY account #12 (a demo account). Account #1 is a foreign account.
      return [{ id: 12, name: "IS6 Technologies LTD", is_demo: true, is_active: false }];
    }
    if (path.startsWith("/api/strategies/strategies/signal-copy/status")) {
      return { armed: false, enabled: false };
    }
    if (path.startsWith("/api/strategies/strategies/signal-copy/readiness")) {
      const m = path.match(/account_id=(\d+)/);
      const accountId = m ? m[1] : "";
      readinessAccountIds.push(accountId);
      if (accountId !== "12") {
        // Mirror the real ownership-scoped endpoint: a non-owned account id is 404.
        throw new Error("404");
      }
      return {
        state: "SETUP_INCOMPLETE", armed: false, enabled: false, can_arm: false,
        next_action: "activate_account", account_id: 12,
        checklist: [
          { key: "demo", ok: true }, { key: "active", ok: false }, { key: "credentials", ok: true },
          { key: "runtime_ready", ok: false }, { key: "pilot_access", ok: false },
        ],
      };
    }
    return {};
  });
  return { apiFetch, readinessAccountIds };
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
    readinessAccountIds.length = 0;
    // A default persisted by a PREVIOUS user/session — account #1, which the current user does NOT own.
    const store: Record<string, string> = { guvfx_marketplace_default_account_id: "1" };
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    });
  });

  it("never queries readiness for the stale/foreign account; uses the user's owned account", async () => {
    render(<Marketplace />);
    // The readiness panel must resolve to the owned demo account (#12) and succeed.
    await waitFor(() => expect(readinessAccountIds).toContain("12"));
    // The stale localStorage default (#1) must never have been used for a readiness query.
    expect(readinessAccountIds).not.toContain("1");
  });
});

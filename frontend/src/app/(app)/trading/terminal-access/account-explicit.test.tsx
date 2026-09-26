import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { LanguageProvider } from "@/components/AppShell";

/**
 * P0 regression (cross-account interactive-delivery isolation): opening the Terminal Access page with an explicit
 * ``?account_id=<id>`` MUST bind the embedded RemoteApp to EXACTLY that owned account and mint its delivery —
 * it must NOT auto-detect (which picks the active account, e.g. account 25, and showed the WRONG terminal for
 * every account_id). This proves the URL → useSearchParams → HostedMt5RemoteApp(accountId) → delivery-connect
 * chain is account-explicit end to end.
 */

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch }));
// The whole point: the URL carries account_id=35.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/trading/terminal-access",
  useSearchParams: () => new URLSearchParams("account_id=35"),
}));
vi.mock("next/link", () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));

const calls: string[] = [];
const connectBodies: string[] = [];
function routeApi(url: string, opts?: { body?: string }) {
  calls.push(url);
  if (url.startsWith("/api/hosted-workspace/delivery-state/")) return Promise.resolve({ is_owner: true });
  if (url.startsWith("/api/hosted-workspace/delivery-connect/")) {
    if (opts?.body) connectBodies.push(opts.body);
    return Promise.resolve({ transport_type: "rdp_remoteapp", embed_url: "https://guvfx.com/guacamole/#/client/x?data=y", session_token: "", expiry: null });
  }
  // Auto-detect would hit this; the explicit path must NOT.
  if (url.startsWith("/api/trading/accounts/")) return Promise.resolve([{ id: 25, name: "IS6", account_number: "1302587", is_active: true }]);
  if (url.startsWith("/api/mt5-interaction/sessions/active")) return Promise.resolve(null);
  if (url.startsWith("/api/mt5-interaction/terminal-bindings")) return Promise.resolve([]);
  return Promise.resolve(null);
}

import TerminalAccessPage from "./page";

describe("Terminal Access — account-explicit RemoteApp binding (P0)", () => {
  beforeEach(() => {
    apiFetch.mockReset(); calls.length = 0; connectBodies.length = 0;
    apiFetch.mockImplementation((url: string, opts?: { body?: string }) => routeApi(url, opts));
  });

  it("binds to account_id=35 explicitly (probes delivery-state?account_id=35, never auto-detects the account list)", async () => {
    vi.spyOn(HTMLIFrameElement.prototype, "focus").mockImplementation(() => {});
    render(<LanguageProvider lang="en"><TerminalAccessPage /></LanguageProvider>);
    // Explicit owner probe for THIS account resolves → the Open button appears.
    const openBtn = await screen.findByRole("button", { name: /open mt5 terminal/i });
    // The explicit binding probes delivery-state for account 35 and does NOT enumerate the account list.
    expect(calls.some((u) => u.includes("/api/hosted-workspace/delivery-state/?account_id=35"))).toBe(true);
    expect(calls.some((u) => u.startsWith("/api/trading/accounts/"))).toBe(false); // NOT auto-detect
    // Clicking Open mints delivery for account 35 (never the active account 25).
    fireEvent.click(openBtn);
    await waitFor(() => expect(connectBodies.length).toBeGreaterThan(0));
    expect(connectBodies.some((b) => b.includes("\"account_id\":35"))).toBe(true);
    expect(connectBodies.some((b) => b.includes("\"account_id\":25"))).toBe(false);
  });
});

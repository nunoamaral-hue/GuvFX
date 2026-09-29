import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { LanguageProvider } from "@/components/AppShell";

/**
 * MT5 first-launch broker-discovery UX safeguard (MT5_BROKER_DISCOVERY_MEMBER_UX_SAFE).
 *
 * The card shows a prominent, accessible warning ABOVE the MT5 window during the pre-connection window, telling
 * the member NOT to click Next until their broker appears. It is honest GUIDANCE (broker discovery is not
 * observable), never a fake progress/found claim. Its show/suppress is driven by a DURABLE, server-derived
 * signal (`broker_ever_matched` — the set-once first-correct-connection latch from the owner-scoped delivery-state
 * projection), never browser-local state, so an established account that later disconnects/reconnects never
 * re-shows the first-launch wizard. It names the member's OWN expected broker/server dynamically (no broker is
 * hardcoded). It must NOT enumerate `/api/trading/accounts/` in the explicit-account path (the P0 isolation guard).
 */

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch }));

import { HostedMt5RemoteApp } from "@/components/hosted/HostedMt5RemoteApp";

const EXACT_PRIMARY = "Do not click Next until the broker you are searching for appears in the list.";

const calls: string[] = [];
type DeliveryState = {
  broker_display_name?: string;
  server_name?: string;
  broker_ever_matched?: boolean;
};
function routeApi(state: DeliveryState) {
  return (url: string) => {
    calls.push(url);
    if (url.startsWith("/api/hosted-workspace/delivery-state/")) {
      return Promise.resolve({ is_owner: true, delivery_state: "AUTHORIZED", ...state });
    }
    if (url.startsWith("/api/hosted-workspace/delivery-connect/")) {
      return Promise.resolve({ transport_type: "rdp_remoteapp", embed_url: "", session_token: "", expiry: null });
    }
    // Auto-detect (the account LIST) must NEVER be reached in the explicit-account path.
    if (url.startsWith("/api/trading/accounts/")) return Promise.resolve([{ id: 25, is_active: true }]);
    return Promise.resolve(null);
  };
}

function renderCard(state: DeliveryState, lang: "en" | "ja" = "en") {
  apiFetch.mockImplementation(routeApi(state));
  return render(
    <LanguageProvider lang={lang}>
      <HostedMt5RemoteApp accountId={35} />
    </LanguageProvider>,
  );
}

beforeEach(() => {
  apiFetch.mockReset();
  calls.length = 0;
  vi.spyOn(HTMLIFrameElement.prototype, "focus").mockImplementation(() => {});
});
afterEach(() => cleanup());

describe("HostedMt5RemoteApp — first-launch broker-discovery safeguard", () => {
  it("shows the warning with the EXACT approved primary copy + dynamic broker/server when not connected", async () => {
    renderCard({ broker_display_name: "Pepperstone", server_name: "Pepperstone-Demo", broker_ever_matched: false });
    const alert = await screen.findByRole("alert");
    // Preserve the approved wording exactly.
    expect(within(alert).getByText(EXACT_PRIMARY)).toBeInTheDocument();
    // Dynamic expected broker/server come from the server projection (never hardcoded).
    expect(within(alert).getByTestId("discovery-expected-broker")).toHaveTextContent("Pepperstone");
    expect(within(alert).getByTestId("discovery-expected-server")).toHaveTextContent("Pepperstone-Demo");
  });

  it("renders a DIFFERENT broker dynamically — proving no broker is hardcoded", async () => {
    renderCard({ broker_display_name: "IS6FX", server_name: "IS6-Live", broker_ever_matched: false });
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByTestId("discovery-expected-broker")).toHaveTextContent("IS6FX");
    expect(within(alert).getByTestId("discovery-expected-server")).toHaveTextContent("IS6-Live");
    // The approved copy is generic — it never names a specific broker like FortressFX.
    expect(alert.textContent || "").not.toMatch(/FortressFX/i);
  });

  it("suppresses the warning once the account has ever correctly connected (durable first-launch latch)", async () => {
    renderCard({ broker_display_name: "Pepperstone", server_name: "Pepperstone-Demo", broker_ever_matched: true });
    // The Open button proves the card resolved to an owned account (so absence of the alert is meaningful).
    await screen.findByRole("button", { name: /open mt5 terminal/i });
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(EXACT_PRIMARY)).toBeNull();
  });

  it("keeps the warning when the broker/server projection is empty (honest fail-open guidance)", async () => {
    renderCard({ broker_display_name: "", server_name: "", broker_ever_matched: false });
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(EXACT_PRIMARY)).toBeInTheDocument();
    // With no expected broker/server known, the labelled fields are simply omitted (no empty labels).
    expect(within(alert).queryByTestId("discovery-expected-broker")).toBeNull();
    expect(within(alert).queryByTestId("discovery-expected-server")).toBeNull();
  });

  it("is an accessible alert and explains the MetaQuotes-first behaviour", async () => {
    renderCard({ broker_display_name: "Pepperstone", server_name: "Pepperstone-Demo", broker_ever_matched: false });
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/MetaQuotes/)).toBeInTheDocument();
    expect(within(alert).getByText(/Do not select MetaQuotes/i)).toBeInTheDocument();
  });

  it("never enumerates /api/trading/accounts/ in the explicit-account path (P0 isolation)", async () => {
    renderCard({ broker_display_name: "Pepperstone", server_name: "Pepperstone-Demo", broker_ever_matched: false });
    await screen.findByRole("alert");
    await waitFor(() =>
      expect(calls.some((u) => u.includes("/api/hosted-workspace/delivery-state/?account_id=35"))).toBe(true));
    expect(calls.some((u) => u.startsWith("/api/trading/accounts/"))).toBe(false);
  });

  it("renders the guidance in Japanese", async () => {
    renderCard({ broker_display_name: "Pepperstone", server_name: "Pepperstone-Demo", broker_ever_matched: false }, "ja");
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText(/検索しているブローカーが一覧に表示されるまで/)).toBeInTheDocument();
  });

  it("self-suppresses when the poll later reports the first correct connection", async () => {
    vi.useFakeTimers();
    try {
      let matched = false; // backend flips this to true once the member correctly connects.
      apiFetch.mockImplementation((url: string) => {
        calls.push(url);
        if (url.startsWith("/api/hosted-workspace/delivery-state/")) {
          return Promise.resolve({ is_owner: true, delivery_state: "AUTHORIZED",
            broker_display_name: "Pepperstone", server_name: "Pepperstone-Demo", broker_ever_matched: matched });
        }
        return Promise.resolve(null);
      });
      render(<LanguageProvider lang="en"><HostedMt5RemoteApp accountId={35} /></LanguageProvider>);
      // Flush detection (owner probe → account) + the initial poll load: the first-launch warning is up. Several
      // rounds cover the microtask+commit hops (detection resolves account, then the acctInfo effect polls).
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      await act(async () => { await vi.advanceTimersByTimeAsync(100); });
      expect(screen.getByRole("alert")).toBeInTheDocument();
      // The account now connects correctly; the next 20s poll observes the latch and removes the warning for good.
      matched = true;
      await act(async () => { await vi.advanceTimersByTimeAsync(20000); });
      expect(screen.queryByRole("alert")).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});

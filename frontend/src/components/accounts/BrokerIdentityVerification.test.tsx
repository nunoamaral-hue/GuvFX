/** BrokerIdentityVerification (WP7) — minimum verify/bind UI: self-hides on the DARK 404, lists the member's own
 * VERIFIED identities (masked), and surfaces a mapped reason on a refused verification. Exposes no financial action. */
import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { BrokerIdentityVerification } from "@/components/accounts/BrokerIdentityVerification";

const IDENTITIES = "/api/broker-intelligence/identities/";
const ACCOUNTS = "/api/trading/accounts/";
const VERIFY = "/api/broker-intelligence/identities/verify/";

function armed(identities: unknown[] = [], mailboxes: unknown[] = [{ id: 1, email_masked: "nr…@googlemail.com", status: "CONNECTED" }]) {
  api.apiFetch.mockImplementation((url: string) => {
    if (url === IDENTITIES) return Promise.resolve({ identities, mailboxes });
    if (url === ACCOUNTS) return Promise.resolve([{ id: 46, broker_name: "TradersWay", account_number: "51760001", masked_account_number: "••••0001", is_demo: false }]);
    return Promise.resolve({});
  });
}

beforeEach(() => { api.apiFetch.mockReset(); });
afterEach(() => { cleanup(); });

describe("BrokerIdentityVerification", () => {
  it("self-hides on the DARK 404 (renders nothing)", async () => {
    const err = Object.assign(new Error("Not found."), { status: 404 });
    api.apiFetch.mockRejectedValue(err);
    const { container } = render(<BrokerIdentityVerification />);
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(container.querySelector('[data-testid="broker-identity-panel"]')).toBeNull();
  });

  it("lists the member's own verified identities (masked)", async () => {
    armed([{ id: 7, broker: "TradersWay", email_masked: "nr…@googlemail.com", status: "VERIFIED", verified: true,
             trading_account_id: 46, connected_mailbox_id: 1, connected_mailbox_masked: "nr…@googlemail.com" }]);
    render(<BrokerIdentityVerification />);
    const panel = await screen.findByTestId("broker-identity-panel");
    expect(panel.textContent).toMatch(/nr…@googlemail\.com · TradersWay/);
    expect(panel.textContent).not.toMatch(/nrfda1111/);   // never the raw local-part
  });

  it("maps a refused verification to a human reason", async () => {
    armed([]);
    const verifyErr = Object.assign(new Error("broker_mismatch"), {
      status: 422, body: { ok: false, error: "broker_mismatch", message: "…" },
    });
    api.apiFetch.mockImplementation((url: string) => {
      if (url === IDENTITIES) return Promise.resolve({ identities: [], mailboxes: [] });
      if (url === ACCOUNTS) return Promise.resolve([{ id: 46, broker_name: "TradersWay", account_number: "51760001", is_demo: false }]);
      if (url === VERIFY) return Promise.reject(verifyErr);
      return Promise.resolve({});
    });
    render(<BrokerIdentityVerification />);
    await screen.findByTestId("broker-identity-panel");
    fireEvent.change(screen.getByTestId("identity-account-select"), { target: { value: "46" } });
    fireEvent.change(screen.getByTestId("identity-email-input"), { target: { value: "nrfda1111@googlemail.com" } });
    fireEvent.click(screen.getByTestId("identity-verify-btn"));
    await waitFor(() => expect(screen.getByTestId("identity-result").textContent).toMatch(/different broker/i));
  });

  it("shows success on a verified bind", async () => {
    api.apiFetch.mockImplementation((url: string) => {
      if (url === IDENTITIES) return Promise.resolve({ identities: [], mailboxes: [] });
      if (url === ACCOUNTS) return Promise.resolve([{ id: 46, broker_name: "TradersWay", account_number: "51760001", is_demo: false }]);
      if (url === VERIFY) return Promise.resolve({ ok: true, status: "VERIFIED", identity_id: 9, identity: {} });
      return Promise.resolve({});
    });
    render(<BrokerIdentityVerification />);
    await screen.findByTestId("broker-identity-panel");
    fireEvent.change(screen.getByTestId("identity-account-select"), { target: { value: "46" } });
    fireEvent.change(screen.getByTestId("identity-email-input"), { target: { value: "nrfda1111@googlemail.com" } });
    fireEvent.click(screen.getByTestId("identity-verify-btn"));
    await waitFor(() => expect(screen.getByTestId("identity-result").textContent).toMatch(/Verified and bound/i));
  });
});

/** ConnectedEmailAccounts — customer Gmail connection/status/reconnect/disconnect: self-hides on the DARK 404, shows
 * the owner's masked mailbox + conceptual state, starts the Google connect (redirect, never handling a code/token),
 * and disconnects with an explicit confirm. Exposes no credential and no financial action. */
import React from "react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, cleanup, fireEvent } from "@testing-library/react";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { ConnectedEmailAccounts } from "@/components/accounts/ConnectedEmailAccounts";

const STATUS = "/api/broker-intelligence/mailboxes/status/";
const CONNECT = "/api/broker-intelligence/mailboxes/connect/";

function statusOf(mailboxes: unknown[]) {
  api.apiFetch.mockImplementation((url: string) => {
    if (url === STATUS) return Promise.resolve({ connected: mailboxes.length > 0, mailboxes });
    return Promise.resolve({});
  });
}

beforeEach(() => {
  api.apiFetch.mockReset();
  // Clean URL by default so a prior test's ?mailbox_connect marker never leaks into another test.
  Object.defineProperty(window, "location", {
    value: { search: "", pathname: "/profile", assign: vi.fn(), href: "" }, writable: true, configurable: true,
  });
});
afterEach(() => { cleanup(); });

function atReturn(search: string) {
  Object.defineProperty(window, "location", {
    value: { search, pathname: "/profile", assign: vi.fn(), href: "" }, writable: true, configurable: true,
  });
}

describe("ConnectedEmailAccounts", () => {
  it("self-hides on the DARK 404 (renders nothing)", async () => {
    api.apiFetch.mockRejectedValue(Object.assign(new Error("Not found."), { status: 404 }));
    const { container } = render(<ConnectedEmailAccounts />);
    await waitFor(() => expect(api.apiFetch).toHaveBeenCalled());
    expect(container.querySelector('[data-testid="connected-email-panel"]')).toBeNull();
  });

  it("shows a healthy connected mailbox (masked) with a verified-binding marker", async () => {
    statusOf([{ mailbox_id: 1, provider: "GMAIL", masked_email: "nr…@gmail.com", state: "HEALTHY",
      connection_status: "CONNECTED", last_successful_sync: "2026-10-18T10:00:00Z",
      reconnect_required: false, has_verified_broker_bindings: true }]);
    render(<ConnectedEmailAccounts />);
    const panel = await screen.findByTestId("connected-email-panel");
    expect(screen.getByTestId("connected-email-state").textContent).toMatch(/monitoring/i);
    expect(panel.textContent).toMatch(/nr…@gmail\.com/);
    expect(panel.textContent).not.toMatch(/nrfda1111/);     // never a raw local-part
    expect(panel.textContent).toMatch(/verified/i);
    // privacy disclosure present, and no claim that GuvFX can move funds
    expect(panel.textContent).toMatch(/cannot send email or move funds/i);
  });

  it("empty state offers Connect Gmail and redirects to the signed Google URL (first-connect op, no code handling)", async () => {
    const url = "https://accounts.google.com/o/oauth2/v2/auth?scope=gmail.readonly&state=SIGNED";
    const connectCalls: string[] = [];
    api.apiFetch.mockImplementation((u: string) => {
      if (u === STATUS) return Promise.resolve({ connected: false, mailboxes: [] });
      if (u.startsWith(CONNECT)) { connectCalls.push(u); return Promise.resolve({ authorization_url: url }); }
      return Promise.resolve({});
    });
    const assign = vi.fn();
    Object.defineProperty(window, "location", { value: { assign, href: "" }, writable: true });
    render(<ConnectedEmailAccounts />);
    await screen.findByTestId("connected-email-panel");
    fireEvent.click(screen.getByTestId("connect-btn"));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(url));
    // the first-connect call carries the explicit op + the web return-journey, and NO mailbox id
    expect(connectCalls[0]).toContain("op=CONNECT_FIRST_MAILBOX");
    expect(connectCalls[0]).toContain("journey=web");
    expect(connectCalls[0]).not.toContain("mailbox_id");
    // the frontend never receives/handles an authorization code or token
    expect(url).not.toContain("code=");
  });

  it("reconnect-required mailbox shows the state and a reconnect action that TARGETS that mailbox", async () => {
    const connectCalls: string[] = [];
    const url = "https://accounts.google.com/o/oauth2/v2/auth?state=SIGNED";
    api.apiFetch.mockImplementation((u: string) => {
      if (u === STATUS) return Promise.resolve({ connected: true, mailboxes: [{ mailbox_id: 2, provider: "GMAIL",
        masked_email: "nr…@gmail.com", state: "RECONNECT_REQUIRED", connection_status: "CONNECTED",
        last_successful_sync: "2026-10-01T10:00:00Z", reconnect_required: true, has_verified_broker_bindings: false }] });
      if (u.startsWith(CONNECT)) { connectCalls.push(u); return Promise.resolve({ authorization_url: url }); }
      return Promise.resolve({});
    });
    const assign = vi.fn();
    Object.defineProperty(window, "location", { value: { assign, href: "" }, writable: true });
    render(<ConnectedEmailAccounts />);
    await screen.findByTestId("connected-email-panel");
    expect(screen.getByTestId("connected-email-state").textContent).toMatch(/reconnect required/i);
    fireEvent.click(screen.getByTestId("reconnect-btn"));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(url));
    // the reconnect is TARGETED: the explicit reconnect op AND the selected mailbox id are sent (never a generic connect)
    expect(connectCalls[0]).toContain("op=RECONNECT_EXISTING_MAILBOX");
    expect(connectCalls[0]).toContain("mailbox_id=2");
  });

  it("a revoked mailbox is not shown active and offers no inapplicable disconnect", async () => {
    statusOf([{ mailbox_id: 7, provider: "GMAIL", masked_email: "nr…@gmail.com", state: "REVOKED",
      connection_status: "REVOKED", last_successful_sync: null,
      reconnect_required: true, has_verified_broker_bindings: false }]);
    // connected=false for a revoked-only member
    api.apiFetch.mockImplementation((u: string) => {
      if (u === STATUS) return Promise.resolve({ connected: false, mailboxes: [{ mailbox_id: 7, provider: "GMAIL",
        masked_email: "nr…@gmail.com", state: "REVOKED", connection_status: "REVOKED", last_successful_sync: null,
        reconnect_required: true, has_verified_broker_bindings: false }] });
      return Promise.resolve({});
    });
    render(<ConnectedEmailAccounts />);
    await screen.findByTestId("connected-email-panel");
    expect(screen.getByTestId("connected-email-state").textContent).toMatch(/disconnected/i);
    expect(screen.queryByTestId("disconnect-btn")).toBeNull();     // no inapplicable 'disconnect'
    expect(screen.getByTestId("reconnect-btn")).toBeTruthy();      // can re-authorize
    expect(screen.getByTestId("connect-btn")).toBeTruthy();        // and a fresh connect is offered (nothing live)
  });

  it("disconnect requires an explicit confirm, then calls the owner-scoped revoke", async () => {
    const calls: Array<[string, unknown]> = [];
    api.apiFetch.mockImplementation((url: string, opts: unknown) => {
      calls.push([url, opts]);
      if (url === STATUS) return Promise.resolve({ connected: true, mailboxes: [{ mailbox_id: 5, provider: "GMAIL",
        masked_email: "nr…@gmail.com", state: "HEALTHY", connection_status: "CONNECTED",
        last_successful_sync: "2026-10-18T10:00:00Z", reconnect_required: false, has_verified_broker_bindings: false }] });
      return Promise.resolve({ ok: true, status: "revoked", mailbox_id: 5 });
    });
    render(<ConnectedEmailAccounts />);
    await screen.findByTestId("connected-email-panel");
    fireEvent.click(screen.getByTestId("disconnect-btn"));               // step 1: reveal confirm + warning
    expect(screen.getByTestId("disconnect-confirm-btn")).toBeTruthy();
    // the confirmation is explicit about what stops and what is kept (no future reads; history preserved)
    const panel = screen.getByTestId("connected-email-panel");
    expect(panel.textContent).toMatch(/no longer read/i);
    expect(panel.textContent).toMatch(/history is kept/i);
    fireEvent.click(screen.getByTestId("disconnect-confirm-btn"));       // step 2: confirm
    await waitFor(() => expect(calls.some(([u, o]) =>
      u === "/api/broker-intelligence/mailboxes/5/revoke/" && (o as { method?: string })?.method === "POST")).toBe(true));
  });

  it("renders multiple mailbox rows and reconnects the correct one (no wrong-row bug)", async () => {
    const connectCalls: string[] = [];
    const url = "https://accounts.google.com/o/oauth2/v2/auth?state=SIGNED";
    api.apiFetch.mockImplementation((u: string) => {
      if (u === STATUS) return Promise.resolve({ connected: true, mailboxes: [
        { mailbox_id: 11, provider: "GMAIL", masked_email: "aa…@gmail.com", state: "HEALTHY",
          connection_status: "CONNECTED", last_successful_sync: "2026-10-18T10:00:00Z",
          reconnect_required: false, has_verified_broker_bindings: false },
        { mailbox_id: 22, provider: "GMAIL", masked_email: "bb…@gmail.com", state: "RECONNECT_REQUIRED",
          connection_status: "CONNECTED", last_successful_sync: "2026-10-01T10:00:00Z",
          reconnect_required: true, has_verified_broker_bindings: false },
      ] });
      if (u.startsWith(CONNECT)) { connectCalls.push(u); return Promise.resolve({ authorization_url: url }); }
      return Promise.resolve({});
    });
    const assign = vi.fn();
    Object.defineProperty(window, "location", { value: { assign, href: "" }, writable: true });
    render(<ConnectedEmailAccounts />);
    await screen.findByTestId("connected-email-panel");
    expect(screen.getAllByTestId("connected-email-row")).toHaveLength(2);
    const reconnects = screen.getAllByTestId("reconnect-btn");
    fireEvent.click(reconnects[1]);   // the SECOND row (mailbox 22)
    await waitFor(() => expect(assign).toHaveBeenCalled());
    expect(connectCalls[0]).toContain("mailbox_id=22");       // acted on the right row
    expect(connectCalls[0]).not.toContain("mailbox_id=11");
  });

  // --- return journey from the Google callback (§506: align with the final callback contract) -------------------
  it("shows a success banner on a successful-consent return and still shows authoritative status", async () => {
    statusOf([{ mailbox_id: 1, provider: "GMAIL", masked_email: "nr…@gmail.com", state: "HEALTHY",
      connection_status: "CONNECTED", last_successful_sync: "2026-10-18T10:00:00Z",
      reconnect_required: false, has_verified_broker_bindings: true }]);
    atReturn("?mailbox_connect=connected");
    render(<ConnectedEmailAccounts />);
    const notice = await screen.findByTestId("connected-email-notice");
    expect(notice.textContent).toMatch(/connected/i);
    expect(screen.getByTestId("connected-email-state").textContent).toMatch(/monitoring/i);  // status authoritative
  });

  it("shows a customer-safe denial banner when consent was denied (no mailbox fabricated)", async () => {
    statusOf([]);
    atReturn("?mailbox_connect=consent_denied");
    render(<ConnectedEmailAccounts />);
    const notice = await screen.findByTestId("connected-email-notice");
    expect(notice.textContent).toMatch(/declined/i);
    expect(screen.queryAllByTestId("connected-email-row")).toHaveLength(0);   // marker can't fabricate a mailbox
  });

  it("shows a reconnect-with-same-account banner on an account mismatch", async () => {
    statusOf([{ mailbox_id: 3, provider: "GMAIL", masked_email: "nr…@gmail.com", state: "RECONNECT_REQUIRED",
      connection_status: "CONNECTED", last_successful_sync: null,
      reconnect_required: true, has_verified_broker_bindings: false }]);
    atReturn("?mailbox_connect=oauth_account_mismatch");
    render(<ConnectedEmailAccounts />);
    const notice = await screen.findByTestId("connected-email-notice");
    expect(notice.textContent).toMatch(/same account/i);
  });

  it("maps an unknown/other error code to a generic customer-safe banner (no raw diagnostic)", async () => {
    statusOf([]);
    atReturn("?mailbox_connect=oauth_no_refresh_token");
    render(<ConnectedEmailAccounts />);
    const notice = await screen.findByTestId("connected-email-notice");
    expect(notice.textContent).toMatch(/couldn.t complete/i);
    expect(notice.textContent).not.toMatch(/refresh_token/);   // never leaks the raw code to the member
  });

  it("localizes the return banner in Japanese", async () => {
    statusOf([]);
    atReturn("?mailbox_connect=consent_denied");
    render(<ConnectedEmailAccounts lang="ja" />);
    const notice = await screen.findByTestId("connected-email-notice");
    expect(notice.textContent).toMatch(/拒否/);   // 'denied' (ja)
  });

  it("shows no banner on a normal visit (no return marker)", async () => {
    statusOf([{ mailbox_id: 1, provider: "GMAIL", masked_email: "nr…@gmail.com", state: "HEALTHY",
      connection_status: "CONNECTED", last_successful_sync: "2026-10-18T10:00:00Z",
      reconnect_required: false, has_verified_broker_bindings: false }]);
    render(<ConnectedEmailAccounts />);
    await screen.findByTestId("connected-email-panel");
    expect(screen.queryByTestId("connected-email-notice")).toBeNull();
  });

  // --- WS2 multi-mailbox: Add email account control (owner-scoped cohort via can_add_mailbox) ---------------------
  it("shows 'Add email account' ONLY when can_add_mailbox is true, and adds via ADD_NEW_MAILBOX (no mailbox id)", async () => {
    const connectCalls: string[] = [];
    const url = "https://accounts.google.com/o/oauth2/v2/auth?state=SIGNED";
    api.apiFetch.mockImplementation((u: string) => {
      if (u === STATUS) return Promise.resolve({ connected: true, can_add_mailbox: true, mailbox_limit: 5, mailboxes: [
        { mailbox_id: 1, provider: "GMAIL", masked_email: "aa…@gmail.com", state: "HEALTHY",
          connection_status: "CONNECTED", last_successful_sync: "2026-10-18T10:00:00Z",
          reconnect_required: false, has_verified_broker_bindings: false }] });
      if (u.startsWith(CONNECT)) { connectCalls.push(u); return Promise.resolve({ authorization_url: url }); }
      return Promise.resolve({});
    });
    const assign = vi.fn();
    Object.defineProperty(window, "location", { value: { assign, href: "" }, writable: true });
    render(<ConnectedEmailAccounts />);
    await screen.findByTestId("connected-email-panel");
    fireEvent.click(screen.getByTestId("add-account-btn"));
    await waitFor(() => expect(assign).toHaveBeenCalledWith(url));
    expect(connectCalls[0]).toContain("op=ADD_NEW_MAILBOX");
    expect(connectCalls[0]).not.toContain("mailbox_id");      // ADD_NEW names no target — never retargets a row
  });

  it("hides 'Add email account' when can_add_mailbox is absent/false (not in cohort, or at the cap)", async () => {
    statusOf([{ mailbox_id: 1, provider: "GMAIL", masked_email: "aa…@gmail.com", state: "HEALTHY",
      connection_status: "CONNECTED", last_successful_sync: "2026-10-18T10:00:00Z",
      reconnect_required: false, has_verified_broker_bindings: false }]);   // statusOf omits can_add_mailbox
    render(<ConnectedEmailAccounts />);
    await screen.findByTestId("connected-email-panel");
    expect(screen.queryByTestId("add-account-btn")).toBeNull();
  });

  it("shows an 'already connected' banner on a duplicate add", async () => {
    statusOf([{ mailbox_id: 1, provider: "GMAIL", masked_email: "aa…@gmail.com", state: "HEALTHY",
      connection_status: "CONNECTED", last_successful_sync: "2026-10-18T10:00:00Z",
      reconnect_required: false, has_verified_broker_bindings: false }]);
    atReturn("?mailbox_connect=mailbox_already_connected");
    render(<ConnectedEmailAccounts />);
    const notice = await screen.findByTestId("connected-email-notice");
    expect(notice.textContent).toMatch(/already connected/i);
  });

  it("shows a 'limit reached' banner when the mailbox cap is hit", async () => {
    statusOf([{ mailbox_id: 1, provider: "GMAIL", masked_email: "aa…@gmail.com", state: "HEALTHY",
      connection_status: "CONNECTED", last_successful_sync: "2026-10-18T10:00:00Z",
      reconnect_required: false, has_verified_broker_bindings: false }]);
    atReturn("?mailbox_connect=mailbox_limit_reached");
    render(<ConnectedEmailAccounts />);
    const notice = await screen.findByTestId("connected-email-notice");
    expect(notice.textContent).toMatch(/maximum/i);
  });

  it("one unhealthy mailbox row does not hide the healthy mailboxes", async () => {
    statusOf([
      { mailbox_id: 1, provider: "GMAIL", masked_email: "aa…@gmail.com", state: "HEALTHY",
        connection_status: "CONNECTED", last_successful_sync: "2026-10-18T10:00:00Z",
        reconnect_required: false, has_verified_broker_bindings: false },
      { mailbox_id: 2, provider: "GMAIL", masked_email: "bb…@gmail.com", state: "RECONNECT_REQUIRED",
        connection_status: "CONNECTED", last_successful_sync: null,
        reconnect_required: true, has_verified_broker_bindings: false },
    ]);
    render(<ConnectedEmailAccounts />);
    await screen.findByTestId("connected-email-panel");
    expect(screen.getAllByTestId("connected-email-row")).toHaveLength(2);
  });
});

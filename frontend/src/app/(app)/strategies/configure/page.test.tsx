import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

/** AJ#7 Configure page — the honest contract + the Enable confirmation that folds ADR-0047. Proves: no
 * cosmetic editable controls; Enable requires an explicit confirmation modal; cancel does nothing; confirm
 * orchestrates authorize → arm then redirects; partial failure is retryable and keeps truthful state; an
 * already-enabled strategy offers Disable; a generic strategy never claims automated execution. */

type Status = { armed: boolean; enabled: boolean; ambiguous?: boolean; account_id?: number | null };
const { state, apiFetch, push, fetchJourney, authorizeExecution } = vi.hoisted(() => {
  const state = {
    search: "mp=mp-010&account=5",
    status: { armed: false, enabled: false } as Status,
    journey: { execution_authorized: false, can_enable_automated_trading: true } as Record<string, unknown> | null,
    armError: null as null | { httpStatus?: number; body?: { status?: string } },
    journeyThrows: false,   // simulate a TRANSIENT (non-404) journey error that fetchJourney rethrows
    accounts: [{ id: 5, name: "Demo A", account_number: "1302587", is_demo: true, is_active: true }],
  };
  const push = vi.fn();
  const authorizeExecution = vi.fn(async () => ({}));
  const fetchJourney = vi.fn(async () => {
    if (state.journeyThrows) throw new Error("502");   // transient upstream error → rethrown by fetchJourney
    return state.journey ? { ok: true, journey: state.journey } : { ok: false, unavailable: true };
  });
  const apiFetch = vi.fn(async (path: string, opts?: RequestInit) => {
    if (path.startsWith("/api/auth/me")) return {};
    if (path.startsWith("/api/trading/accounts")) return state.accounts;
    if (path.includes("/signal-copy/status")) return { ...state.status };
    if (path.includes("/signal-copy/get")) { state.status = { armed: true, enabled: false, account_id: 5 }; return { status: "owned", assignment_id: 9, enabled: false }; }
    if (path.includes("/signal-copy/arm")) {
      if (state.armError) { throw Object.assign(new Error("nope"), state.armError); }
      state.status = { armed: true, enabled: true, account_id: 5 };
      return { status: "armed", enabled: true };
    }
    if (path.includes("/signal-copy/toggle")) { state.status = { armed: true, enabled: false, account_id: 5 }; return { status: "disabled", enabled: false }; }
    void opts;
    return {};
  });
  return { state, apiFetch, push, fetchJourney, authorizeExecution };
});

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/lib/hosted-journey", () => ({ fetchJourney, authorizeExecution }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(state.search),
}));

import ConfigurePage from "./page";

const armCalls = () => apiFetch.mock.calls.filter((c) => String(c[0]).includes("/signal-copy/arm")).length;

describe("Configure page — Wayond (automated)", () => {
  beforeEach(() => {
    apiFetch.mockClear(); push.mockClear(); authorizeExecution.mockClear(); fetchJourney.mockClear();
    state.search = "mp=mp-010&account=5";
    state.status = { armed: true, enabled: false, account_id: 5 };  // owned, not enabled
    state.journey = { execution_authorized: false, can_enable_automated_trading: true };
    state.armError = null;
    state.journeyThrows = false;
  });

  it("shows the honest contract with managed rows and NO editable sizing/TP/SL controls", async () => {
    render(<ConfigurePage />);
    await screen.findByText("Strategy settings");
    // Real, honest values — not editable inputs.
    expect(screen.getByText("Managed by GuvFX (beta)")).toBeTruthy();
    expect(screen.getByText("Follows the provider's targets")).toBeTruthy();
    expect(screen.getByText("Not used by this strategy")).toBeTruthy();
    // There is NOT a single free-text / numeric control anywhere on the page (no cosmetic knobs).
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByRole("spinbutton")).toBeNull();
    expect(screen.getByText(/advanced customisation|coming soon|GuvFX-managed settings/i)).toBeTruthy();
  });

  it("Enable requires an explicit confirmation; merely rendering never arms or authorizes", async () => {
    render(<ConfigurePage />);
    const enable = await screen.findByRole("button", { name: "Enable Strategy" });
    expect(armCalls()).toBe(0);
    expect(authorizeExecution).not.toHaveBeenCalled();
    fireEvent.click(enable);
    // A confirmation dialog appears — the click alone still hasn't armed/authorized.
    await screen.findByRole("dialog");
    expect(screen.getByText(/allow GuvFX to place trades automatically/i)).toBeTruthy();
    expect(armCalls()).toBe(0);
    expect(authorizeExecution).not.toHaveBeenCalled();
  });

  it("Cancel does nothing (no authorize, no arm, no redirect)", async () => {
    render(<ConfigurePage />);
    fireEvent.click(await screen.findByRole("button", { name: "Enable Strategy" }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(authorizeExecution).not.toHaveBeenCalled();
    expect(armCalls()).toBe(0);
    expect(push).not.toHaveBeenCalled();
  });

  it("Confirm orchestrates authorize → arm, then redirects to My Strategies", async () => {
    render(<ConfigurePage />);
    fireEvent.click(await screen.findByRole("button", { name: "Enable Strategy" }));
    const dialog = await screen.findByRole("dialog");
    // The confirm button inside the dialog is the explicit ADR-0047 consent.
    fireEvent.click(within(dialog).getByRole("button", { name: "Enable Strategy" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/strategies?enabled=1"));
    expect(authorizeExecution).toHaveBeenCalledTimes(1);
    expect(armCalls()).toBe(1);
  });

  it("partial failure (arm fails) keeps the modal open, shows a retryable error, no redirect; retry succeeds", async () => {
    state.armError = { httpStatus: 409, body: { status: "runtime_not_ready" } };
    render(<ConfigurePage />);
    fireEvent.click(await screen.findByRole("button", { name: "Enable Strategy" }));
    let dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Enable Strategy" }));
    // Truthful failure: still on the dialog, customer-safe message, no redirect, exactly one arm attempt.
    await screen.findByRole("alert");
    expect(screen.getByText(/still getting ready/i)).toBeTruthy();
    expect(push).not.toHaveBeenCalled();
    expect(armCalls()).toBe(1);
    // Retry now succeeds → single additional arm, then redirect.
    state.armError = null;
    dialog = screen.getByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith("/strategies?enabled=1"));
    expect(armCalls()).toBe(2);
  });

  it("an already-enabled strategy offers Disable + Manage, never a second Enable", async () => {
    state.status = { armed: true, enabled: true, account_id: 5 };
    render(<ConfigurePage />);
    await screen.findByText("Automated trading is enabled");
    expect(screen.getByRole("button", { name: "Disable Strategy" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Enable Strategy" })).toBeNull();
  });

  it("not-owned + account → offers Get Strategy (acquire), which never arms", async () => {
    state.status = { armed: false, enabled: false };
    render(<ConfigurePage />);
    const get = await screen.findByRole("button", { name: "Get Strategy" });
    fireEvent.click(get);
    await waitFor(() => expect(apiFetch.mock.calls.some((c) => String(c[0]).includes("/signal-copy/get"))).toBe(true));
    expect(armCalls()).toBe(0);
    expect(authorizeExecution).not.toHaveBeenCalled();
  });

  it("workspace preparing → Enable degrades to a 'getting ready' state, never an Enable modal", async () => {
    // Journey loads OK but is not yet enable-able (still preparing) → the auto-updating getting-ready panel.
    state.journey = { phase: "WORKSPACE_PREPARING", next_action: "wait", execution_authorized: false, can_enable_automated_trading: false };
    state.status = { armed: true, enabled: false, account_id: 5 };
    render(<ConfigurePage />);
    await screen.findByText(/getting ready/i);
    expect(screen.queryByRole("button", { name: "Enable Strategy" })).toBeNull();
    // AJ#7.1 nav-loop guard (#12): the forward action opens the customer's MetaTrader terminal (stable page),
    // and must NEVER link to /onboarding/hosted (which bounces to the marketplace → owned card → Configure).
    const fwd = screen.getByRole("link", { name: /open metatrader/i });
    expect(fwd).toHaveAttribute("href", "/trading/terminal-access");
    expect(screen.queryByRole("link", { name: /open your workspace/i })).toBeNull();
    for (const l of screen.queryAllByRole("link")) {
      expect(l.getAttribute("href")).not.toBe("/onboarding/hosted");
    }
  });

  it("workspace unavailable (journey fails to load) → honest 'needs attention' + Contact support, no false auto-update promise, no bounce", async () => {
    // AJ#7.2 adversarial fix: a workspace that will NOT become ready on its own must NOT show the
    // auto-updating getting-ready panel (a false "the Enable button will appear here" promise). It surfaces
    // an honest attention state with a support route — and still never bounces to /onboarding/hosted.
    state.journey = null;                       // fetchJourney → {ok:false} (unavailable / not entitled)
    state.status = { armed: true, enabled: false, account_id: 5 };
    render(<ConfigurePage />);
    await screen.findByText(/needs attention/i);
    expect(screen.getByRole("link", { name: /contact support/i })).toBeTruthy();
    expect(screen.queryByText(/updates automatically/i)).toBeNull();   // no false self-heal promise
    expect(screen.queryByText(/getting ready/i)).toBeNull();
    expect(screen.queryByRole("button", { name: "Enable Strategy" })).toBeNull();
    for (const l of screen.queryAllByRole("link")) {
      expect(l.getAttribute("href")).not.toBe("/onboarding/hosted");
    }
  });

  it("a TRANSIENT journey error (thrown 5xx, not a 404) shows getting-ready — never the sticky 'contact support'", async () => {
    // fetchJourney rethrows non-404 errors; the page must treat that as not-yet-loaded (getting-ready, which
    // the poll retries) rather than a permanent 'needs attention', so a momentary blip self-heals.
    state.journeyThrows = true;
    state.status = { armed: true, enabled: false, account_id: 5 };
    render(<ConfigurePage />);
    await screen.findByText(/getting ready/i);
    expect(screen.queryByRole("link", { name: /contact support/i })).toBeNull();
    expect(screen.queryByRole("button", { name: "Enable Strategy" })).toBeNull();
    state.journeyThrows = false;   // reset for other tests
  });

  it("a phase that needs a customer step (NO_WORKSPACE) → 'Continue setup', no false auto-update promise", async () => {
    // AJ#7.2 adversarial fix: polling can't advance a phase that requires the customer to act, so the panel
    // must NOT promise the Enable button will appear automatically — it sends them to finish setup instead.
    state.journey = { phase: "NO_WORKSPACE", next_action: "request_workspace", execution_authorized: false, can_enable_automated_trading: false };
    state.status = { armed: true, enabled: false, account_id: 5 };
    render(<ConfigurePage />);
    await screen.findByText(/finish setting up/i);
    const cont = screen.getByRole("link", { name: /continue setup/i });
    expect(cont).toHaveAttribute("href", "/onboarding/hosted");   // correct destination; loop-free (not READY)
    expect(screen.queryByText(/updates automatically/i)).toBeNull();     // no false self-heal promise
    expect(screen.queryByRole("button", { name: "Enable Strategy" })).toBeNull();
  });
});

describe("Configure page — generic (research/template)", () => {
  beforeEach(() => {
    apiFetch.mockClear(); push.mockClear(); authorizeExecution.mockClear(); fetchJourney.mockClear();
    state.search = "mp=mp-001&account=5";
    state.status = { armed: false, enabled: false };
  });

  it("never claims automated execution and offers no Enable", async () => {
    render(<ConfigurePage />);
    await screen.findByText("Research strategy");
    expect(screen.getByText(/place trades automatically/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Enable Strategy" })).toBeNull();
    // Never touches the signal-copy execution endpoints for a generic strategy.
    expect(apiFetch.mock.calls.some((c) => String(c[0]).includes("/signal-copy/"))).toBe(false);
    expect(authorizeExecution).not.toHaveBeenCalled();
  });
});

// Local import to avoid a top-level testing-library helper name clash.
import { within } from "@testing-library/react";

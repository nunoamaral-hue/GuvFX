import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock only the journey I/O; keep the real (unit-tested) describeJourney/STEPS state machine.
const jm = vi.hoisted(() => ({
  fetchJourney: vi.fn(),
  requestWorkspace: vi.fn(),
  bindExpectedAccount: vi.fn(),
  confirmAccount: vi.fn(),
}));
vi.mock("@/lib/hosted-journey", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/hosted-journey")>();
  return { ...actual, ...jm };
});

// AJ#4: the real RemoteApp component (network detection + Guacamole iframe) has its own tests; here we stub it
// to a marker so the journey tests verify WHERE it is embedded and WHEN, not its internals.
vi.mock("@/components/hosted/HostedMt5RemoteApp", () => ({
  HostedMt5RemoteApp: () => <div data-testid="onboarding-mt5-embed">Embedded MetaTrader</div>,
}));

import { HostedWorkspaceJourney, deriveStageIndex } from "@/components/onboarding/HostedWorkspaceJourney";
import { LanguageProvider } from "@/components/AppShell";
import type { HostedJourney } from "@/lib/hosted-journey";

function journey(over: Partial<HostedJourney> = {}): HostedJourney {
  return {
    phase: "WORKSPACE_READY", next_action: "assign_strategy", confirmed: true,
    strategy_eligible: true, delivery: "DELIVERY_READY", active_login_masked: "***561",
    identity_declared: true, ...over,
  };
}

// Any legacy account-creation affordance that must NOT appear on the hosted journey.
const LEGACY = [/create account/i, /add account/i, /new account/i, /connect a broker account/i];

beforeEach(() => { jm.fetchJourney.mockReset(); jm.requestWorkspace.mockReset(); jm.bindExpectedAccount.mockReset(); jm.confirmAccount.mockReset(); });
afterEach(() => cleanup());

// Mirror the SERVER through the deferred-bind: before the write-once bind the journey reports
// identity_declared=false (declaration form); after bind it reports true (waiting/ready panel). fetchJourney
// flips with the bind call — exactly like production — so the 5s background poll can NEVER regress a linked
// customer back to the form. `after` defaults to the same phase as `before`.
function declareFlow(before: Partial<HostedJourney>, after: Partial<HostedJourney> = before) {
  let bound = false;
  jm.fetchJourney.mockImplementation(async () => ({
    ok: true, journey: bound ? journey({ ...after, identity_declared: true })
                              : journey({ ...before, identity_declared: false }),
  }));
  jm.bindExpectedAccount.mockImplementation(async () => { bound = true; return journey({ ...after, identity_declared: true }); });
}

// AJ#3 redesign: the customer DECLARES their account (form) first; after Save the waiting/ready panel OWNS the
// page. This helper performs the declare step so a test can reach the owned states (real timers only).
async function saveBrokerDetails(login = "700900") {
  fireEvent.change(await screen.findByLabelText(/broker account number/i), { target: { value: login } });
  fireEvent.click(screen.getByRole("button", { name: /save my broker details/i }));
}

describe("HostedWorkspaceJourney", () => {
  it("renders the production-observed preparing panel entirely in Japanese", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({
      phase: "WORKSPACE_PREPARING", next_action: "wait", delivery: "DELIVERY_PREPARING",
      identity_declared: true, strategy_eligible: false,
    }) });
    const { container } = render(
      <LanguageProvider lang="ja"><HostedWorkspaceJourney /></LanguageProvider>,
    );
    expect(await screen.findByRole("heading", { name: "ワークスペースを準備しています" })).toBeInTheDocument();
    expect(screen.getByText(/お客様専用の独立したMT5ワークスペースを構築しています/)).toBeInTheDocument();
    expect(container.textContent).not.toContain("Preparing your workspace");
    expect(container.textContent).not.toContain("We're building your private");
  });

  it("AJ#4: once linked + deliverable, MetaTrader is EMBEDDED inside onboarding (no navigation to Terminal Access)", async () => {
    // no broker login observed yet (active_login_masked empty) → "Log into your broker account" copy.
    declareFlow({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_READY", active_login_masked: "" });
    render(<HostedWorkspaceJourney />);
    await saveBrokerDetails();
    // The MT5 terminal is embedded INLINE — the customer never leaves onboarding.
    expect(await screen.findByTestId("onboarding-mt5-embed")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /log into your broker account/i })).toBeInTheDocument();
    // AJ#5: the intentional "You're using MetaTrader" frame + live detection status.
    expect(screen.getByText(/you're using metatrader/i)).toBeInTheDocument();
    expect(screen.getAllByText(/detecting your account/i).length).toBeGreaterThan(0);
    // AJ#5.1: the live-status wizard rail — completed + active + pending stages all visible.
    expect(screen.getByRole("list", { name: /onboarding progress/i })).toBeInTheDocument();
    expect(screen.getByText(/waiting for broker login/i)).toBeInTheDocument();
    expect(screen.getByText(/workspace ready/i)).toBeInTheDocument();
    // No link out to Terminal Access anywhere on the page.
    const hrefs = Array.from(document.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain("/trading/terminal-access");
    // the page is OWNED by this step — the declaration form + its Save button are gone.
    expect(screen.queryByRole("button", { name: /save my broker details/i })).toBeNull();
  });

  it("AJ#3: after linking, a not-ready workspace shows the waiting panel — never a live launch link", async () => {
    declareFlow({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_PREPARING" });
    const { container } = render(<HostedWorkspaceJourney />);
    await saveBrokerDetails();
    expect((await screen.findAllByText(/setting up your secure metatrader workspace/i)).length).toBeGreaterThan(0);
    const hrefs = Array.from(container.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain("/trading/terminal-access");
  });

  it("AJ#3: the waiting panel OWNS the page — links the account, tells the customer to stay, shows an active timeline", async () => {
    declareFlow({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_PREPARING" });
    render(<HostedWorkspaceJourney />);
    await saveBrokerDetails();
    expect((await screen.findAllByText(/broker account linked/i)).length).toBeGreaterThan(0);
    expect(screen.getByText(/remain on this page/i)).toBeInTheDocument();
    expect(screen.getByText(/automatically continue when everything is ready/i)).toBeInTheDocument();
    // active preparation timeline (no invented percentages)
    expect(screen.getByText(/workspace requested/i)).toBeInTheDocument();
    expect(screen.getAllByText(/setting up your secure metatrader workspace/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/final verification/i)).toBeInTheDocument();
    // Sponsor micro-polish: a password reassurance persists into the waiting state (the form's own note is gone).
    expect(screen.getByText(/never ask for it here/i)).toBeInTheDocument();
    // Sponsor: a FINAL explicit instruction — stay on this page; the single next action (Open MetaTrader) will
    // appear here automatically when the workspace is ready (no navigation, no manual refresh).
    expect(screen.getByText(/keep this page open/i)).toBeInTheDocument();
    expect(screen.getByText(/appear here automatically/i)).toBeInTheDocument();
    // owned page — the form is hidden COMPLETELY (no inputs), the Save button is gone, no competing launch link
    expect(screen.queryByRole("button", { name: /save my broker details/i })).toBeNull();
    expect(screen.queryByLabelText(/broker account number/i)).toBeNull();
    expect(screen.queryByLabelText(/broker server/i)).toBeNull();
    expect(screen.queryByRole("link", { name: /open metatrader/i })).toBeNull();
  });

  it("AJ#3: after the normal window the waiting copy becomes 'taking a little longer than expected' (never an endless spinner)", async () => {
    vi.useFakeTimers();
    try {
      declareFlow({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_PREPARING" });
      render(<HostedWorkspaceJourney />);
      await act(async () => { await vi.advanceTimersByTimeAsync(10); });   // flush initial fetch → declare form
      fireEvent.change(screen.getByLabelText(/broker account number/i), { target: { value: "700900" } });
      fireEvent.click(screen.getByRole("button", { name: /save my broker details/i }));
      await act(async () => { await vi.advanceTimersByTimeAsync(10); });   // flush bind → waiting panel
      expect(screen.getAllByText(/broker account linked/i).length).toBeGreaterThan(0);
      expect(screen.queryByText(/taking a little longer than expected/i)).toBeNull();
      await act(async () => { await vi.advanceTimersByTimeAsync(120_001); });   // past SLOW_WAIT_MS
      expect(screen.getByText(/taking a little longer than expected/i)).toBeInTheDocument();
      expect(screen.getByText(/contact support/i)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("AJ#3: the long-wait timer is anchored to LINK time, not to time spent on the declare form", async () => {
    vi.useFakeTimers();
    try {
      declareFlow({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_PREPARING" });
      render(<HostedWorkspaceJourney />);
      await act(async () => { await vi.advanceTimersByTimeAsync(10); });        // declare form on screen
      // customer lingers on the form well past the slow-wait window BEFORE linking — the clock must not be running
      await act(async () => { await vi.advanceTimersByTimeAsync(120_001); });
      fireEvent.change(screen.getByLabelText(/broker account number/i), { target: { value: "700900" } });
      fireEvent.click(screen.getByRole("button", { name: /save my broker details/i }));
      await act(async () => { await vi.advanceTimersByTimeAsync(10); });        // just linked
      // countdown starts at LINK time → the fresh copy shows, NOT the premature "taking longer than expected"
      expect(screen.queryByText(/taking a little longer than expected/i)).toBeNull();
      expect(screen.getAllByText(/broker account linked/i).length).toBeGreaterThan(0);
    } finally {
      vi.useRealTimers();
    }
  });

  it("BB#1 + AJ#4: once linked, a DELIVERABLE workspace embeds MetaTrader (openable BEFORE CONNECTED)", async () => {
    declareFlow({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_DELIVERABLE" });
    render(<HostedWorkspaceJourney />);
    await saveBrokerDetails();
    expect(await screen.findByTestId("onboarding-mt5-embed")).toBeInTheDocument();       // embedded inline
    expect(screen.queryByText(/setting up your secure metatrader workspace/i)).toBeNull();  // openable, not waiting
  });

  it("AJ#3: identity_declared is the source of truth — an already-linked workspace shows the waiting panel on a fresh load, never the form", async () => {
    // Simulates a hard page RELOAD after the write-once bind: no local state, the server reports the identity as
    // already declared, so the declaration form must NOT re-appear — deterministic across refreshes/devices.
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({
      phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_PREPARING",
      identity_declared: true }) });
    render(<HostedWorkspaceJourney />);
    expect((await screen.findAllByText(/broker account linked/i)).length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: /save my broker details/i })).toBeNull();
    expect(screen.queryByLabelText(/broker account number/i)).toBeNull();   // the form never re-shows on reload
  });

  it("AJ#4: a wrong-account (BROKER_CONNECTED) state keeps the corrective guidance AND embeds MetaTrader inline", async () => {
    // The customer linked, then logged MetaTrader into the wrong broker account. The corrective guidance stays
    // visible AND the embedded terminal is right there so they fix it without leaving; the form never re-appears.
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({
      phase: "BROKER_CONNECTED", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_READY",
      active_login_masked: "***561", identity_declared: true }) });
    render(<HostedWorkspaceJourney />);
    expect(await screen.findByText(/account you told us/i)).toBeInTheDocument();
    expect(screen.getByTestId("onboarding-mt5-embed")).toBeInTheDocument();
    // No navigation out to Terminal Access.
    const hrefs = Array.from(document.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain("/trading/terminal-access");
    expect(screen.queryByRole("button", { name: /save my broker details/i })).toBeNull();
    expect(screen.queryByLabelText(/broker account number/i)).toBeNull();
  });

  it("fails closed to a neutral 'not available' when the journey is dark (404)", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: false, unavailable: true });
    render(<HostedWorkspaceJourney />);
    expect(await screen.findByText(/isn't available yet/i)).toBeInTheDocument();
    expect(screen.queryByRole("form")).not.toBeInTheDocument();
  });

  it("the workspace-unavailable state gives an actionable Contact support control — never a plain-text dead end", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({ phase: "WORKSPACE_UNAVAILABLE", next_action: "contact_support", delivery: "DELIVERY_NOT_AVAILABLE" }) });
    render(<HostedWorkspaceJourney />);
    const link = await screen.findByRole("link", { name: /contact support/i });
    expect(link.getAttribute("href")).toMatch(/^mailto:/);
  });

  it("fails closed to a retryable error state when the load throws", async () => {
    jm.fetchJourney.mockRejectedValueOnce(new Error("boom"));
    render(<HostedWorkspaceJourney />);
    expect(await screen.findByRole("button", { name: /try again/i })).toBeInTheDocument();
  });

  it("requests a workspace with NO broker details (deferred bind) — bare button, no inputs", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({ phase: "NO_WORKSPACE", next_action: "request_workspace", delivery: "DELIVERY_NOT_AVAILABLE", identity_declared: false }) });
    jm.requestWorkspace.mockResolvedValue(journey({ phase: "WORKSPACE_REQUESTED", next_action: "wait" }));
    const { container } = render(<HostedWorkspaceJourney />);
    const button = await screen.findByRole("button", { name: /request workspace/i });
    // No broker inputs and certainly no password field at the request step — identity is declared later.
    expect(container.querySelector("input")).toBeNull();
    fireEvent.click(button);
    await waitFor(() => expect(jm.requestWorkspace).toHaveBeenCalledTimes(1));
    expect(jm.requestWorkspace.mock.calls[0].length).toBe(0);   // called with NO arguments
  });

  it("declares the broker identity at the connect step (deferred bind) — never a password", async () => {
    declareFlow({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_READY" },
                { phase: "BROKER_CONNECTED", next_action: "open_mt5_and_log_in" });
    const { container } = render(<HostedWorkspaceJourney />);
    // UX: the account-number field now has a real <label> (a11y) — query by label, not placeholder.
    const input = await screen.findByLabelText(/broker account number/i);
    expect(container.querySelector('input[type="password"]')).toBeNull();
    fireEvent.change(input, { target: { value: "700900" } });
    fireEvent.click(screen.getByRole("button", { name: /save my broker details/i }));
    await waitFor(() => expect(jm.bindExpectedAccount).toHaveBeenCalledTimes(1));
    const arg = jm.bindExpectedAccount.mock.calls[0][0];
    expect(arg.expected_login).toBe("700900");
    expect(JSON.stringify(arg).toLowerCase()).not.toContain("password");
  });

  it("AJ#3: acknowledges the linked account immediately after save (no silent wait)", async () => {
    declareFlow({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in", delivery: "DELIVERY_PREPARING" });
    render(<HostedWorkspaceJourney />);
    await saveBrokerDetails();
    expect((await screen.findAllByText(/broker account linked/i)).length).toBeGreaterThan(0);
  });

  it("never renders a legacy account-creation path or leaks internal identifiers", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({ phase: "ACCOUNT_CONFIRMATION_REQUIRED", next_action: "confirm_broker_account", delivery: "DELIVERY_READY" }) });
    const { container } = render(<HostedWorkspaceJourney />);
    await screen.findByRole("heading", { name: /account detected/i });
    for (const rx of LEGACY) expect(screen.queryByText(rx)).toBeNull();
    // No link back into the legacy /accounts creation surface.
    const hrefs = Array.from(container.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain("/accounts");
    const blob = (container.textContent || "").toLowerCase();
    for (const bad of ["canonical", "provisioning", "execution_node", "rdp_host", "guvfx_u_", "auto_shadow"]) {
      expect(blob).not.toContain(bad);
    }
  });

  it("AJ#4: the confirm step is the retained activation ('I confirm this is my trading account') inside onboarding — then auto-advances to Ready with no spinner", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({
      phase: "ACCOUNT_CONFIRMATION_REQUIRED", next_action: "confirm_broker_account", delivery: "DELIVERY_READY",
      active_login_masked: "***561" }) });
    jm.confirmAccount.mockResolvedValue(journey({ phase: "WORKSPACE_READY", next_action: "assign_strategy" }));
    render(<HostedWorkspaceJourney />);
    const confirmBtn = await screen.findByRole("button", { name: /i confirm this is my trading account/i });
    expect(screen.getByRole("heading", { name: /account detected/i })).toBeInTheDocument();
    // Identity already proven — the copy CONFIRMS ownership, it does not ask the customer to prove who they are.
    expect(screen.getByText(/identity is already verified/i)).toBeInTheDocument();
    // No detour: never a link to Broker Accounts or Terminal Access from the confirm step.
    const hrefs = Array.from(document.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).not.toContain("/accounts");
    expect(hrefs).not.toContain("/trading/terminal-access");
    // Confirming transitions straight to Workspace Ready — the customer never sees another spinner.
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(jm.confirmAccount).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/workspace ready/i)).toBeInTheDocument();
  });

  it("AJ#4: Workspace Ready offers Choose Strategy (primary) + a secondary Open MetaTrader that re-opens MT5 inline (no duplicate session by default)", async () => {
    jm.fetchJourney.mockResolvedValue({ ok: true, journey: journey({
      phase: "WORKSPACE_READY", next_action: "assign_strategy", delivery: "DELIVERY_READY" }) });
    render(<HostedWorkspaceJourney />);
    const choose = await screen.findByRole("link", { name: /choose strategy/i });
    expect(choose).toHaveAttribute("href", "/strategies/marketplace");
    // MT5 is NOT mounted on the ready page until the customer asks — no duplicate terminal session by default.
    expect(screen.queryByTestId("onboarding-mt5-embed")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /open metatrader/i }));
    expect(await screen.findByTestId("onboarding-mt5-embed")).toBeInTheDocument();  // re-opens inline, still in onboarding
  });
});

describe("deriveStageIndex (AJ#5.1 live-status wizard — pure, genuine journey state only)", () => {
  const j = (over: Partial<HostedJourney>): HostedJourney => ({
    phase: "NO_WORKSPACE", next_action: "request_workspace", confirmed: false, strategy_eligible: false,
    delivery: "DELIVERY_NOT_AVAILABLE", active_login_masked: "", identity_declared: false, ...over,
  });
  it("waits for broker login before any login is observed", () => {
    expect(deriveStageIndex(j({ phase: "AWAITING_BROKER_LOGIN", active_login_masked: "" }))).toBe(1);
  });
  it("advances to detecting once a login is observed", () => {
    expect(deriveStageIndex(j({ phase: "AWAITING_BROKER_LOGIN", active_login_masked: "***561" }))).toBe(2);
    expect(deriveStageIndex(j({ phase: "BROKER_CONNECTED", active_login_masked: "***561" }))).toBe(2);
  });
  it("moves to confirmation, then all-done", () => {
    expect(deriveStageIndex(j({ phase: "ACCOUNT_CONFIRMATION_REQUIRED" }))).toBe(4);
    expect(deriveStageIndex(j({ phase: "WORKSPACE_READY" }))).toBe(6);
  });
  it("is null-safe and never invents progress for unknown/early states", () => {
    expect(deriveStageIndex(null)).toBe(0);
    expect(deriveStageIndex(j({ phase: "WORKSPACE_PREPARING" }))).toBe(0);
  });
});

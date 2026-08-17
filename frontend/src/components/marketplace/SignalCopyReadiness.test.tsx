import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

/** WS-D — the readiness panel that replaces the opaque "not armed" hint. It must render the backend's
 * ✓/✕ checklist + one customer-safe next action, gate the Enable-Trading button on the backend's can_arm
 * (never on frontend visibility alone), and degrade to a neutral "unavailable" — never a false "not
 * ready" — when the read-only status fetch fails. */
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch }));
// eslint-disable-next-line @typescript-eslint/no-explicit-any
vi.mock("next/link", () => ({ default: ({ children, href }: any) => <a href={href}>{children}</a> }));

import { SignalCopyReadiness } from "./SignalCopyReadiness";

const READY = {
  state: "READY", armed: false, enabled: false, can_arm: true, account_id: 5,
  next_action: "ready_enable",
  checklist: [
    { key: "demo", ok: true }, { key: "active", ok: true }, { key: "credentials", ok: true },
    { key: "runtime_ready", ok: true }, { key: "pilot_access", ok: true },
  ],
};

function props(over: Record<string, unknown> = {}) {
  return {
    lang: "en" as const,
    marketplaceStrategyId: "mp-010",
    accounts: [{ id: 5, name: "Demo", is_demo: true }],
    selectedAccountId: 5 as number | "" | undefined,
    onSelectAccount: vi.fn(),
    armUiEnabled: false,
    isAuthed: true,
    arming: false,
    onArm: vi.fn(),
    ...over,
  };
}

describe("SignalCopyReadiness (WS-D)", () => {
  beforeEach(() => { apiFetch.mockReset(); });

  it("shows an add-account prompt (and fetches nothing) when there are no demo accounts", () => {
    render(<SignalCopyReadiness {...props({
      accounts: [{ id: 9, name: "Live", is_demo: false }], selectedAccountId: undefined,
    })} />);
    expect(screen.getByText(/demo account first/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Go to Broker Accounts/i })).toHaveAttribute("href", "/accounts");
    expect(apiFetch).not.toHaveBeenCalled();
  });

  it("renders the checklist + a nav next action (no live Enable button) when the arm UI is DARK", async () => {
    apiFetch.mockResolvedValue(READY);
    render(<SignalCopyReadiness {...props({ armUiEnabled: false })} />);
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(screen.getByText("Trading access enabled")).toBeInTheDocument();
    expect(screen.getByText(/ready\. Enable trading/i)).toBeInTheDocument();
    // DARK build: no live arm control ever appears...
    expect(screen.queryByRole("button", { name: /Enable Trading/i })).toBeNull();
    // ...but the card is never status-only — it offers a navigation next action (P0.2).
    expect(screen.getByRole("link", { name: "Continue" })).toHaveAttribute("href", "/onboarding/hosted");
  });

  it("gives a navigation action for a denial that has no button path (single_tenant → Accounts)", async () => {
    apiFetch.mockResolvedValue({ ...READY, can_arm: false, next_action: "single_tenant" });
    render(<SignalCopyReadiness {...props({ armUiEnabled: true })} />);
    const link = await screen.findByRole("link", { name: /go to accounts/i });
    expect(link).toHaveAttribute("href", "/accounts");
  });

  it("enables the Enable-Trading button only when can_arm, and calls onArm on click", async () => {
    apiFetch.mockResolvedValue(READY);
    const onArm = vi.fn();
    render(<SignalCopyReadiness {...props({ armUiEnabled: true, onArm })} />);
    const btn = await screen.findByRole("button", { name: /Enable Trading/i });
    expect(btn).not.toBeDisabled();
    fireEvent.click(btn);
    expect(onArm).toHaveBeenCalledWith(5);
  });

  it("disables Enable-Trading (and shows a contact-support next action) when can_arm is false", async () => {
    apiFetch.mockResolvedValue({
      ...READY, can_arm: false, next_action: "request_access",
      checklist: READY.checklist.map((c) => (c.key === "pilot_access" ? { ...c, ok: false } : c)),
    });
    const onArm = vi.fn();
    render(<SignalCopyReadiness {...props({ armUiEnabled: true, onArm })} />);
    const btn = await screen.findByRole("button", { name: /Enable Trading/i });
    expect(btn).toBeDisabled();
    expect(screen.getByText(/contact support to request access/i)).toBeInTheDocument();
    fireEvent.click(btn);
    expect(onArm).not.toHaveBeenCalled();
  });

  it("shows a neutral 'unavailable' with a Retry action (never a false 'not ready' dead end) when the fetch fails", async () => {
    apiFetch.mockRejectedValue(new Error("boom"));
    render(<SignalCopyReadiness {...props({ armUiEnabled: true })} />);
    await waitFor(() =>
      expect(screen.getByText(/couldn't check your account status/i)).toBeInTheDocument());
    // P0.2: the failed state offers a next action (retry), not just status text.
    const retry = await screen.findByRole("button", { name: /try again/i });
    const before = apiFetch.mock.calls.length;
    fireEvent.click(retry);
    await waitFor(() => expect(apiFetch.mock.calls.length).toBeGreaterThan(before));
  });
});

// ─────────────────────────────────────────────────────────────────────────────────────────────────────────
// AJ#6.5 — the hosted forward path (Option B). For a hosted-ready customer the Wayond card OWNS the ONE next
// action (authorize → arm) and NEVER bounces back to /onboarding/hosted (the reciprocal loop). armUiEnabled is
// FALSE here on purpose — it mirrors production (NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED off), the exact
// condition that produced the loop. The concepts stay distinct: customer authorization ("Enable automated
// trading") ≠ strategy arm ("Enable this strategy"), and neither control can perform the other's action.
// ─────────────────────────────────────────────────────────────────────────────────────────────────────────

// CASE A backing readiness for an EXECUTION_READY-but-UNAUTHORIZED hosted account: the hosted readiness gate
// fails closed (execution not authorized) → runtime_ready ✕, can_arm false, next_action "preparing" (which the
// pre-AJ6.5 card mapped to /onboarding/hosted — the loop).
const CASE_A_READINESS = {
  state: "PREPARING", armed: false, enabled: false, can_arm: false, account_id: 5,
  next_action: "preparing",
  checklist: [
    { key: "demo", ok: true }, { key: "active", ok: true }, { key: "credentials", ok: true },
    { key: "runtime_ready", ok: false }, { key: "pilot_access", ok: true },
  ],
};

function noBounce() {
  // The reciprocal loop leg: a link to the hosted onboarding page. It must NEVER appear for a hosted-ready
  // customer, in either case.
  return document.querySelector('a[href="/onboarding/hosted"]');
}

describe("SignalCopyReadiness (AJ#6.5 — hosted forward path, no reciprocal loop)", () => {
  beforeEach(() => { apiFetch.mockReset(); });

  it("CASE A (ready, NOT authorized): shows 'Enable automated trading', never bounces to onboarding, and calls onAuthorize (never onArm)", async () => {
    apiFetch.mockResolvedValue(CASE_A_READINESS);
    const onAuthorize = vi.fn();
    const onArm = vi.fn();
    render(<SignalCopyReadiness {...props({
      armUiEnabled: false, onAuthorize, onArm,
      hostedComplete: true, canEnableAutomatedTrading: true, hostedAuthorized: false,
    })} />);
    const btn = await screen.findByRole("button", { name: /Enable automated trading/i });
    // The card OWNS the authorization step here — no bounce, and the arm is not offered yet (separation).
    expect(noBounce()).toBeNull();
    expect(screen.queryByRole("button", { name: /Enable this strategy/i })).toBeNull();
    fireEvent.click(btn);
    expect(onAuthorize).toHaveBeenCalledTimes(1);
    expect(onArm).not.toHaveBeenCalled();
  });

  it("CASE B (ready, authorized) with armUiEnabled OFF: shows the live 'Enable this strategy' arm, never bounces, and calls onArm (never onAuthorize)", async () => {
    apiFetch.mockResolvedValue(READY);
    const onAuthorize = vi.fn();
    const onArm = vi.fn();
    render(<SignalCopyReadiness {...props({
      armUiEnabled: false, onAuthorize, onArm,
      hostedComplete: true, canEnableAutomatedTrading: false, hostedAuthorized: true,
    })} />);
    const btn = await screen.findByRole("button", { name: /Enable this strategy/i });
    expect(btn).not.toBeDisabled();
    // The loop leg is gone even though the broker-connectivity build flag is OFF (the prod condition).
    expect(noBounce()).toBeNull();
    // The authorize step is NOT re-offered once authorized (no manufactured second authorization).
    expect(screen.queryByRole("button", { name: /Enable automated trading/i })).toBeNull();
    fireEvent.click(btn);
    expect(onArm).toHaveBeenCalledWith(5);
    expect(onAuthorize).not.toHaveBeenCalled();
  });

  it("CASE B but a transient arm gate (can_arm false): shows the goal button DISABLED and still never bounces", async () => {
    apiFetch.mockResolvedValue({ ...READY, can_arm: false, next_action: "attention_validation" });
    const onArm = vi.fn();
    render(<SignalCopyReadiness {...props({
      armUiEnabled: false, onArm,
      hostedComplete: true, canEnableAutomatedTrading: false, hostedAuthorized: true,
    })} />);
    const btn = await screen.findByRole("button", { name: /Enable this strategy/i });
    expect(btn).toBeDisabled();
    expect(noBounce()).toBeNull();
    fireEvent.click(btn);
    expect(onArm).not.toHaveBeenCalled();
  });

  it("WAITING (onboarding-complete but NOT yet EXECUTION_READY): shows the preparing reassurance and NEVER bounces (the band that reintroduced the loop)", async () => {
    // This is the exact confirmed-HIGH scenario: strategy_eligible (WORKSPACE_READY) so onboarding's
    // "Choose Strategy" sends the customer here, but the workspace is CONNECTED-not-EXECUTION_READY
    // (AutoTrading off / market closed) → execution not authorizable yet, not authorized. can_arm=false,
    // next_action="preparing" (which the pre-AJ6.5 legacy branch mapped to /onboarding/hosted → loop).
    apiFetch.mockResolvedValue(CASE_A_READINESS);
    const onArm = vi.fn();
    const onAuthorize = vi.fn();
    render(<SignalCopyReadiness {...props({
      armUiEnabled: false, onArm, onAuthorize,
      hostedComplete: true, canEnableAutomatedTrading: false, hostedAuthorized: false,
    })} />);
    // The reassurance line appears and the card OWNS the state — no bounce back to onboarding.
    await waitFor(() => expect(screen.getByText(/getting ready for automated trading/i)).toBeInTheDocument());
    expect(noBounce()).toBeNull();
    // A disabled goal control, never a live authorize/arm and never a navigation dead-end.
    const btn = screen.getByRole("button", { name: /Enable automated trading/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onAuthorize).not.toHaveBeenCalled();
    expect(onArm).not.toHaveBeenCalled();
  });

  it("owned but with an ACTIONABLE non-loop state (e.g. inactive account) keeps its live fix (→ /accounts), NOT the masking 'no action needed'", async () => {
    // Regression guard for the re-review MEDIUM: caseWaiting must suppress ONLY a /onboarding/hosted bounce.
    // An onboarding-complete customer whose account went inactive has next_action="activate_account" → /accounts
    // (a legitimate fix, not the loop). The card must surface that live nav, never the "getting ready" mask.
    apiFetch.mockResolvedValue({
      state: "SETUP_INCOMPLETE", armed: false, enabled: false, can_arm: false, account_id: 5,
      next_action: "activate_account",
      checklist: [
        { key: "demo", ok: true }, { key: "active", ok: false }, { key: "credentials", ok: true },
        { key: "runtime_ready", ok: false }, { key: "pilot_access", ok: true },
      ],
    });
    const onArm = vi.fn();
    const onAuthorize = vi.fn();
    render(<SignalCopyReadiness {...props({
      armUiEnabled: false, onArm, onAuthorize,
      hostedComplete: true, canEnableAutomatedTrading: false, hostedAuthorized: false,
    })} />);
    // The live navigation fix is preserved (→ /accounts, "Continue setup") and the reassurance mask is NOT shown …
    const link = await screen.findByRole("link", { name: /continue setup/i });
    expect(link).toHaveAttribute("href", "/accounts");
    expect(screen.queryByText(/getting ready for automated trading/i)).toBeNull();
    // … and it still never bounces to onboarding, nor authorizes/arms on its own.
    expect(noBounce()).toBeNull();
    expect(onArm).not.toHaveBeenCalled();
    expect(onAuthorize).not.toHaveBeenCalled();
  });

  it("owned defence-in-depth: even a can_arm selected account with the arm UI DARK never bounces an onboarding-complete customer to onboarding", async () => {
    // The multi-account edge the re-verify flagged: owned (strategy_eligible) + not-authorized/-authorizable
    // (caseWaiting) while the SELECTED account's readiness is can_arm (a ready demo, next_action=ready_enable,
    // not in NEXT_NAV). Legacy would render <Link /onboarding/hosted>; owned must instead show a disabled goal.
    apiFetch.mockResolvedValue(READY);
    const onArm = vi.fn();
    render(<SignalCopyReadiness {...props({
      armUiEnabled: false, onArm,
      hostedComplete: true, canEnableAutomatedTrading: false, hostedAuthorized: false,
    })} />);
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    expect(noBounce()).toBeNull();
    // No live arm (arm UI dark) — a disabled goal button, and onArm is never called.
    const btn = await screen.findByRole("button", { name: /Enable Trading/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onArm).not.toHaveBeenCalled();
  });

  it("authorization transition (CASE A → CASE B): re-reads readiness when authorization flips, swapping authorize → arm", async () => {
    // First fetch (mount) = CASE A; the re-fetch after authorization = READY.
    apiFetch.mockResolvedValueOnce(CASE_A_READINESS).mockResolvedValue(READY);
    const base = props({
      armUiEnabled: false,
      hostedComplete: true, canEnableAutomatedTrading: true, hostedAuthorized: false,
    });
    const { rerender } = render(<SignalCopyReadiness {...base} />);
    await screen.findByRole("button", { name: /Enable automated trading/i });
    const fetchesBefore = apiFetch.mock.calls.length;

    // The customer authorized (page re-read the journey) → authorized=true, can_enable=false.
    rerender(<SignalCopyReadiness {...base}
      canEnableAutomatedTrading={false} hostedAuthorized={true} />);
    // The authorization change triggers a readiness re-fetch (can_arm flips), then the arm control appears.
    await waitFor(() => expect(apiFetch.mock.calls.length).toBeGreaterThan(fetchesBefore));
    await screen.findByRole("button", { name: /Enable this strategy/i });
    expect(noBounce()).toBeNull();
    expect(screen.queryByRole("button", { name: /Enable automated trading/i })).toBeNull();
  });

  it("non-hosted caller is UNCHANGED: with no hosted context the legacy DARK-build bounce still renders", async () => {
    // Regression guard: the fix is scoped to hosted-ready customers. A caller with no hosted journey keeps the
    // exact pre-AJ6.5 behaviour (this is not the reciprocal loop — a non-hosted customer has no ready workspace).
    apiFetch.mockResolvedValue(READY);
    render(<SignalCopyReadiness {...props({ armUiEnabled: false })} />);
    const link = await screen.findByRole("link", { name: "Continue" });
    expect(link).toHaveAttribute("href", "/onboarding/hosted");
    expect(screen.queryByRole("button", { name: /Enable automated trading/i })).toBeNull();
  });
});

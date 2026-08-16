import { beforeEach, describe, expect, it, vi } from "vitest";

const apiMock = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => apiMock);

import {
  describeJourney, fetchJourney, requestWorkspace, bindExpectedAccount, confirmAccount, STEPS,
  type HostedJourney, type JourneyPhase, type NextAction,
} from "@/lib/hosted-journey";

function journey(over: Partial<HostedJourney> = {}): HostedJourney {
  return {
    phase: "WORKSPACE_READY", next_action: "assign_strategy", confirmed: true,
    strategy_eligible: true, delivery: "DELIVERY_READY", active_login_masked: "***561",
    identity_declared: true, ...over,
  };
}

// Every internal identifier that must NEVER reach a customer.
const LEAKS = ["canonical", "PROVISIONING", "WAITING_FOR_LOGIN", "execution_node", "workspace_node",
  "rdp_host", "guvfx_u_", "AUTO_SHADOW", "proj_"];

describe("describeJourney — customer state machine", () => {
  const cases: Array<[JourneyPhase, NextAction, number, string]> = [
    ["NO_WORKSPACE", "request_workspace", 0, "action"],
    ["WORKSPACE_REQUESTED", "wait", 1, "progress"],
    ["WORKSPACE_PREPARING", "wait", 1, "progress"],
    ["AWAITING_BROKER_LOGIN", "open_mt5_and_log_in", 2, "action"],
    ["BROKER_CONNECTED", "open_mt5_and_log_in", 2, "action"],
    ["ACCOUNT_CONFIRMATION_REQUIRED", "confirm_broker_account", 3, "action"],
    ["ACCOUNT_BOUND", "wait", 3, "progress"],
    ["WORKSPACE_READY", "assign_strategy", 4, "ready"],
    ["WORKSPACE_UNAVAILABLE", "contact_support", -1, "error"],
  ];

  it.each(cases)("phase %s → step %s tone %s (deterministic)", (phase, next_action, step, tone) => {
    const v = describeJourney(journey({ phase, next_action }));
    expect(v.stepIndex).toBe(step);
    expect(v.tone).toBe(tone);
    expect(v.title).toBeTruthy();
    expect(v.description).toBeTruthy();
  });

  it("maps next_action to the correct primary action button", () => {
    expect(describeJourney(journey({ phase: "NO_WORKSPACE", next_action: "request_workspace" })).action?.kind).toBe("request");
    expect(describeJourney(journey({ phase: "AWAITING_BROKER_LOGIN", next_action: "open_mt5_and_log_in" })).action?.kind).toBe("launch");
    expect(describeJourney(journey({ phase: "ACCOUNT_CONFIRMATION_REQUIRED", next_action: "confirm_broker_account" })).action?.kind).toBe("confirm");
    expect(describeJourney(journey({ phase: "WORKSPACE_READY", next_action: "assign_strategy" })).action?.kind).toBe("assign");
    expect(describeJourney(journey({ phase: "WORKSPACE_UNAVAILABLE", next_action: "contact_support" })).action?.kind).toBe("support");
  });

  it("wait next_action shows progress with no action button", () => {
    expect(describeJourney(journey({ phase: "WORKSPACE_PREPARING", next_action: "wait" })).action).toBeNull();
  });

  it("canLaunch is true when delivery is READY or DELIVERABLE (BB#1: openable before CONNECTED)", () => {
    expect(describeJourney(journey({ delivery: "DELIVERY_READY" })).canLaunch).toBe(true);
    // BB#1: DELIVERABLE (authority proved it openable) enables the live launch BEFORE any CONNECTED — the
    // customer's click is what creates the session. This breaks the button⇄CONNECTED deadlock.
    expect(describeJourney(journey({ delivery: "DELIVERY_DELIVERABLE" })).canLaunch).toBe(true);
    expect(describeJourney(journey({ delivery: "DELIVERY_PREPARING" })).canLaunch).toBe(false);
    expect(describeJourney(journey({ delivery: "DELIVERY_NOT_AVAILABLE" })).canLaunch).toBe(false);
    expect(describeJourney(journey({ delivery: "DELIVERY_EXTERNAL_GATE" })).canLaunch).toBe(false);
  });

  it("null journey → the 'request workspace' start view (fail-safe, no crash)", () => {
    const v = describeJourney(null);
    expect(v.stepIndex).toBe(0);
    expect(v.action?.kind).toBe("request");
  });

  it("unknown/garbled phase → fail-closed support fallback (never a leak, never a crash)", () => {
    // deliberately malformed payload
    const v = describeJourney({ phase: "NONSENSE" } as unknown as HostedJourney);
    expect(v.tone).toBe("error");
    expect(v.action?.kind).toBe("support");
  });

  it("shows the masked login hint but never a full login", () => {
    const v = describeJourney(journey({ phase: "ACCOUNT_CONFIRMATION_REQUIRED", next_action: "confirm_broker_account", active_login_masked: "***561" }));
    expect(v.description).toContain("***561");
    expect(v.description).not.toContain("1302561");
  });

  it("NEVER leaks internal identifiers in any customer copy", () => {
    for (const [phase, next_action] of cases) {
      const v = describeJourney(journey({ phase, next_action }));
      const blob = `${v.title} ${v.description} ${v.action?.label ?? ""}`.toLowerCase();
      for (const bad of LEAKS) expect(blob).not.toContain(bad.toLowerCase());
    }
  });

  it("STEPS is the fixed 5-step customer stepper", () => {
    expect(STEPS).toHaveLength(5);
  });
});

describe("journey API wrappers", () => {
  beforeEach(() => apiMock.apiFetch.mockReset());

  it("fetchJourney returns the journey on success", async () => {
    apiMock.apiFetch.mockResolvedValue(journey({ phase: "WORKSPACE_PREPARING" }));
    const r = await fetchJourney();
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.journey.phase).toBe("WORKSPACE_PREPARING");
  });

  it("fetchJourney maps a 404 to unavailable (feature dark / not admitted), never a dead end", async () => {
    apiMock.apiFetch.mockRejectedValueOnce(Object.assign(new Error("Not found"), { status: 404 }));
    const r = await fetchJourney();
    expect(r).toEqual({ ok: false, unavailable: true });
  });

  it("fetchJourney rethrows non-404 errors so the page can retry", async () => {
    apiMock.apiFetch.mockRejectedValueOnce(Object.assign(new Error("boom"), { status: 503 }));
    await expect(fetchJourney()).rejects.toThrow();
  });

  it("requestWorkspace posts NO broker identity (deferred bind) and never a password", async () => {
    apiMock.apiFetch.mockResolvedValue(journey());
    await requestWorkspace();
    const [path, opts] = apiMock.apiFetch.mock.calls[0];
    expect(path).toContain("/onboarding/request/");
    const body = JSON.parse((opts as RequestInit).body as string);
    expect(body).toEqual({});
    expect(JSON.stringify(body).toLowerCase()).not.toContain("password");
  });

  it("bindExpectedAccount posts trimmed login/server to /onboarding/bind/ (never a password)", async () => {
    apiMock.apiFetch.mockResolvedValue(journey());
    await bindExpectedAccount({ expected_login: " 700900 ", expected_server: " IS6-Demo " });
    const [path, opts] = apiMock.apiFetch.mock.calls[0];
    expect(path).toContain("/onboarding/bind/");
    const body = JSON.parse((opts as RequestInit).body as string);
    expect(body).toEqual({ expected_login: "700900", expected_server: "IS6-Demo" });
    expect(JSON.stringify(body).toLowerCase()).not.toContain("password");
  });

  it("confirmAccount POSTs with no body", async () => {
    apiMock.apiFetch.mockResolvedValue(journey());
    await confirmAccount();
    const [path, opts] = apiMock.apiFetch.mock.calls[0];
    expect(path).toContain("/onboarding/confirm/");
    expect((opts as RequestInit).method).toBe("POST");
  });
});

import { beforeEach, describe, expect, it, vi } from "vitest";

// AJ#7 — unit tests for the strategy-journey contract + orchestration layer. Proves: the Configure contract
// is honest (no cosmetic editable controls the execution path ignores); Get never arms/authorizes; Enable
// orchestrates ADR-0047 authorize → strategy arm correctly and is idempotent + retryable on partial failure.

const { apiFetch, fetchJourney, authorizeExecution } = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  fetchJourney: vi.fn(),
  authorizeExecution: vi.fn(),
}));

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("@/lib/hosted-journey", () => ({ fetchJourney, authorizeExecution }));

import {
  priceLabel,
  priceFor,
  isAutomated,
  configContract,
  deriveOwnedState,
  getStrategy,
  enableStrategy,
  disableStrategy,
} from "./strategy-journey";

const armPath = "/api/strategies/strategies/signal-copy/arm/";
const getPath = "/api/strategies/strategies/signal-copy/get/";
const togglePath = "/api/strategies/strategies/signal-copy/toggle/";

beforeEach(() => {
  apiFetch.mockReset();
  fetchJourney.mockReset();
  authorizeExecution.mockReset();
});

describe("commercial model", () => {
  it("beta strategies are Free and Wayond is the automated one", () => {
    expect(priceLabel(priceFor("mp-010"))).toBe("Free");
    expect(priceLabel(priceFor("mp-001"))).toBe("Free");
    expect(isAutomated("mp-010")).toBe(true);
    expect(isAutomated("mp-001")).toBe(false);
  });
  it("price labels are commercially extensible without changing the CTA slot", () => {
    expect(priceLabel({ kind: "monthly", amount: "£29" })).toBe("£29/month");
    expect(priceLabel({ kind: "oneTime", amount: "£99" })).toBe("£99 one-time");
    expect(priceLabel({ kind: "included" })).toBe("Included with plan");
  });
});

describe("honest Configure contract", () => {
  it("Wayond exposes ONLY account as customer-selectable; sizing/TP/SL/trailing are managed (read-only)", () => {
    const rows = configContract("mp-010", "Demo A (123)");
    const byKey = Object.fromEntries(rows.map((r) => [r.key, r]));
    // The single genuinely customer-selectable input.
    expect(byKey.account.kind).toBe("account");
    expect(byKey.account.value).toBe("Demo A (123)");
    // The deferred per-customer knobs are present but MANAGED (never editable) — no cosmetic controls.
    for (const k of ["sizing", "takeprofit", "stoploss", "trailing"]) {
      expect(byKey[k].kind).toBe("managed");
    }
    // No row is anything other than account/managed/info — there is no editable "control" kind in AJ#7.
    for (const r of rows) expect(["account", "managed", "info"]).toContain(r.kind);
  });
  it("non-signal-copy strategies have no automated contract", () => {
    expect(configContract("mp-001", "Demo A")).toEqual([]);
  });
});

describe("deriveOwnedState (customer lifecycle, no internal terms)", () => {
  const base = { owned: false, enabled: false, ambiguous: false, canArm: false, journeyReady: false };
  it("maps each state", () => {
    expect(deriveOwnedState({ ...base }).state).toBe("not_owned");
    expect(deriveOwnedState({ ...base, owned: true }).state).toBe("owned_setup_required");
    expect(deriveOwnedState({ ...base, owned: true, canArm: true, journeyReady: true }).state).toBe("ready_to_enable");
    expect(deriveOwnedState({ ...base, owned: true, enabled: true }).state).toBe("enabled");
    expect(deriveOwnedState({ ...base, owned: true, ambiguous: true }).state).toBe("needs_attention");
  });
  it("ambiguous always wins (fail-closed to needs attention)", () => {
    expect(deriveOwnedState({ ...base, owned: true, enabled: true, ambiguous: true }).state).toBe("needs_attention");
  });
});

describe("getStrategy — acquire WITHOUT enabling execution", () => {
  it("POSTs signal-copy/get and NEVER arms/authorizes", async () => {
    apiFetch.mockResolvedValueOnce({ status: "owned", assignment_id: 7, enabled: false });
    const res = await getStrategy("mp-010", 5);
    expect(res.assignment_id).toBe(7);
    expect(apiFetch).toHaveBeenCalledTimes(1);
    expect(apiFetch).toHaveBeenCalledWith(getPath, expect.objectContaining({ method: "POST" }));
    // No arm, no authorize.
    expect(authorizeExecution).not.toHaveBeenCalled();
    expect(apiFetch.mock.calls.some((c) => String(c[0]) === armPath)).toBe(false);
  });
});

describe("enableStrategy — ADR-0047 authorize → strategy arm orchestration", () => {
  it("authorizes THEN arms when the workspace is ready-but-unauthorized", async () => {
    fetchJourney.mockResolvedValueOnce({ ok: true, journey: { can_enable_automated_trading: true } });
    authorizeExecution.mockResolvedValueOnce({});
    apiFetch.mockResolvedValueOnce({ status: "armed", enabled: true }); // arm
    const res = await enableStrategy("mp-010", 5);
    expect(res.ok).toBe(true);
    expect(authorizeExecution).toHaveBeenCalledTimes(1);
    // Exactly one arm POST (idempotent, no duplicate).
    const armCalls = apiFetch.mock.calls.filter((c) => String(c[0]) === armPath);
    expect(armCalls).toHaveLength(1);
    expect(JSON.parse(armCalls[0][1].body)).toEqual({ marketplace_strategy_id: "mp-010", account_id: 5 });
  });

  it("does NOT re-authorize when the workspace is already authorized — only arms", async () => {
    fetchJourney.mockResolvedValueOnce({ ok: true, journey: { execution_authorized: true, can_enable_automated_trading: false } });
    apiFetch.mockResolvedValueOnce({ status: "armed", enabled: true });
    const res = await enableStrategy("mp-010", 5);
    expect(res.ok).toBe(true);
    expect(authorizeExecution).not.toHaveBeenCalled();
    expect(apiFetch.mock.calls.filter((c) => String(c[0]) === armPath)).toHaveLength(1);
  });

  it("partial failure (authorize OK, arm fails) → retryable, customer-safe, no duplicate", async () => {
    fetchJourney.mockResolvedValue({ ok: true, journey: { can_enable_automated_trading: true } });
    authorizeExecution.mockResolvedValue({});
    const armErr = Object.assign(new Error("nope"), { httpStatus: 409, body: { status: "runtime_not_ready" } });
    apiFetch.mockRejectedValueOnce(armErr);
    const res = await enableStrategy("mp-010", 5);
    expect(res.ok).toBe(false);
    if (!res.ok) {
      expect(res.stage).toBe("arm");
      expect(res.code).toBe("runtime_not_ready");
      expect(res.message).not.toMatch(/runtime_not_ready/); // never leaks the raw slug
      expect(res.message.length).toBeGreaterThan(0);
    }
    // A retry re-runs authorize(idempotent) + a single arm — no duplicate assignment on the FE side.
    apiFetch.mockResolvedValueOnce({ status: "armed", enabled: true });
    const retry = await enableStrategy("mp-010", 5);
    expect(retry.ok).toBe(true);
    expect(apiFetch.mock.calls.filter((c) => String(c[0]) === armPath)).toHaveLength(2); // one per attempt
  });

  it("authorization failure is reported at the authorize stage (arm never runs)", async () => {
    fetchJourney.mockResolvedValueOnce({ ok: true, journey: { can_enable_automated_trading: true } });
    authorizeExecution.mockRejectedValueOnce(Object.assign(new Error("unauthorized"), { status: 401 }));
    const res = await enableStrategy("mp-010", 5);
    expect(res.ok).toBe(false);
    if (!res.ok) expect(res.stage).toBe("authorize");
    expect(apiFetch.mock.calls.some((c) => String(c[0]) === armPath)).toBe(false);
  });

  it("still attempts arm (backend fail-closes) when the workspace journey is not ready", async () => {
    fetchJourney.mockResolvedValueOnce({ ok: false, unavailable: true });
    const armErr = Object.assign(new Error("nope"), { httpStatus: 409, body: { status: "workspace_execution_not_authorized" } });
    apiFetch.mockRejectedValueOnce(armErr);
    const res = await enableStrategy("mp-010", 5);
    expect(authorizeExecution).not.toHaveBeenCalled(); // no journey → can't authorize; the backend gate decides
    expect(res.ok).toBe(false);
  });
});

describe("disableStrategy — pause execution", () => {
  it("POSTs toggle enabled:false", async () => {
    apiFetch.mockResolvedValueOnce({ status: "disabled", enabled: false });
    await disableStrategy("mp-010", 5);
    expect(apiFetch).toHaveBeenCalledWith(togglePath, expect.objectContaining({ method: "POST" }));
    expect(JSON.parse(apiFetch.mock.calls[0][1].body)).toEqual({ marketplace_strategy_id: "mp-010", account_id: 5, enabled: false });
  });
});

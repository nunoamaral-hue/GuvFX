import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

import LotSizeControl from "@/components/strategy/LotSizeControl";

const apiFetch = vi.fn();
vi.mock("@/lib/api", () => ({ apiFetch: (...a: unknown[]) => apiFetch(...a) }));

const sizing = {
  ok: true, assignment_id: 42, account_id: 1, signal_source: "ti_signals",
  lot_per_leg: "0.01", is_override: true, version: 1, default_lot_per_leg: "0.01",
  min: "0.01", step: "0.01", max: "0.40", source_cap: "0.40", max_legs: 3,
  applies_to_live_execution: true,
};

beforeEach(() => { apiFetch.mockReset(); });

describe("LotSizeControl", () => {
  it("shows the saved per-leg value and the transparent max total (0.01 x 3 = 0.03)", async () => {
    apiFetch.mockResolvedValueOnce(sizing);
    render(<LotSizeControl assignmentId={42} lang="en" />);
    const input = (await screen.findByLabelText("Lot size per trade")) as HTMLInputElement;
    expect(input.value).toBe("0.01");
    // per-position wording (not "total") + derived maximum
    expect(screen.getByText(/EACH position/i)).toBeTruthy();
    expect(screen.getByText(/0\.03 lots total/i)).toBeTruthy();
  });

  it("saves an edited value via PUT and confirms success", async () => {
    apiFetch.mockResolvedValueOnce(sizing);                                   // initial GET
    apiFetch.mockResolvedValueOnce({ ...sizing, lot_per_leg: "0.02", version: 2 }); // PUT
    render(<LotSizeControl assignmentId={42} lang="en" />);
    const input = (await screen.findByLabelText("Lot size per trade")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "0.02" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(screen.getByText("Saved")).toBeTruthy());
    const putCall = apiFetch.mock.calls.find((c) => (c[1] as RequestInit)?.method === "PUT");
    expect(putCall?.[0]).toBe("/api/strategies/assignments/42/leg-sizing/");
    expect(JSON.parse((putCall?.[1] as RequestInit).body as string)).toEqual({ lot_per_leg: "0.02" });
  });

  it("surfaces a customer-safe server validation error", async () => {
    apiFetch.mockResolvedValueOnce(sizing);
    const err = Object.assign(new Error("bad"), { body: { ok: false, errors: { lot_per_leg: ["must be at most 0.40 for this strategy"] } } });
    apiFetch.mockRejectedValueOnce(err);
    render(<LotSizeControl assignmentId={42} lang="en" />);
    const input = (await screen.findByLabelText("Lot size per trade")) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "0.99" } });
    fireEvent.click(screen.getByText("Save"));
    await waitFor(() => expect(screen.getByText(/at most 0\.40/i)).toBeTruthy());
  });

  it("renders the persisted backend value, not a hardcoded constant (P0-A)", async () => {
    // Guard: if the backend ever returns the source-cap fallback (0.40, e.g. a pre-seed assignment),
    // the control must display THAT persisted value — never assume the 0.01 default.
    apiFetch.mockResolvedValueOnce({ ...sizing, lot_per_leg: "0.40", is_override: false });
    render(<LotSizeControl assignmentId={42} lang="en" />);
    const input = (await screen.findByLabelText("Lot size per trade")) as HTMLInputElement;
    expect(input.value).toBe("0.40");
    expect(input.value).not.toBe("0.01");
  });

  it("renders nothing when there is no assignment", () => {
    const { container } = render(<LotSizeControl assignmentId={null} lang="en" />);
    expect(container.textContent).toBe("");
  });
});

import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import {
  EMPTY_FILTERS, OpsFilters, applyClientFilters, type OpsFilterState,
} from "@/components/operations/OpsFilters";
import type { OperationalEvent } from "@/types/operations";

function ev(over: Partial<OperationalEvent> = {}): OperationalEvent {
  return {
    id: 1, timestamp: "2026-08-01T10:00:00Z", account_id: 7, runtime_uuid: "rt",
    category: "HEALTH", event_type: "health_degraded", severity: "WARNING", status: "OPEN",
    reason_code: "heartbeat_stale", summary: "Broker heartbeat is stale", source: "health_engine",
    correlation_id: "c", state_version: 1, actor: "system", customer_visible: true,
    resolved: false, resolved_at: null, metadata: {}, ...over,
  };
}
const f = (over: Partial<OpsFilterState> = {}): OpsFilterState => ({ ...EMPTY_FILTERS, ...over });

describe("applyClientFilters", () => {
  // Timestamps are built at LOCAL midnight (via new Date(y, m, d)) so the date-range assertions are
  // TZ-agnostic — they pass under any CI timezone, matching the local-basis day boundaries in the impl.
  const events = [
    ev({ id: 1, severity: "INFO", resolved: true, customer_visible: true, timestamp: new Date(2026, 6, 1).toISOString(), summary: "login ok", reason_code: "login_ok" }),
    ev({ id: 2, severity: "WARNING", resolved: false, customer_visible: false, timestamp: new Date(2026, 7, 1).toISOString(), summary: "heartbeat stale", reason_code: "heartbeat_stale" }),
    ev({ id: 3, severity: "ERROR", resolved: false, customer_visible: true, timestamp: new Date(2026, 7, 15).toISOString(), summary: "dispatch failed", reason_code: "dispatch_failed" }),
  ];

  it("returns everything with empty filters", () => {
    expect(applyClientFilters(events, EMPTY_FILTERS).map((e) => e.id)).toEqual([1, 2, 3]);
  });
  it("filters by severity", () => {
    expect(applyClientFilters(events, f({ severity: "ERROR" })).map((e) => e.id)).toEqual([3]);
  });
  it("filters by resolution open/resolved", () => {
    expect(applyClientFilters(events, f({ resolution: "open" })).map((e) => e.id)).toEqual([2, 3]);
    expect(applyClientFilters(events, f({ resolution: "resolved" })).map((e) => e.id)).toEqual([1]);
  });
  it("filters by visibility", () => {
    expect(applyClientFilters(events, f({ visibility: "operator" })).map((e) => e.id)).toEqual([2]);
    expect(applyClientFilters(events, f({ visibility: "customer" })).map((e) => e.id)).toEqual([1, 3]);
  });
  it("filters by inclusive date range (local end-of-day), TZ-agnostic", () => {
    expect(applyClientFilters(events, f({ from: "2026-08-01", to: "2026-08-01" })).map((e) => e.id)).toEqual([2]);
    expect(applyClientFilters(events, f({ from: "2026-08-01" })).map((e) => e.id)).toEqual([2, 3]);
    // Regression for the UTC-boundary bug: a late-evening LOCAL event stays on its local day regardless
    // of the viewer's TZ offset (would fail under the old Date.parse("YYYY-MM-DD") UTC boundary).
    const lateAug1 = ev({ id: 9, timestamp: new Date(2026, 7, 1, 23, 30).toISOString() });
    expect(applyClientFilters([lateAug1], f({ to: "2026-08-01" }))).toHaveLength(1);   // same local day → kept
    expect(applyClientFilters([lateAug1], f({ to: "2026-07-31" }))).toHaveLength(0);   // prior day → dropped
    expect(applyClientFilters([lateAug1], f({ from: "2026-08-02" }))).toHaveLength(0); // next day → dropped
  });
  it("searches across summary/reason/source/type/category", () => {
    expect(applyClientFilters(events, f({ search: "heartbeat" })).map((e) => e.id)).toEqual([2]);
    expect(applyClientFilters(events, f({ search: "HEALTH" })).map((e) => e.id)).toEqual([1, 2, 3]); // category match
    expect(applyClientFilters(events, f({ search: "nothing-here" }))).toHaveLength(0);
  });
  it("does NOT filter by category (that is server-side only)", () => {
    // category in the filter state is ignored client-side.
    expect(applyClientFilters(events, f({ category: "EXECUTION" })).map((e) => e.id)).toEqual([1, 2, 3]);
  });
});

describe("OpsFilters bar", () => {
  it("is a labelled search region and reports changes", () => {
    const onChange = vi.fn();
    render(<OpsFilters value={EMPTY_FILTERS} onChange={onChange} />);
    expect(screen.getByRole("search", { name: /filter operational events/i })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Severity"), { target: { value: "ERROR" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ severity: "ERROR" }));
  });
  it("Clear resets to EMPTY_FILTERS", () => {
    const onChange = vi.fn();
    render(<OpsFilters value={f({ severity: "ERROR", search: "x" })} onChange={onChange} />);
    fireEvent.click(screen.getByText("Clear"));
    expect(onChange).toHaveBeenCalledWith(EMPTY_FILTERS);
  });
});

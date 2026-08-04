import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { OpsTimelineTable } from "@/components/operations/OpsTimelineTable";
import type { OperationalEvent } from "@/types/operations";

function ev(over: Partial<OperationalEvent> = {}): OperationalEvent {
  return {
    id: 1, timestamp: "2026-08-01T10:00:00Z", account_id: 7, runtime_uuid: "rt-xyz",
    category: "HEALTH", event_type: "health_degraded", severity: "WARNING", status: "OPEN",
    reason_code: "heartbeat_stale", summary: "Broker heartbeat is stale", source: "health_engine",
    correlation_id: "corr-1", state_version: 3, actor: "system", customer_visible: true,
    resolved: false, resolved_at: null, metadata: {}, ...over,
  };
}

describe("OpsTimelineTable", () => {
  it("shows an empty state when there are no events", () => {
    render(<OpsTimelineTable events={[]} />);
    expect(screen.getByText("No events")).toBeInTheDocument();
  });

  it("renders a row per event with mapped severity/category/status", () => {
    render(<OpsTimelineTable events={[ev({ id: 1 }), ev({ id: 2, summary: "Second event" })]} />);
    expect(screen.getByText("Broker heartbeat is stale")).toBeInTheDocument();
    expect(screen.getByText("Second event")).toBeInTheDocument();
    expect(screen.getAllByText("Warning").length).toBe(2); // severity mapped, not "WARNING"
    expect(screen.queryByText("WARNING")).toBeNull();
    expect(screen.getAllByText("Open").length).toBe(2); // resolution mapped
  });

  it("invokes onSelect on click and on keyboard Enter", () => {
    const onSelect = vi.fn();
    render(<OpsTimelineTable events={[ev({ id: 42 })]} onSelect={onSelect} />);
    // rows are focusable <tr> with an aria-label; query by label.
    const target = screen.getByLabelText(/View event:/i);
    expect(target).toHaveAttribute("tabindex", "0");
    fireEvent.click(target);
    expect(onSelect).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(target, { key: "Enter" });
    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(onSelect.mock.calls[0][0].id).toBe(42);
  });
});

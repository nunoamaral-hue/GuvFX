import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { EventDetailDialog } from "@/components/operations/EventDetailDialog";
import type { OperationalEvent } from "@/types/operations";

function ev(over: Partial<OperationalEvent> = {}): OperationalEvent {
  return {
    id: 1, timestamp: "2026-08-01T10:00:00Z", account_id: 7, runtime_uuid: "SECRET-RUNTIME-UUID",
    category: "CREDENTIAL", event_type: "credential_replaced", severity: "INFO", status: "RESOLVED",
    reason_code: "operator_replaced", summary: "Credentials were replaced", source: "broker_api",
    correlation_id: "corr-9", state_version: 5, actor: "operator", customer_visible: false,
    resolved: true, resolved_at: "2026-08-01T11:00:00Z",
    // A realistic projection-shaped metadata dict mixing FORBIDDEN internal keys (state_version, raw
    // from/to state enums, phase, job/plan/pause ids, raw status) with SAFE human-facing keys.
    metadata: {
      state_version: 7, from_state: "HEALTHY", to_state: "DISCONNECTED", phase: "dispatch",
      job_id: 1234, plan_id: 24, pause_record_id: 137, resulting_status: "REJECTED",
      is_demo: true, retryable: false, trigger: "credential_replacement",
    },
    ...over,
  };
}

describe("EventDetailDialog", () => {
  it("renders nothing when there is no selected event", () => {
    const { container } = render(<EventDetailDialog event={null} onClose={() => {}} />);
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });

  it("shows the customer-safe projection (summary, reason, allow-listed metadata)", () => {
    render(<EventDetailDialog event={ev()} onClose={() => {}} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Credentials were replaced")).toBeInTheDocument();
    expect(screen.getByText("operator_replaced")).toBeInTheDocument();
    // allow-listed metadata: labelled + human-formatted (booleans -> Yes/No)
    expect(screen.getByText("Demo account")).toBeInTheDocument();
    expect(screen.getByText("Trigger")).toBeInTheDocument();
    expect(screen.getByText("credential_replacement")).toBeInTheDocument();
  });

  it("never renders forbidden internals: raw state enums, version counters, internal ids", () => {
    render(<EventDetailDialog event={ev()} onClose={() => {}} />);
    // top-level internals the view deliberately omits
    expect(screen.queryByText("SECRET-RUNTIME-UUID")).toBeNull(); // runtime_uuid
    expect(screen.queryByText(/RESOLVED\b/)).toBeNull();          // raw top-level status enum
    // forbidden METADATA keys/values must be dropped by the fail-closed allow-list
    expect(screen.queryByText("7")).toBeNull();            // state_version
    expect(screen.queryByText("HEALTHY")).toBeNull();      // from_state (raw enum)
    expect(screen.queryByText("DISCONNECTED")).toBeNull(); // to_state (raw enum)
    expect(screen.queryByText("dispatch")).toBeNull();     // phase
    expect(screen.queryByText("1234")).toBeNull();         // job_id
    expect(screen.queryByText("24")).toBeNull();           // plan_id
    expect(screen.queryByText("137")).toBeNull();          // pause_record_id
    expect(screen.queryByText("REJECTED")).toBeNull();     // resulting_status (raw enum)
    // and the forbidden keys' labels never appear either
    expect(screen.queryByText(/state_version|from_state|to_state|job_id|plan_id/i)).toBeNull();
  });

  it("closes via the close button", () => {
    const onClose = vi.fn();
    render(<EventDetailDialog event={ev()} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });
});

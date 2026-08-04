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
    metadata: { attempt: 2, note: "re-validated" }, ...over,
  };
}

describe("EventDetailDialog", () => {
  it("renders nothing when there is no selected event", () => {
    const { container } = render(<EventDetailDialog event={null} onClose={() => {}} />);
    expect(container.querySelector('[role="dialog"]')).toBeNull();
  });

  it("shows the customer-safe projection (summary, reason, metadata)", () => {
    render(<EventDetailDialog event={ev()} onClose={() => {}} />);
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Credentials were replaced")).toBeInTheDocument();
    expect(screen.getByText("operator_replaced")).toBeInTheDocument();
    expect(screen.getByText("re-validated")).toBeInTheDocument(); // allow-listed metadata value
  });

  it("never renders internal identifiers (runtime uuid / status / state_version)", () => {
    render(<EventDetailDialog event={ev()} onClose={() => {}} />);
    expect(screen.queryByText("SECRET-RUNTIME-UUID")).toBeNull();
    expect(screen.queryByText(/RESOLVED\b/)).toBeNull(); // raw status enum not shown
    expect(screen.queryByText("5")).toBeNull();          // state_version not shown
  });

  it("closes via the close button", () => {
    const onClose = vi.fn();
    render(<EventDetailDialog event={ev()} onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalled();
  });
});

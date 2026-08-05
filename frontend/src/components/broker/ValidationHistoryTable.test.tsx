import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ValidationHistoryTable } from "@/components/broker/ValidationHistoryTable";
import type { ValidationAttempt } from "@/types/broker";

const attempt = (o: Partial<ValidationAttempt>): ValidationAttempt => ({
  id: 1, trigger: "test", status: "HEALTHY", reason_code: "demo_ok", retryable: false, is_demo: true,
  server: "IS6-Demo", login_masked: "***", correlation_id: "c", created_at: "2026-08-04T00:00:00Z", ...o,
});

describe("ValidationHistoryTable", () => {
  it("renders attempts with customer-safe reason wording and never the raw code", () => {
    render(<ValidationHistoryTable attempts={[attempt({ reason_code: "invalid_password", status: "NEEDS_ATTENTION" })]} />);
    expect(screen.getByText(/password was not accepted/i)).toBeInTheDocument();
    expect(screen.queryByText("invalid_password")).toBeNull();
  });

  it("shows an empty state when there are none", () => {
    render(<ValidationHistoryTable attempts={[]} />);
    expect(screen.getByText(/no validation attempts yet/i)).toBeInTheDocument();
  });

  it("customer view hides the correlation ID; a concise outcome is shown", () => {
    render(<ValidationHistoryTable attempts={[attempt({ correlation_id: "validate-acct-13-abc",
      reason_code: "validation_ipc_unavailable", status: "UNAVAILABLE" })]} />);
    expect(screen.queryByText("Correlation ID")).toBeNull();          // header not shown to customers
    expect(screen.queryByText("validate-acct-13-abc")).toBeNull();    // correlation id never shown
    expect(screen.getByText(/couldn't start the validation session/i)).toBeInTheDocument();  // concise outcome
  });

  it("staff view shows the correlation ID column", () => {
    render(<ValidationHistoryTable staff attempts={[attempt({ correlation_id: "validate-acct-13-abc" })]} />);
    expect(screen.getByText("Correlation ID")).toBeInTheDocument();
    expect(screen.getByText("validate-acct-13-abc")).toBeInTheDocument();
    expect(screen.getByText("Verified")).toBeInTheDocument();          // HEALTHY → "Verified" outcome
  });
});

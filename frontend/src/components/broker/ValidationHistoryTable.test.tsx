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
});

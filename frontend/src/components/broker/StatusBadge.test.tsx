import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "@/components/broker/StatusBadge";
import { validationStatusView } from "@/lib/broker-status";

describe("StatusBadge", () => {
  it("renders the mapped label (never a raw backend enum)", () => {
    render(<StatusBadge view={validationStatusView("VALIDATED")} title="Validation status" />);
    expect(screen.getByText("Validated")).toBeInTheDocument();
    expect(screen.queryByText("VALIDATED")).toBeNull();
  });
});

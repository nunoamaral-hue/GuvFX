import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { CategoryBadge, ResolutionBadge, SeverityBadge } from "@/components/operations/OpsBadges";

/** WP5.3 — badges render the mapped label, never the raw backend enum. */
describe("operational badges", () => {
  it("SeverityBadge renders the mapped label, not the enum", () => {
    render(<SeverityBadge severity="CRITICAL" />);
    expect(screen.getByText("Critical")).toBeInTheDocument();
    expect(screen.queryByText("CRITICAL")).toBeNull();
  });

  it("CategoryBadge renders the mapped label, not the enum", () => {
    render(<CategoryBadge category="CONNECTIVITY" />);
    expect(screen.getByText("Connectivity")).toBeInTheDocument();
    expect(screen.queryByText("CONNECTIVITY")).toBeNull();
  });

  it("ResolutionBadge distinguishes open vs resolved", () => {
    const { rerender } = render(<ResolutionBadge resolved={false} />);
    expect(screen.getByText("Open")).toBeInTheDocument();
    rerender(<ResolutionBadge resolved={true} />);
    expect(screen.getByText("Resolved")).toBeInTheDocument();
  });
});

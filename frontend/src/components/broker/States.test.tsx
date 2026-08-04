import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { EmptyState, ErrorState, LoadingState } from "@/components/broker/States";

describe("broker states", () => {
  it("LoadingState exposes an aria-busy status region", () => {
    render(<LoadingState label="Loading broker accounts…" />);
    const s = screen.getByRole("status");
    expect(s).toHaveAttribute("aria-busy", "true");
    expect(s).toHaveTextContent("Loading broker accounts…");
  });

  it("EmptyState renders a title, body and optional action", () => {
    render(<EmptyState action={<button>Add account</button>} />);
    expect(screen.getByText(/no broker accounts yet/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /add account/i })).toBeInTheDocument();
  });

  it("ErrorState is an alert and retries", async () => {
    const onRetry = vi.fn();
    render(<ErrorState message="Boom" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("Boom");
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

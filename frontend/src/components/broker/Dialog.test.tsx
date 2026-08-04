import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Dialog } from "@/components/broker/Dialog";

describe("Dialog (accessibility)", () => {
  it("is a labelled modal dialog", () => {
    render(<Dialog open onClose={() => {}} title="Replace credentials"><p>Body</p></Dialog>);
    const d = screen.getByRole("dialog");
    expect(d).toHaveAttribute("aria-modal", "true");
    expect(d).toHaveAccessibleName("Replace credentials");
  });

  it("renders nothing when closed", () => {
    render(<Dialog open={false} onClose={() => {}} title="X">body</Dialog>);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("closes on Escape and via the close button", async () => {
    const onClose = vi.fn();
    render(<Dialog open onClose={onClose} title="X"><button>inside</button></Dialog>);
    await userEvent.keyboard("{Escape}");
    await userEvent.click(screen.getByRole("button", { name: /close/i }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("does not close while busy", async () => {
    const onClose = vi.fn();
    render(<Dialog open busy onClose={onClose} title="X"><button>inside</button></Dialog>);
    await userEvent.keyboard("{Escape}");
    expect(onClose).not.toHaveBeenCalled();
  });
});

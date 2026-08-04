import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { disconnectAccount } = vi.hoisted(() => ({
  disconnectAccount: vi.fn().mockResolvedValue({ disconnected: true, credential_destroyed: true, row_deleted: false }),
}));
vi.mock("@/lib/broker-api", () => ({ disconnectAccount }));

import { DisconnectDialog } from "@/components/broker/DisconnectDialog";

describe("DisconnectDialog", () => {
  it("explains the effect and calls the backend on confirm", async () => {
    const onDisconnected = vi.fn();
    render(<DisconnectDialog open accountId={7} accountLabel="My Broker" onClose={() => {}} onDisconnected={onDisconnected} />);
    expect(screen.getByText(/permanently destroyed/i)).toBeInTheDocument();
    expect(screen.getByText(/history is preserved/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /^disconnect$/i }));
    expect(disconnectAccount).toHaveBeenCalledWith(7);
    await waitFor(() => expect(onDisconnected).toHaveBeenCalled());
  });
});

import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { createAccount, testConnection } = vi.hoisted(() => ({
  createAccount: vi.fn().mockResolvedValue({ id: 42 }),
  testConnection: vi.fn().mockResolvedValue({ status: "HEALTHY", reason_code: "demo_ok" }),
}));
vi.mock("@/lib/broker-api", () => ({ createAccount, testConnection }));

import { BrokerAccountWizard } from "@/components/broker/BrokerAccountWizard";

describe("BrokerAccountWizard", () => {
  it("uses a password input (never a plaintext text field)", () => {
    render(<BrokerAccountWizard open onClose={() => {}} onAdded={() => {}} />);
    expect(screen.getByLabelText(/^password$/i)).toHaveAttribute("type", "password");
  });

  it("creates the account then validates it, then shows the result", async () => {
    const onAdded = vi.fn();
    render(<BrokerAccountWizard open onClose={() => {}} onAdded={onAdded} />);
    await userEvent.type(screen.getByLabelText(/^broker$/i), "IS6");
    await userEvent.type(screen.getByLabelText(/account number/i), "1302575");
    await userEvent.type(screen.getByLabelText(/^password$/i), "secret");
    await userEvent.click(screen.getByRole("button", { name: /add & validate/i }));
    await waitFor(() => expect(createAccount).toHaveBeenCalledTimes(1));
    expect(testConnection).toHaveBeenCalledWith(42);
    expect(onAdded).toHaveBeenCalled();
    expect(await screen.findByText(/added and validated/i)).toBeInTheDocument();
  });
});

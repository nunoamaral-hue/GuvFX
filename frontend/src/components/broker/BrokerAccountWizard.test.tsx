import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const { createAccount, testConnection, addHostedAccount } = vi.hoisted(() => ({
  createAccount: vi.fn().mockResolvedValue({ id: 42 }),
  testConnection: vi.fn().mockResolvedValue({ status: "HEALTHY", reason_code: "demo_ok" }),
  addHostedAccount: vi.fn().mockResolvedValue({ status: "created", trading_account_id: 99, workspace_uuid: "w1" }),
}));
vi.mock("@/lib/broker-api", () => ({ createAccount, testConnection, addHostedAccount }));

import { BrokerAccountWizard } from "@/components/broker/BrokerAccountWizard";

describe("BrokerAccountWizard — legacy (traditional) mode", () => {
  it("uses a password input (never a plaintext text field)", () => {
    render(<BrokerAccountWizard open onClose={() => {}} onAdded={() => {}} />);
    expect(screen.getByLabelText(/^password$/i)).toHaveAttribute("type", "password");
  });

  it("creates the account then validates it, then shows the result", async () => {
    createAccount.mockClear();
    const onAdded = vi.fn();
    render(<BrokerAccountWizard open onClose={() => {}} onAdded={onAdded} />);
    await userEvent.type(screen.getByLabelText(/^broker$/i), "IS6");
    await userEvent.type(screen.getByLabelText(/account number/i), "1302575");
    await userEvent.type(screen.getByLabelText(/^password$/i), "secret");
    // D2: an explicit account type must be chosen (no default) before the account can be created.
    await userEvent.click(screen.getByRole("radio", { name: /demo account/i }));
    await userEvent.click(screen.getByRole("button", { name: /add & validate/i }));
    await waitFor(() => expect(createAccount).toHaveBeenCalledTimes(1));
    expect(createAccount).toHaveBeenCalledWith(expect.objectContaining({ account_type: "demo" }));
    expect(testConnection).toHaveBeenCalledWith(42);
    expect(onAdded).toHaveBeenCalled();
    expect(await screen.findByText(/added and validated/i)).toBeInTheDocument();
  });

  it("D2: disables Add & validate until an account type is chosen (no default)", async () => {
    createAccount.mockClear();
    render(<BrokerAccountWizard open onClose={() => {}} onAdded={() => {}} />);
    await userEvent.type(screen.getByLabelText(/^broker$/i), "IS6");
    await userEvent.type(screen.getByLabelText(/account number/i), "1302575");
    await userEvent.type(screen.getByLabelText(/^password$/i), "secret");
    // No type chosen yet → submit disabled and neither radio is pre-selected (no default).
    const submit = screen.getByRole("button", { name: /add & validate/i });
    expect(submit).toBeDisabled();
    expect(screen.getByRole("radio", { name: /demo account/i })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByRole("radio", { name: /live account/i })).toHaveAttribute("aria-checked", "false");
    // Choosing Live enables it and maps to the live account type.
    await userEvent.click(screen.getByRole("radio", { name: /live account/i }));
    expect(submit).toBeEnabled();
    await userEvent.click(submit);
    await waitFor(() => expect(createAccount).toHaveBeenCalledTimes(1));
    expect(createAccount).toHaveBeenCalledWith(expect.objectContaining({ account_type: "live" }));
  });
});

describe("BrokerAccountWizard — hosted mode (member onboarding)", () => {
  it("collects NO password and posts to the hosted endpoint with broker/login/server/is_demo", async () => {
    createAccount.mockClear(); addHostedAccount.mockClear();
    const onAdded = vi.fn();
    render(<BrokerAccountWizard open hosted onClose={() => {}} onAdded={onAdded} />);
    // no password field in hosted mode (login happens in MT5)
    expect(screen.queryByLabelText(/^password$/i)).not.toBeInTheDocument();
    await userEvent.type(screen.getByLabelText(/^broker$/i), "Taurex");
    await userEvent.type(screen.getByLabelText(/^server$/i), "Taurex-Demo");
    await userEvent.type(screen.getByLabelText(/account number/i), "830227146");
    await userEvent.click(screen.getByRole("button", { name: /^add account$/i }));
    await waitFor(() => expect(addHostedAccount).toHaveBeenCalledWith({
      broker_name: "Taurex", expected_login: "830227146", expected_server: "Taurex-Demo", is_demo: true,
    }));
    expect(createAccount).not.toHaveBeenCalled();   // never the plain/password path
    expect(onAdded).toHaveBeenCalled();
    expect(await screen.findByText(/being set up/i)).toBeInTheDocument();
    expect(screen.getByText(/Open MT5/i)).toBeInTheDocument();
  });

  it("requires a server in hosted mode (needed for the broker identity)", async () => {
    addHostedAccount.mockClear();
    render(<BrokerAccountWizard open hosted onClose={() => {}} onAdded={() => {}} />);
    await userEvent.type(screen.getByLabelText(/^broker$/i), "Taurex");
    await userEvent.type(screen.getByLabelText(/account number/i), "830227146");
    // leave server empty → the native required attribute blocks submit; assert the field is required
    expect(screen.getByLabelText(/^server$/i)).toBeRequired();
    expect(addHostedAccount).not.toHaveBeenCalled();
  });
});

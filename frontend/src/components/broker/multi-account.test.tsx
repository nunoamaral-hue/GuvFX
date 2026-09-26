/** Phase C4 — customer-visible multi-account model (DARK): frontend behaviour matrix.
 *
 * Covers: card rendering (masked #, Demo/Live, strategy-count badge, broker/server), secret-safety (never
 * renders the full number or any credential), account-EXPLICIT View MT5 (correct id; viewing B never mutates
 * A), STANDARD vs CONCURRENT header + the STANDARD switch confirm modal, Myfxbook link isolation, and the
 * single-account regression. */
import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { BrokerAccount } from "@/types/broker";

const api = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  getBrokerStatus: vi.fn(),
  getEntitlementSummary: vi.fn(),
  openMt5Desktop: vi.fn(),
  setAccountActive: vi.fn(),
}));
vi.mock("@/lib/broker-api", () => api);
vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a>,
}));

import { AccountCard } from "@/components/broker/AccountCard";
import { BrokerAccountsContent } from "@/components/broker/BrokerAccountsContent";

function acct(over: Partial<BrokerAccount> = {}): BrokerAccount {
  return {
    id: 1, name: "Primary", broker_name: "Pepperstone", broker_display_name: "Pepperstone",
    server_name: "PepperstoneUK-Demo", account_number: "62139344", masked_account_number: "••••9344",
    active_strategy_count: 2, is_demo: true, is_active: false, readiness_provider: "persistent_workspace",
    mt5_instance: null, runtime_ready: true, runtime_state: "READY",
    myfxbook_url: null, myfxbook_system_id: null, myfxbook_enabled: false, ...over,
  };
}

beforeEach(() => {
  api.listAccounts.mockReset();
  api.getBrokerStatus.mockReset().mockResolvedValue(null);
  api.getEntitlementSummary.mockReset();
  api.openMt5Desktop.mockReset().mockResolvedValue({ url: "https://guac.example/x" });
  api.setAccountActive.mockReset().mockResolvedValue({ ok: true, id: 1, is_active: true });
});

describe("AccountCard (C4)", () => {
  it("renders masked number, Demo label, strategy badge, broker & server — never the full number", () => {
    render(<AccountCard account={acct()} />);
    expect(screen.getByText(/••••9344/)).toBeInTheDocument();
    expect(screen.queryByText(/62139344/)).not.toBeInTheDocument();  // full number NEVER rendered
    expect(screen.getByText("Demo")).toBeInTheDocument();
    expect(screen.getByText("2 strategies")).toBeInTheDocument();
    expect(screen.getByText(/Pepperstone · PepperstoneUK-Demo/)).toBeInTheDocument();
  });

  it("shows Live for a live account and singular '1 strategy'", () => {
    render(<AccountCard account={acct({ is_demo: false, active_strategy_count: 1 })} />);
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByText("1 strategy")).toBeInTheDocument();
  });

  it("renders a Myfxbook link only when enabled with a url", () => {
    const { rerender } = render(<AccountCard account={acct()} onViewMt5={() => {}} />);
    expect(screen.queryByRole("link", { name: /myfxbook/i })).not.toBeInTheDocument();
    rerender(<AccountCard account={acct({ myfxbook_enabled: true, myfxbook_url: "https://myfxbook.com/x" })}
                          onViewMt5={() => {}} />);
    const link = screen.getByRole("link", { name: /myfxbook/i });
    expect(link).toHaveAttribute("href", "https://myfxbook.com/x");
    expect(link).toHaveAttribute("rel", expect.stringContaining("noopener"));
  });

  it("never renders a non-http(s) Myfxbook scheme (defence-in-depth)", () => {
    render(<AccountCard account={acct({ myfxbook_enabled: true, myfxbook_url: "javascript:alert(1)" })}
                        onViewMt5={() => {}} />);
    expect(screen.queryByRole("link", { name: /myfxbook/i })).not.toBeInTheDocument();
  });

  it("View MT5 calls back with THIS account's id (account-explicit)", async () => {
    const onViewMt5 = vi.fn();
    render(<AccountCard account={acct({ id: 77 })} onViewMt5={onViewMt5} />);
    await userEvent.click(screen.getByRole("button", { name: /view mt5/i }));
    expect(onViewMt5).toHaveBeenCalledWith(77);
  });
});

describe("BrokerAccountsContent (C4)", () => {
  it("STANDARD: shows 'only one can trade' and confirms a switch before activating B", async () => {
    api.listAccounts.mockResolvedValue([
      acct({ id: 1, name: "Alpha", is_active: true }),
      acct({ id: 2, name: "Bravo", account_number: "70000002", masked_account_number: "••••0002", is_active: false }),
    ]);
    api.getEntitlementSummary.mockResolvedValue({
      account_mode: "standard", active_count: 1, concurrent_limit: 1, owned_count: 2, owned_limit: 5,
    });
    render(<BrokerAccountsContent />);
    expect(await screen.findByText(/only one account can trade at a time/i)).toBeInTheDocument();

    // Activate Bravo → confirm modal (does NOT call the API yet).
    await userEvent.click(await screen.findByRole("button", { name: /^activate bravo$/i }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent(/will stop/i);
    expect(api.setAccountActive).not.toHaveBeenCalled();

    // Confirm → activates Bravo (id 2) exactly.
    await userEvent.click(within(dialog).getByRole("button", { name: /activate bravo/i }));
    await waitFor(() => expect(api.setAccountActive).toHaveBeenCalledWith(2, true));
  });

  it("CONCURRENT: shows 'Active N / limit' and activates without a switch modal", async () => {
    api.listAccounts.mockResolvedValue([
      acct({ id: 1, name: "Alpha", is_active: true }),
      acct({ id: 2, name: "Bravo", is_active: false }),
    ]);
    api.getEntitlementSummary.mockResolvedValue({
      account_mode: "concurrent", active_count: 1, concurrent_limit: 5, owned_count: 2, owned_limit: 5,
    });
    render(<BrokerAccountsContent />);
    expect(await screen.findByText(/Active 1 \/ 5/)).toBeInTheDocument();

    await userEvent.click(await screen.findByRole("button", { name: /^activate bravo$/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();  // no switch modal in CONCURRENT
    await waitFor(() => expect(api.setAccountActive).toHaveBeenCalledWith(2, true));
  });

  it("View MT5 is account-explicit and viewing B never activates/mutates A", async () => {
    api.listAccounts.mockResolvedValue([
      acct({ id: 1, name: "Alpha", is_active: true }),
      acct({ id: 2, name: "Bravo", is_active: false }),
    ]);
    api.getEntitlementSummary.mockResolvedValue({
      account_mode: "concurrent", active_count: 1, concurrent_limit: 5, owned_count: 2, owned_limit: 5,
    });
    const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
    render(<BrokerAccountsContent />);
    await userEvent.click(await screen.findByRole("button", { name: /view mt5 for bravo/i }));
    await waitFor(() => expect(api.openMt5Desktop).toHaveBeenCalledWith(2));  // explicit id, not a guess
    expect(api.setAccountActive).not.toHaveBeenCalled();  // viewing never mutates active state
    openSpy.mockRestore();
  });

  it("single-account regression: renders one card and degrades header when entitlement fails", async () => {
    api.listAccounts.mockResolvedValue([acct({ id: 9, name: "Solo", is_active: true })]);
    api.getEntitlementSummary.mockRejectedValue(new Error("unavailable"));
    render(<BrokerAccountsContent />);
    expect(await screen.findByText("Solo")).toBeInTheDocument();
    expect(await screen.findByText(/connect and validate the broker accounts/i)).toBeInTheDocument();
  });
});

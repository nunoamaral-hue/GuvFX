/** WAYOND account-scoped strategy management — the account management page, the account-scoped
 * Manage-Strategies experience (add/remove, per-account isolation), Start/Stop wording, and the
 * broker-naming helper (Phase 11). */
import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { accountTitle, brokerLabel } from "@/lib/broker-status";

const nav = vi.hoisted(() => ({ replace: vi.fn() }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: nav.replace, refresh: vi.fn() }),
  useParams: () => ({ id: "35" }),
  redirect: vi.fn(),
}));
vi.mock("next/link", () => ({
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  default: ({ children, href, ...rest }: any) => <a href={href} {...rest}>{children}</a>,
}));

const broker = vi.hoisted(() => ({
  getBrokerAccountsUxEnabled: vi.fn(),
  getAccount: vi.fn(),
  getBrokerStatus: vi.fn(),
  getDeliveryState: vi.fn(),
  getEntitlementSummary: vi.fn(),
  openMt5Desktop: vi.fn(),
  setAccountActive: vi.fn(),
}));
vi.mock("@/lib/broker-api", () => broker);

const asn = vi.hoisted(() => ({
  listAccountAssignments: vi.fn(),
  listMyStrategies: vi.fn(),
  createAssignment: vi.fn(),
  removeAssignment: vi.fn(),
}));
vi.mock("@/lib/strategy-assignments-api", () => asn);

import { AccountManageContent } from "@/components/broker/AccountManageContent";
import { AccountStrategiesContent } from "@/components/broker/AccountStrategiesContent";

const HOSTED_IS6 = {
  id: 25, name: "Hosted Workspace", broker_name: "Hosted Workspace", broker_display_name: "",
  server_name: "IS6Technologies-Demo", account_number: "1302587", masked_account_number: "••••2587",
  is_demo: true, is_active: true, readiness_provider: "persistent_workspace", mt5_instance: null,
};
const PEPPERSTONE = {
  id: 35, name: "Pepperstone", broker_name: "Pepperstone", broker_display_name: "",
  server_name: "PepperstoneUK-Demo", account_number: "62145672", masked_account_number: "••••5672",
  is_demo: true, is_active: true, readiness_provider: "persistent_workspace", mt5_instance: null,
};

beforeEach(() => {
  Object.values(broker).forEach((f) => f.mockReset());
  Object.values(asn).forEach((f) => f.mockReset());
  nav.replace.mockReset();
  broker.getBrokerAccountsUxEnabled.mockResolvedValue(true);
  broker.getBrokerStatus.mockResolvedValue(null);
  broker.getDeliveryState.mockResolvedValue({ account_id: 35, deliverable: true, connected: false, delivery_readiness: "DELIVERY_DELIVERABLE", delivery_state: "NONE", remoteapp_ready: false, node_assigned: true, is_owner: true });
  broker.getEntitlementSummary.mockResolvedValue({ account_mode: "concurrent", active_count: 2, concurrent_limit: 5, owned_count: 2, owned_limit: 5, switch_enforced: true });
  asn.listAccountAssignments.mockResolvedValue([]);
  asn.listMyStrategies.mockResolvedValue([]);
});

// ─────────────────────────── Phase 11 — broker naming ───────────────────────────
describe("broker naming helper (Phase 11)", () => {
  it("derives a friendly broker identity from the server when the name is the generic placeholder", () => {
    expect(accountTitle(HOSTED_IS6)).toBe("IS6 Technologies");
    expect(brokerLabel(HOSTED_IS6)).toBe("IS6 Technologies");
    // never surfaces the infrastructure placeholder
    expect(accountTitle(HOSTED_IS6)).not.toMatch(/hosted workspace/i);
  });
  it("prefers a real stored broker name (Pepperstone demonstrates the desired presentation)", () => {
    expect(accountTitle(PEPPERSTONE)).toBe("Pepperstone");
    expect(brokerLabel(PEPPERSTONE)).toBe("Pepperstone");
  });
  it("prefers an explicit broker_display_name above all", () => {
    expect(brokerLabel({ ...HOSTED_IS6, broker_display_name: "IS6 Technologies Ltd" })).toBe("IS6 Technologies Ltd");
  });
});

// ─────────────────────────── Phase 2 — account management page ───────────────────────────
describe("AccountManageContent (Phase 2)", () => {
  it("renders the derived broker identity, member facts, and account-scoped actions — no infra terms", async () => {
    broker.getAccount.mockResolvedValue(HOSTED_IS6);
    asn.listAccountAssignments.mockResolvedValue([{ id: 10, strategy: 10, strategy_name: "Wayond WIM", account: 25, is_active: true, stage: "LIVE" }]);
    render(<AccountManageContent accountId={25} />);
    expect(await screen.findByRole("heading", { name: "IS6 Technologies" })).toBeInTheDocument();
    expect(screen.queryByText(/hosted workspace/i)).not.toBeInTheDocument();       // placeholder never shown
    expect(screen.getByText(/1 assigned/)).toBeInTheDocument();                    // strategies count
    expect(screen.getByRole("link", { name: /manage strategies/i })).toHaveAttribute("href", "/accounts/25/strategies");
    expect(screen.getByRole("button", { name: /stop trading/i })).toBeInTheDocument(); // active → Stop
    // no infrastructure vocabulary anywhere on the page
    expect(screen.queryByText(/SID|runtime path|workspace uuid|endpoint|bridge|node/i)).not.toBeInTheDocument();
  });

  it("non-granted user is routed to /accounts (per-user gate, not a dead end)", async () => {
    broker.getBrokerAccountsUxEnabled.mockResolvedValue(false);
    render(<AccountManageContent accountId={25} />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/accounts"));
    expect(broker.getAccount).not.toHaveBeenCalled();
  });
});

// ─────────────────────────── Phase 3/4/5 — account-scoped Manage Strategies ───────────────────────────
describe("AccountStrategiesContent (Phase 3/4/5)", () => {
  it("lists ONLY this account's assignments and offers unassigned owned strategies to add", async () => {
    broker.getAccount.mockResolvedValue(PEPPERSTONE);
    asn.listAccountAssignments.mockResolvedValue([
      { id: 16, strategy: 10, strategy_name: "Wayond WIM", account: 35, is_active: true, stage: "LIVE", lot_per_leg: "0.01" },
    ]);
    asn.listMyStrategies.mockResolvedValue([{ id: 10, name: "Wayond WIM" }, { id: 20, name: "Momentum X" }]);
    render(<AccountStrategiesContent accountId={35} />);
    // assigned list shows the strategy name + its sizing
    expect(await screen.findByText("Wayond WIM")).toBeInTheDocument();
    expect(screen.getByText(/0\.01 lots\/leg/)).toBeInTheDocument();
    // the fetch was account-explicit
    expect(asn.listAccountAssignments).toHaveBeenCalledWith(35);
    // Add picker excludes the already-assigned strategy (10), offers the unassigned one (20)
    expect(screen.getByRole("option", { name: "Momentum X" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Wayond WIM" })).not.toBeInTheDocument();
  });

  it("Add strategy binds the chosen strategy to THIS account only", async () => {
    broker.getAccount.mockResolvedValue(PEPPERSTONE);
    asn.listAccountAssignments.mockResolvedValue([]);
    asn.listMyStrategies.mockResolvedValue([{ id: 20, name: "Momentum X" }]);
    asn.createAssignment.mockResolvedValue({ id: 99, strategy: 20, account: 35, is_active: true, stage: "TEST" });
    render(<AccountStrategiesContent accountId={35} />);
    await screen.findByRole("option", { name: "Momentum X" });
    await userEvent.selectOptions(screen.getByRole("combobox"), "20");
    await userEvent.click(screen.getByRole("button", { name: /add strategy/i }));
    await waitFor(() => expect(asn.createAssignment).toHaveBeenCalledWith({ strategy: 20, account: 35 }));
  });

  it("Remove unassigns ONLY that assignment (per-account isolation)", async () => {
    broker.getAccount.mockResolvedValue(PEPPERSTONE);
    asn.listAccountAssignments.mockResolvedValue([
      { id: 16, strategy: 10, strategy_name: "Wayond WIM", account: 35, is_active: true, stage: "LIVE", lot_per_leg: "0.01" },
    ]);
    asn.removeAssignment.mockResolvedValue(undefined);
    render(<AccountStrategiesContent accountId={35} />);
    await screen.findByText("Wayond WIM");
    await userEvent.click(screen.getByRole("button", { name: /remove/i }));
    await waitFor(() => expect(asn.removeAssignment).toHaveBeenCalledWith(16));
  });

  it("multiple strategies per account are all shown (no product limit)", async () => {
    broker.getAccount.mockResolvedValue(PEPPERSTONE);
    asn.listAccountAssignments.mockResolvedValue([
      { id: 16, strategy: 10, strategy_name: "Wayond WIM", account: 35, is_active: true, stage: "LIVE" },
      { id: 17, strategy: 20, strategy_name: "Momentum X", account: 35, is_active: true, stage: "TEST" },
      { id: 18, strategy: 21, strategy_name: "Mean Reversion", account: 35, is_active: false, stage: "TEST" },
    ]);
    render(<AccountStrategiesContent accountId={35} />);
    expect(await screen.findByText("Wayond WIM")).toBeInTheDocument();
    expect(screen.getByText("Momentum X")).toBeInTheDocument();
    expect(screen.getByText("Mean Reversion")).toBeInTheDocument();
  });
});

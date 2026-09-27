/** WAYOND account-scoped strategy management — the account management page, the account-scoped
 * Manage-Strategies experience (add/remove, per-account isolation), Start/Stop wording, and the
 * broker-naming helper (Phase 11). */
import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { accountTitle, brokerLabel } from "@/lib/broker-status";

// A STABLE router object (Next's real useRouter is memoized) so effects depending on `router` don't
// re-run every render — an unstable mock would reset controlled inputs mid-typing.
const nav = vi.hoisted(() => {
  const replace = vi.fn();
  return { replace, router: { push: () => {}, replace, refresh: () => {} } };
});
vi.mock("next/navigation", () => ({
  useRouter: () => nav.router,
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
  listAssignableStrategies: vi.fn(),
  createAssignment: vi.fn(),
  removeAssignment: vi.fn(),
  getAssignment: vi.fn(),
  getLegSizing: vi.fn(),
  setLegSizing: vi.fn(),
}));
vi.mock("@/lib/strategy-assignments-api", () => asn);

import { AccountManageContent } from "@/components/broker/AccountManageContent";
import { AccountStrategiesContent } from "@/components/broker/AccountStrategiesContent";
import { AssignmentConfigContent } from "@/components/broker/AssignmentConfigContent";

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
  asn.listAssignableStrategies.mockResolvedValue([]);
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
    asn.listAssignableStrategies.mockResolvedValue([{ id: 10, name: "Wayond WIM" }, { id: 20, name: "Momentum X" }]);
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
    asn.listAssignableStrategies.mockResolvedValue([{ id: 20, name: "Momentum X" }]);
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

  it("offers PUBLISHED strategies the member does not own (availability, not ownership)", async () => {
    broker.getAccount.mockResolvedValue(PEPPERSTONE);
    asn.listAccountAssignments.mockResolvedValue([]);
    // a published marketplace strategy the member does not own is offered to add
    asn.listAssignableStrategies.mockResolvedValue([{ id: 99, name: "GuvFX Momentum (Marketplace)", is_marketplace: true }]);
    render(<AccountStrategiesContent accountId={35} />);
    expect(await screen.findByRole("option", { name: "GuvFX Momentum (Marketplace)" })).toBeInTheDocument();
  });

  it("hides a strategy whose FAMILY is already assigned (canonical not offered on a legacy-WIM account)", async () => {
    broker.getAccount.mockResolvedValue(PEPPERSTONE);
    // account already runs a legacy WIM (family wayond-wim)
    asn.listAccountAssignments.mockResolvedValue([
      { id: 16, strategy: 10, strategy_name: "Wayond WIM", strategy_family: "wayond-wim", account: 35, is_active: true, stage: "LIVE" },
    ]);
    // canonical (different id, same family) + an unrelated strategy
    asn.listAssignableStrategies.mockResolvedValue([
      { id: 99, name: "Wayond WIM Strategy", is_marketplace: true, family: "wayond-wim" },
      { id: 77, name: "Momentum X", is_marketplace: true, family: "momentum-x" },
    ]);
    render(<AccountStrategiesContent accountId={35} />);
    expect(await screen.findByRole("option", { name: "Momentum X" })).toBeInTheDocument();
    // the canonical WIM is NOT offered — its family is already assigned (would double-run)
    expect(screen.queryByRole("option", { name: "Wayond WIM Strategy" })).not.toBeInTheDocument();
  });

  it("an INACTIVE same-family assignment does NOT hide the canonical (transition stays possible)", async () => {
    broker.getAccount.mockResolvedValue(PEPPERSTONE);
    // legacy WIM present but STOPPED (is_active false) — matches the backend active-only family guard
    asn.listAccountAssignments.mockResolvedValue([
      { id: 16, strategy: 10, strategy_name: "Wayond WIM", strategy_family: "wayond-wim", account: 35, is_active: false, stage: "TEST" },
    ]);
    asn.listAssignableStrategies.mockResolvedValue([
      { id: 99, name: "Wayond WIM Strategy", is_marketplace: true, family: "wayond-wim" },
    ]);
    render(<AccountStrategiesContent accountId={35} />);
    // canonical IS offered because the only same-family row is inactive
    expect(await screen.findByRole("option", { name: "Wayond WIM Strategy" })).toBeInTheDocument();
  });

  it("Configure opens the ASSIGNMENT-specific route (not the global strategy page)", async () => {
    broker.getAccount.mockResolvedValue(PEPPERSTONE);
    asn.listAccountAssignments.mockResolvedValue([
      { id: 16, strategy: 10, strategy_name: "Wayond WIM", account: 35, is_active: true, stage: "LIVE", lot_per_leg: "0.01" },
    ]);
    render(<AccountStrategiesContent accountId={35} />);
    const cfg = await screen.findByRole("link", { name: /configure/i });
    expect(cfg).toHaveAttribute("href", "/accounts/35/strategies/16");   // assignment-scoped, not /strategies/10
  });
});

// ─────────────────────────── Phase 7/9/11 — assignment-specific Configure ───────────────────────────
describe("AssignmentConfigContent (assignment-specific Configure)", () => {
  const LEG = {
    assignment_id: 16, lot_per_leg: "0.01", is_override: true, default_lot_per_leg: "0.01",
    min: "0.01", step: "0.01", max: "0.40", source_cap: "0.40", max_legs: 3, applies_to_live_execution: true,
    note: "Sets the lot size for EACH position Wayond opens.",
  };
  beforeEach(() => {
    broker.getAccount.mockResolvedValue(PEPPERSTONE);
    asn.getAssignment.mockResolvedValue({ id: 16, strategy: 10, strategy_name: "Wayond WIM", account: 35, is_active: true, stage: "LIVE" });
    asn.getLegSizing.mockResolvedValue(LEG);
    asn.setLegSizing.mockResolvedValue({ ...LEG, lot_per_leg: "0.02" });
  });

  it("shows the strategy name, broker identity, status, and the REAL 0.01 assignment sizing", async () => {
    render(<AssignmentConfigContent accountId={35} assignmentId={16} />);
    expect(await screen.findByRole("heading", { name: "Wayond WIM" })).toBeInTheDocument();
    expect(screen.getByText(/Pepperstone · PepperstoneUK-Demo/)).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    // the sizing input reflects the assignment's actual 0.01 — not a blank global risk field
    expect((screen.getByLabelText(/position size per trade leg/i) as HTMLInputElement).value).toBe("0.01");
  });

  it("saving a new size PUTs to THIS assignment only", async () => {
    render(<AssignmentConfigContent accountId={35} assignmentId={16} />);
    const user = userEvent.setup();
    const input = await screen.findByLabelText(/position size per trade leg/i);
    await user.clear(input);
    await user.type(input, "0.02");
    await waitFor(() => expect(input).toHaveValue("0.02"));
    await user.click(screen.getByRole("button", { name: /^save$/i }));
    await waitFor(() => expect(asn.setLegSizing).toHaveBeenCalledWith(16, "0.02"));
  });

  it("redirects to the strategies list when the assignment is not on this account", async () => {
    asn.getAssignment.mockResolvedValue({ id: 16, strategy: 10, strategy_name: "Wayond WIM", account: 999, is_active: true, stage: "LIVE" });
    render(<AssignmentConfigContent accountId={35} assignmentId={16} />);
    await waitFor(() => expect(nav.replace).toHaveBeenCalledWith("/accounts/35/strategies"));
  });
});

import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { CopyTradingConfig } from "@/types/copy-trading";

/** Founder config UX: permanent SHADOW banner, cohort not-available gate, master↔follower overlap prevention,
 * and the explicit save→confirm step posting the right body (execution never enabled from the UI). */

const { getCopyTradingConfig, saveCopyTradingConfig } = vi.hoisted(() => ({
  getCopyTradingConfig: vi.fn(), saveCopyTradingConfig: vi.fn(),
}));
vi.mock("@/lib/copy-trading-api", () => ({ getCopyTradingConfig, saveCopyTradingConfig }));

import { CopyTradingConfigContent } from "./CopyTradingConfigContent";

const acct = (id: number, broker: string, monitored = true) => ({
  account_id: id, name: `A${id}`, broker, environment: "live" as const, masked_login: "••••1234",
  currency: "USD", monitoring_state: (monitored ? "monitored" : "stale") as "monitored" | "stale",
  data_age_seconds: 10, ready: monitored,
});

const enabledConfig = (over: Partial<CopyTradingConfig> = {}): CopyTradingConfig => ({
  enabled: true, banner: "SHADOW — EXECUTION OFF", execution: "off",
  explain: "GuvFX is observing and comparing potential replication. No follower broker order is placed. Saving configuration does NOT enable execution.",
  eligible_accounts: [acct(46, "TradersWay"), acct(48, "IS6FX"), acct(49, "Pepperstone", false)],
  config: null, ...over,
});

beforeEach(() => {
  getCopyTradingConfig.mockReset();
  saveCopyTradingConfig.mockReset();
});

describe("CopyTradingConfigContent", () => {
  it("shows the not-available state (cohort default-DENY) without leaking accounts", async () => {
    getCopyTradingConfig.mockResolvedValue({ enabled: false, banner: "SHADOW — EXECUTION OFF" });
    render(<CopyTradingConfigContent />);
    expect(await screen.findByText(/not available for your account/i)).toBeTruthy();
    expect(screen.getByTestId("copier-config-banner").textContent).toMatch(/SHADOW — EXECUTION OFF/);
    expect(screen.queryByLabelText("master 46")).toBeNull();   // no account controls leaked
  });

  it("always shows the permanent SHADOW — EXECUTION OFF banner and the explain copy", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig());
    render(<CopyTradingConfigContent />);
    expect((await screen.findByTestId("copier-config-banner")).textContent).toMatch(/SHADOW — EXECUTION OFF/);
    expect(screen.getByText(/Saving configuration does NOT enable execution/i)).toBeTruthy();
  });

  it("prevents a master from also being a follower (overlap)", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig());
    render(<CopyTradingConfigContent />);
    await screen.findByLabelText("master 46");
    fireEvent.click(screen.getByLabelText("follower 48"));          // select 48 as follower
    fireEvent.click(screen.getByLabelText("master 48"));            // now make 48 the master
    const follower48 = screen.getByLabelText("follower 48") as HTMLInputElement;
    expect(follower48.disabled).toBe(true);                         // 48 can no longer be a follower
    expect(follower48.checked).toBe(false);
  });

  it("requires explicit confirmation and posts a SHADOW config that cannot enable execution", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig());
    saveCopyTradingConfig.mockResolvedValue({
      ok: true, banner: "SHADOW — EXECUTION OFF", execution: "off",
      audit: { created: [48], updated: [], enabled: [48], disabled: [] }, config: null,
    });
    render(<CopyTradingConfigContent />);
    await screen.findByLabelText("master 46");
    fireEvent.click(screen.getByLabelText("master 46"));
    fireEvent.click(screen.getByLabelText("follower 48"));
    fireEvent.click(screen.getByTestId("copier-config-save"));      // opens confirmation
    expect(screen.getByText(/No follower broker order will be placed and execution stays OFF/i)).toBeTruthy();
    fireEvent.click(screen.getByTestId("copier-config-confirm"));   // explicit confirm
    await waitFor(() => expect(saveCopyTradingConfig).toHaveBeenCalledTimes(1));
    const body = saveCopyTradingConfig.mock.calls[0][0];
    expect(body.master_account_id).toBe(46);
    expect(body.followers.map((f: { account_id: number }) => f.account_id)).toEqual([48]);
    expect(body.followers[0].sizing_mode).toBe("FIXED_LOT");
    expect(await screen.findByTestId("copier-config-result")).toBeTruthy();
  });
});

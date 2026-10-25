import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { CopyTradingConfig, ConfigPair } from "@/types/copy-trading";

/** Founder config UX: compact observation-mode status (not an alarm banner), cohort not-available gate,
 * master↔follower overlap prevention, the explicit save→confirm step (execution never enabled from the UI),
 * PER-FOLLOWER sizing isolation, instrument-map evidence + fail-closed "not execution-ready" verdict, and EN/JA. */

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

const pair = (over: Partial<ConfigPair> & { follower_account_id: number }): ConfigPair => ({
  enabled: true, follower_current: true, sizing_mode: "FIXED_LOT", fixed_lot: "0.01", max_volume: "0.10",
  max_concurrent_positions: 1, instrument_map_ready: true, instrument_maps: [], observer_last_observed_at: null,
  last_observed_event: null, supported_risk_controls: [], unavailable_risk_metadata: [], live_execution_blocked: true,
  ...over,
});

const enabledConfig = (over: Partial<CopyTradingConfig> = {}): CopyTradingConfig => ({
  enabled: true, banner: "SHADOW — EXECUTION OFF", execution: "off",
  execution_status: { mode: "observation", execution: "off", label: "Observation mode",
                      detail: "Copy execution off", note: "Saving configuration does not activate trading." },
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
    getCopyTradingConfig.mockResolvedValue({ enabled: false, banner: "" });
    render(<CopyTradingConfigContent />);
    expect(await screen.findByText(/not available for your account/i)).toBeTruthy();
    expect(screen.getByTestId("copier-config-status").textContent).toMatch(/Observation mode/);
    expect(screen.queryByLabelText("master 46")).toBeNull();   // no account controls leaked
  });

  it("shows the quiet observation-mode status (no full-width alarm banner) and the explain copy", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig());
    render(<CopyTradingConfigContent />);
    const chip = await screen.findByTestId("copier-config-status");
    expect(chip.textContent).toMatch(/Observation mode/);
    expect(chip.textContent).toMatch(/Copy execution off/);
    expect(chip.textContent).toMatch(/does not activate trading/i);
    expect(screen.queryByText(/SHADOW — EXECUTION OFF/)).toBeNull();     // the red banner is gone
    expect(screen.getByText(/Saving configuration does NOT enable execution/i)).toBeTruthy();
  });

  it("prevents a master from also being a follower (overlap)", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig());
    render(<CopyTradingConfigContent />);
    await screen.findByLabelText("master 46");
    fireEvent.click(screen.getByLabelText("follower 48"));
    fireEvent.click(screen.getByLabelText("master 48"));
    const follower48 = screen.getByLabelText("follower 48") as HTMLInputElement;
    expect(follower48.disabled).toBe(true);
    expect(follower48.checked).toBe(false);
  });

  it("requires explicit confirmation and posts a config (with per-follower sizing) that cannot enable execution", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig());
    saveCopyTradingConfig.mockResolvedValue({
      ok: true, banner: "", execution: "off",
      audit: { created: [48], updated: [], enabled: [48], disabled: [] }, config: null,
    });
    render(<CopyTradingConfigContent />);
    await screen.findByLabelText("master 46");
    fireEvent.click(screen.getByLabelText("master 46"));
    fireEvent.click(screen.getByLabelText("follower 48"));
    fireEvent.click(screen.getByTestId("copier-config-save"));
    expect(screen.getByText(/No follower broker order will be placed and execution stays OFF/i)).toBeTruthy();
    fireEvent.click(screen.getByTestId("copier-config-confirm"));
    await waitFor(() => expect(saveCopyTradingConfig).toHaveBeenCalledTimes(1));
    const body = saveCopyTradingConfig.mock.calls[0][0];
    expect(body.master_account_id).toBe(46);
    expect(body.followers.map((f: { account_id: number }) => f.account_id)).toEqual([48]);
    expect(body.followers[0].sizing_mode).toBe("FIXED_LOT");
    expect(body.followers[0].fixed_lot).toBeTruthy();
    expect(await screen.findByTestId("copier-config-result")).toBeTruthy();
  });

  it("renders only the eligible accounts the API returns (historical/disconnected never appear)", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig());
    render(<CopyTradingConfigContent />);
    await screen.findByLabelText("master 46");
    expect(screen.getByLabelText("follower 48")).toBeTruthy();
    expect(screen.queryByLabelText("master 43")).toBeNull();
    expect(screen.queryByLabelText("follower 47")).toBeNull();
  });

  it("discloses operator attention (fail-safe) when the saved config references a now-ineligible account", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig({
      config: { master_account_id: 46, observe_enabled: true, master_current: true, needs_operator_attention: true,
                followers: [pair({ follower_account_id: 48, follower_current: false })] },
    }));
    render(<CopyTradingConfigContent />);
    const notice = await screen.findByTestId("copier-config-attention");
    expect(notice.textContent).toMatch(/Operator attention required/i);
    expect(notice.textContent).toMatch(/execution remains OFF/i);
  });

  // --- per-follower sizing isolation (Workstream A) ---
  it("keeps each follower's sizing independent — editing one never changes another", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig({
      config: { master_account_id: 46, observe_enabled: true, master_current: true, needs_operator_attention: false,
        followers: [
          pair({ follower_account_id: 48, fixed_lot: "0.01", max_volume: "0.10" }),
          pair({ follower_account_id: 49, fixed_lot: "1.000", max_volume: "5.000" }),
        ] },
    }));
    render(<CopyTradingConfigContent />);
    const lot48 = await screen.findByLabelText("fixed lot 48") as HTMLInputElement;
    const lot49 = screen.getByLabelText("fixed lot 49") as HTMLInputElement;
    expect(lot48.value).toBe("0.01");       // each follower shows its OWN stored lot
    expect(lot49.value).toBe("1.000");
    fireEvent.change(lot49, { target: { value: "2.0" } });   // edit ONLY #49
    expect((screen.getByLabelText("fixed lot 49") as HTMLInputElement).value).toBe("2.0");
    expect((screen.getByLabelText("fixed lot 48") as HTMLInputElement).value).toBe("0.01");   // #48 untouched
  });

  it("sends each follower's own sizing in the POST body (no shared value)", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig({
      config: { master_account_id: 46, observe_enabled: true, master_current: true, needs_operator_attention: false,
        followers: [
          pair({ follower_account_id: 48, fixed_lot: "0.01", max_volume: "0.10" }),
          pair({ follower_account_id: 49, fixed_lot: "1.000", max_volume: "5.000" }),
        ] },
    }));
    saveCopyTradingConfig.mockResolvedValue({ ok: true, banner: "", execution: "off",
      audit: { created: [], updated: [], enabled: [], disabled: [] }, config: null });
    render(<CopyTradingConfigContent />);
    await screen.findByLabelText("fixed lot 48");
    fireEvent.click(screen.getByTestId("copier-config-save"));
    fireEvent.click(screen.getByTestId("copier-config-confirm"));
    await waitFor(() => expect(saveCopyTradingConfig).toHaveBeenCalledTimes(1));
    const body = saveCopyTradingConfig.mock.calls[0][0];
    const byId = Object.fromEntries(body.followers.map((f: { account_id: number; fixed_lot: string }) => [f.account_id, f.fixed_lot]));
    expect(byId[48]).toBe("0.01");
    expect(byId[49]).toBe("1.000");
  });

  // --- instrument-map evidence + fail-closed verdict (Workstream A/B) ---
  it("shows the source→follower symbol map evidence and a 'valid · observation only' verdict when ready", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig({
      config: { master_account_id: 46, observe_enabled: true, master_current: true, needs_operator_attention: false,
        followers: [pair({ follower_account_id: 48, config_status: "ready", config_blocked_reason: null,
          instrument_map: { master_broker: "Tradersway-Live", follower_broker: "IS6Technologies-Live",
            same_broker: false, state: "verified", ready: true, failure_reason: null,
            last_verified_at: "2026-10-24T16:19:37Z",
            pairs: [{ master_symbol: "XAUUSD", follower_symbol: "XAUUSD.std", verified: true, verified_at: "2026-10-24T16:19:37Z" }] },
          observed_constraints: { available: true, reason: null,
            symbols: { "XAUUSD.std": { volume_min: 0.01, volume_step: 0.01, volume_max: 30, contract_size: 100, currency_base: "USD", currency_profit: "USD", trade_mode: 4 } } } })] },
    }));
    render(<CopyTradingConfigContent />);
    const verdict = await screen.findByTestId("copier-config-verdict-48");
    expect(verdict.textContent).toMatch(/Configuration valid/i);
    expect(verdict.textContent).not.toMatch(/execution-ready/i);     // never a positive execution-ready claim
    expect(screen.getByText(/XAUUSD → XAUUSD\.std/)).toBeTruthy();    // source → follower symbol
    expect(screen.getByText(/min 0\.01/)).toBeTruthy();               // broker-observed constraint shown
  });

  it("fails closed with 'Not execution-ready' + reason when the instrument map is missing", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig({
      config: { master_account_id: 46, observe_enabled: true, master_current: true, needs_operator_attention: false,
        followers: [pair({ follower_account_id: 49, instrument_map_ready: false, config_status: "blocked",
          config_blocked_reason: "no_symbol_map",
          instrument_map: { master_broker: "Tradersway-Live", follower_broker: "PepperstoneUK-Live",
            same_broker: false, state: "missing", ready: false, failure_reason: "no_symbol_map",
            last_verified_at: null, pairs: [] },
          observed_constraints: { available: false, reason: "no_verified_map", symbols: {} } })] },
    }));
    render(<CopyTradingConfigContent />);
    const verdict = await screen.findByTestId("copier-config-verdict-49");
    expect(verdict.textContent).toMatch(/Not execution-ready/i);
    expect(verdict.textContent).toMatch(/No verified instrument map/i);
  });

  // --- Japanese + accessibility ---
  it("renders Japanese labels when lang='ja'", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig());
    render(<CopyTradingConfigContent lang="ja" />);
    expect(await screen.findByText(/コピートレード — 設定/)).toBeTruthy();
    expect(screen.getByTestId("copier-config-status").textContent).toMatch(/観測モード/);
    expect(screen.getByText(/フォロワー口座/)).toBeTruthy();
  });

  it("exposes accessible roles and per-follower input labels", async () => {
    getCopyTradingConfig.mockResolvedValue(enabledConfig({
      config: { master_account_id: 46, observe_enabled: true, master_current: true, needs_operator_attention: false,
        followers: [pair({ follower_account_id: 48 })] },
    }));
    render(<CopyTradingConfigContent />);
    const chip = await screen.findByTestId("copier-config-status");
    expect(chip.getAttribute("role")).toBe("status");
    // per-follower sizing inputs are individually labelled (no ambiguous shared control)
    expect(screen.getByLabelText("fixed lot 48")).toBeTruthy();
    expect(screen.getByLabelText("max volume 48")).toBeTruthy();
    expect(screen.getByLabelText("max concurrent 48")).toBeTruthy();
    fireEvent.click(screen.getByTestId("copier-config-save"));
    const dialog = screen.getByRole("dialog");
    expect(dialog.getAttribute("aria-modal")).toBe("true");
  });
});

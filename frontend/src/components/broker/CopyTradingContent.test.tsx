import React from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import type { CopyTradingOverview, CopyTradingFollower, CopyTradingReplication } from "@/types/copy-trading";

/** PR C UX truthfulness (ADR-0051): a recorded intent (SHADOW_RECORDED) must read as "NO ORDER SENT",
 * never as a broker-verified success; a LIVE follower must show execution BLOCKED; and the panel must
 * stay DARK-safe (calm "not enabled" state) when the API reports enabled:false or fails. */

const { getCopyTradingOverview } = vi.hoisted(() => ({ getCopyTradingOverview: vi.fn() }));
vi.mock("@/lib/copy-trading-api", () => ({ getCopyTradingOverview }));

import CopyTradingContent from "./CopyTradingContent";

function overview(partial: Partial<CopyTradingOverview>): CopyTradingOverview {
  return {
    enabled: true, master: null, followers: [], recent_events: [], recent_replications: [],
    limitations: { live_follower_execution: "blocked", note: "blocked" },
    ...partial,
  };
}

const MASTER = {
  id: 1, account_id: 100, broker: "IS6", environment: "demo" as const,
  observe_enabled: true, last_observed_at: null,
};

function replication(status: string): CopyTradingReplication {
  return {
    id: 1, event_sequence: 1, event_type: "OPEN", follower_account_id: 200, requested_op: "OPEN",
    status, reason: "", target_volume: "0.05", follower_symbol: "XAUUSD", translated_sl: null,
    translated_tp: null, constraints_authoritative: false, sltp_translation_ok: true,
    follower_ticket: null, latency_ms: null, created_at: "2026-10-20T00:00:00Z",
  };
}

function liveFollower(): CopyTradingFollower {
  return {
    id: 1, follower_account_id: 200, broker: "TradersWay", environment: "live", enabled: true,
    sizing_mode: "MULTIPLIER", fixed_lot: null, multiplier: "1", risk_per_trade_pct: null,
    max_volume: null, max_aggregate_exposure: null, max_concurrent_positions: null,
    max_spread_points: null, max_slippage_points: null, sltp_translation: "SAME_PRICE",
    disable_policy: "A_KEEP_MANAGING", unprotected_close_enabled: false,
    unprotected_close_authorization_version: 0, sync_status: "PENDING", last_outcome: "",
    live_execution_blocked: true, updated_at: "2026-10-20T00:00:00Z",
  };
}

describe("CopyTradingContent", () => {
  it("labels SHADOW_RECORDED as NO ORDER SENT (intent only, not a broker-verified success)", async () => {
    getCopyTradingOverview.mockResolvedValue(overview({ master: MASTER, recent_replications: [replication("SHADOW_RECORDED")] }));
    render(<CopyTradingContent accountId={100} />);
    // The status pill AND the legend both say NO ORDER SENT; the raw enum must NOT be shown as-is.
    expect((await screen.findAllByText(/NO ORDER SENT/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText("SHADOW_RECORDED")).toBeNull();
  });

  it("labels SHADOW_JOB_RECORDED as a shadow job with no order sent (not execution/success)", async () => {
    getCopyTradingOverview.mockResolvedValue(overview({ master: MASTER, recent_replications: [replication("SHADOW_JOB_RECORDED")] }));
    render(<CopyTradingContent accountId={100} />);
    expect((await screen.findAllByText(/SHADOW JOB RECORDED .* NO ORDER SENT/i)).length).toBeGreaterThan(0);
    expect(screen.queryByText("SHADOW_JOB_RECORDED")).toBeNull();   // raw enum never shown as-is
  });

  it("shows a calm not-enabled state when the API reports enabled:false (DARK)", async () => {
    getCopyTradingOverview.mockResolvedValue(overview({ enabled: false }));
    render(<CopyTradingContent accountId={100} />);
    expect(await screen.findByText(/not enabled for your account/i)).toBeTruthy();
  });

  it("shows a LIVE follower as execution-blocked, not tradeable", async () => {
    getCopyTradingOverview.mockResolvedValue(overview({ master: MASTER, followers: [liveFollower()] }));
    render(<CopyTradingContent accountId={100} />);
    expect(await screen.findByText(/LIVE execution blocked/i)).toBeTruthy();
  });

  it("stays DARK-safe (not-enabled) when the API call fails", async () => {
    getCopyTradingOverview.mockRejectedValue(new Error("404"));
    render(<CopyTradingContent accountId={100} />);
    expect(await screen.findByText(/not enabled for your account/i)).toBeTruthy();
  });
});

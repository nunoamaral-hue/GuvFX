import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ValidationTimelinePanel } from "@/components/broker/ValidationTimelinePanel";
import type { ValidationTimeline, ValidationTimelineStage } from "@/types/broker";

function stage(key: string, state: ValidationTimelineStage["state"], reason = ""): ValidationTimelineStage {
  return { key, operator_label: `${key} op`, customer_label: `${key} cust`, state, reason };
}

const IPC_TIMELINE: ValidationTimeline = {
  correlation_id: "validate-acct-13-a252c05b0463", found: true, attempt_id: 7, account_id: 13,
  status: "UNAVAILABLE", reason_code: "validation_ipc_unavailable", is_demo: null,
  server: "IS6Technologies-Demo", login_masked: "***587", trigger: "test",
  started_at: "2026-08-05T17:16:03Z", finished_at: "2026-08-05T17:18:20Z", duration_ms: 135819,
  stages: [
    stage("api_received", "ok"), stage("credential_decrypted", "ok"), stage("envelope_sealed", "ok"),
    stage("request_signed", "ok"), stage("agent_received", "ok"),
    stage("mt5_launched", "failed", "validation_ipc_unavailable"),
    stage("broker_login", "not_reached"), stage("broker_response", "not_reached"),
    stage("persisted", "ok"), stage("browser_response", "ok"),
  ],
  customer_summary: "We couldn't start the secure broker-validation session; try again later.",
  operator_summary: "Furthest stage reached: agent_received op. First failing stage: mt5_launched op (reason: validation_ipc_unavailable).",
};

describe("ValidationTimelinePanel", () => {
  it("renders the stage rail with the correlation id, attempt, and both summaries", () => {
    render(<ValidationTimelinePanel timeline={IPC_TIMELINE} />);
    expect(screen.getByText("validate-acct-13-a252c05b0463")).toBeInTheDocument();
    expect(screen.getByText(/Attempt #7/)).toBeInTheDocument();
    expect(screen.getByText(/Customer summary:/)).toBeInTheDocument();
    expect(screen.getByText(/Operator:/)).toBeInTheDocument();
  });

  it("shows a failed marker on the failing stage and 'not reached' markers after it", () => {
    render(<ValidationTimelinePanel timeline={IPC_TIMELINE} />);
    // 5 ok (api..agent) + 2 ok (persisted/browser) = 7 ok; 1 failed (mt5_launched); 2 not_reached
    expect(screen.getAllByLabelText("done").length).toBe(7);
    expect(screen.getAllByLabelText("failed").length).toBe(1);
    expect(screen.getAllByLabelText("not reached").length).toBe(2);
  });

  it("renders a friendly not-found state", () => {
    render(<ValidationTimelinePanel timeline={{ ...IPC_TIMELINE, found: false }} />);
    expect(screen.getByText(/No validation found for that search/i)).toBeInTheDocument();
  });

  it("shows the total duration in a human unit", () => {
    render(<ValidationTimelinePanel timeline={IPC_TIMELINE} />);
    expect(screen.getByText(/Total: 135\.8 s/)).toBeInTheDocument();
  });
});

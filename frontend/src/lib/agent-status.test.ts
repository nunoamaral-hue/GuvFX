import { describe, it, expect } from "vitest";
import {
  operatorAgentView,
  operatorStateLabel,
  alertSeverityView,
  customerAgentView,
  type CustomerAgentStatus,
} from "@/lib/agent-status";

describe("agent-status (WS-G)", () => {
  it("maps operator bands to colours", () => {
    expect(operatorAgentView("HEALTHY")).toEqual({ label: "Healthy", color: "green" });
    expect(operatorAgentView("DEGRADED")).toEqual({ label: "Degraded", color: "yellow" });
    expect(operatorAgentView("UNAVAILABLE")).toEqual({ label: "Unavailable", color: "red" });
    expect(operatorAgentView("garbage").color).toBe("gray");
  });

  it("labels operator fine-states (incl. the Aug-5 unsupervised state)", () => {
    expect(operatorStateLabel("UNSUPERVISED")).toBe("Unsupervised listener");
    expect(operatorStateLabel("HEALTHY")).toBe("Healthy");
    expect(operatorStateLabel("weird")).toBe("Unknown");
  });

  it("maps alert severities", () => {
    expect(alertSeverityView("HIGH").color).toBe("red");
    expect(alertSeverityView("MEDIUM").color).toBe("yellow");
    expect(alertSeverityView(undefined).color).toBe("blue");
  });

  it("customer view exposes ONLY availability, never internal detail", () => {
    const unavailable: CustomerAgentStatus = {
      available: false,
      status: "temporarily_unavailable",
      message: "Broker validation is temporarily unavailable. Please try again shortly — there is nothing you need to change.",
    };
    const view = customerAgentView(unavailable);
    // never surfaces a reason/state/supervision word to the customer
    const blob = JSON.stringify(view).toLowerCase();
    for (const forbidden of ["unsupervised", "unreachable", "negotiate", "supervis", "8791", "correlation"]) {
      expect(blob).not.toContain(forbidden);
    }
    expect(view.label).toBe("Temporarily unavailable");
    // never blames the customer
    expect(unavailable.message).toContain("nothing you need to change");
    expect(customerAgentView({ available: true, status: "available", message: "" }).label).toBe("Available");
  });
});

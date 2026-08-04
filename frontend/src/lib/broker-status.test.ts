import { describe, expect, it } from "vitest";
import {
  connectionView, healthStatusView, maskAccountNumber, reasonMessage, validationStatusView,
} from "@/lib/broker-status";

describe("validationStatusView", () => {
  it("maps every known status to a label + colour", () => {
    expect(validationStatusView("VALIDATED")).toEqual({ label: "Validated", color: "green" });
    expect(validationStatusView("CONNECTION_FAILED").color).toBe("red");
    expect(validationStatusView("TECHNICAL_ERROR").color).toBe("yellow");
    expect(validationStatusView("NEVER").label).toBe("Not validated");
  });
  it("falls back to Unknown for unrecognised/empty values", () => {
    expect(validationStatusView("GIBBERISH")).toEqual({ label: "Unknown", color: "gray" });
    expect(validationStatusView(null).label).toBe("Unknown");
    expect(validationStatusView(undefined).label).toBe("Unknown");
  });
});

describe("healthStatusView", () => {
  it("maps HEALTHY/NEEDS_ATTENTION/UNAVAILABLE", () => {
    expect(healthStatusView("HEALTHY").color).toBe("green");
    expect(healthStatusView("NEEDS_ATTENTION").color).toBe("yellow");
    expect(healthStatusView("UNAVAILABLE").color).toBe("red");
  });
  it("defaults to Unknown", () => {
    expect(healthStatusView(undefined).label).toBe("Unknown");
    expect(healthStatusView("weird").label).toBe("Unknown");
  });
});

describe("reasonMessage", () => {
  it("maps known reason codes to customer-safe wording", () => {
    expect(reasonMessage("invalid_password")).toMatch(/password was not accepted/i);
    expect(reasonMessage("demo_ok")).toMatch(/demo account verified/i);
  });
  it("never surfaces an unknown raw code — returns a generic message", () => {
    const out = reasonMessage("internal_stacktrace_x99");
    expect(out).not.toContain("internal_stacktrace_x99");
    expect(out).toMatch(/check your details/i);
  });
  it("returns empty for no code", () => {
    expect(reasonMessage("")).toBe("");
    expect(reasonMessage(null)).toBe("");
  });
});

describe("connectionView", () => {
  it("disconnected wins over active", () => {
    expect(connectionView(true, "2026-08-04T00:00:00Z").label).toBe("Disconnected");
    expect(connectionView(true, null).label).toBe("Connected");
    expect(connectionView(false, null).label).toBe("Inactive");
  });
});

describe("maskAccountNumber", () => {
  it("shows only the last 4 digits", () => {
    expect(maskAccountNumber("1302575")).toBe("••••2575");
    expect(maskAccountNumber("12")).toBe("••12");
    expect(maskAccountNumber("")).toBe("");
    expect(maskAccountNumber(null)).toBe("");
    expect(maskAccountNumber("1302575")).not.toContain("130");
  });
});

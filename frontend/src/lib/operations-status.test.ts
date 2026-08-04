import { describe, expect, it } from "vitest";
import {
  CATEGORIES, SEVERITIES, categoryView, credentialView, disconnectView,
  healthStateView, pauseView, resolutionView, severityView,
} from "@/lib/operations-status";

/** WP5.3 — the single vocabulary→view mapping. These tests lock the mapping AND the invariant that every
 * view uses only the shared 5-colour palette (no duplicated / off-palette colour mapping). */
const PALETTE = new Set(["green", "gray", "blue", "red", "yellow"]);

describe("severityView", () => {
  it("maps each severity to its label + palette colour", () => {
    expect(severityView("INFO")).toEqual({ label: "Info", color: "blue" });
    expect(severityView("WARNING")).toEqual({ label: "Warning", color: "yellow" });
    expect(severityView("ERROR")).toEqual({ label: "Error", color: "red" });
    expect(severityView("CRITICAL")).toEqual({ label: "Critical", color: "red" });
  });
  it("is case-insensitive and falls back to Unknown/gray", () => {
    expect(severityView("info")).toEqual({ label: "Info", color: "blue" });
    expect(severityView("")).toEqual({ label: "Unknown", color: "gray" });
    expect(severityView(null)).toEqual({ label: "Unknown", color: "gray" });
    expect(severityView("BOGUS")).toEqual({ label: "Unknown", color: "gray" });
  });
});

describe("categoryView", () => {
  it("maps every canonical category to a non-raw label", () => {
    for (const c of CATEGORIES) {
      const v = categoryView(c);
      expect(v.label).not.toBe(c); // never render the raw enum
      expect(PALETTE.has(v.color)).toBe(true);
    }
  });
  it("falls back to Unknown for out-of-vocab categories", () => {
    expect(categoryView("WAT")).toEqual({ label: "Unknown", color: "gray" });
    expect(categoryView(null)).toEqual({ label: "Unknown", color: "gray" });
  });
});

describe("resolutionView", () => {
  it("resolved → green Resolved; open → yellow Open", () => {
    expect(resolutionView(true)).toEqual({ label: "Resolved", color: "green" });
    expect(resolutionView(false)).toEqual({ label: "Open", color: "yellow" });
  });
});

describe("healthStateView", () => {
  it("maps known states; unavailable → Not observed", () => {
    expect(healthStateView("HEALTHY", true)).toEqual({ label: "Healthy", color: "green" });
    expect(healthStateView("DISCONNECTED", true)).toEqual({ label: "Disconnected", color: "red" });
    expect(healthStateView("STALE", true)).toEqual({ label: "Stale", color: "yellow" });
    expect(healthStateView("HEALTHY", false)).toEqual({ label: "Not observed", color: "gray" });
    expect(healthStateView("GONE", true)).toEqual({ label: "Unknown", color: "gray" });
  });
});

describe("boolean state views", () => {
  it("pause / credential / disconnect", () => {
    expect(pauseView(true)).toEqual({ label: "Paused", color: "red" });
    expect(pauseView(false)).toEqual({ label: "Active", color: "green" });
    expect(credentialView(true)).toEqual({ label: "On file", color: "green" });
    expect(credentialView(false)).toEqual({ label: "Missing", color: "gray" });
    expect(disconnectView(true)).toEqual({ label: "Disconnected", color: "red" });
    expect(disconnectView(false)).toEqual({ label: "Connected", color: "green" });
  });
});

describe("palette invariant", () => {
  it("every view produced by every mapper uses only the 5-colour palette", () => {
    const views = [
      ...SEVERITIES.map(severityView),
      ...CATEGORIES.map(categoryView),
      resolutionView(true), resolutionView(false),
      pauseView(true), pauseView(false),
      credentialView(true), credentialView(false),
      disconnectView(true), disconnectView(false),
      healthStateView("HEALTHY", true), healthStateView("DEGRADED", true),
      healthStateView("TOMBSTONED", true), healthStateView("X", false),
    ];
    for (const v of views) expect(PALETTE.has(v.color)).toBe(true);
  });
});

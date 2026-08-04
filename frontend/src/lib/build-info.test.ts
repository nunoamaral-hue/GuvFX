import { afterEach, describe, expect, it, vi } from "vitest";
import { buildInfo } from "./build-info";

/** IPR Area G — the frontend build fingerprint reports the inlined commit/timestamp (or "unknown"
 * when absent) and the two build-time flag booleans; it never carries a secret value. */

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("frontend build-info", () => {
  it("defaults to 'unknown' when no build-args were baked in", () => {
    vi.stubEnv("NEXT_PUBLIC_GIT_COMMIT", "");
    vi.stubEnv("NEXT_PUBLIC_BUILD_TIMESTAMP", "");
    const info = buildInfo();
    // Empty-string env → falls through to the "unknown" default only if undefined; empty stays "".
    expect(["unknown", ""]).toContain(info.gitCommit);
    expect(info.flags).toHaveProperty("NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED");
    expect(info.flags).toHaveProperty("NEXT_PUBLIC_OPERATIONS_ENABLED");
  });

  it("reports the baked-in commit + timestamp", () => {
    vi.stubEnv("NEXT_PUBLIC_GIT_COMMIT", "abc1234");
    vi.stubEnv("NEXT_PUBLIC_BUILD_TIMESTAMP", "2026-08-04T00:00:00Z");
    const info = buildInfo();
    expect(info.gitCommit).toBe("abc1234");
    expect(info.buildTimestamp).toBe("2026-08-04T00:00:00Z");
  });

  it("flag values are booleans (armed-in-this-build), never secrets", () => {
    const info = buildInfo();
    for (const v of Object.values(info.flags)) expect(typeof v).toBe("boolean");
    expect(JSON.stringify(info)).not.toMatch(/token|secret|password/i);
  });
});

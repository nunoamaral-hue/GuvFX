import { describe, expect, it } from "vitest";
// The build-info emitter is a build-time script (scripts/emit-build-info.mjs); its pure computation is
// unit-tested here. It bakes a static, self-surfacing /build-info.json — see IPR Area G.
import { computeBuildInfo } from "../../scripts/emit-build-info.mjs";

const FLAGS = ["NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED", "NEXT_PUBLIC_OPERATIONS_ENABLED"];

describe("computeBuildInfo (frontend build fingerprint)", () => {
  it("defaults to 'unknown' and OFF flags when nothing is set", () => {
    const info = computeBuildInfo({}, FLAGS);
    expect(info.gitCommit).toBe("unknown");
    expect(info.buildTimestamp).toBe("unknown");
    expect(info.flags).toEqual({
      NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED: false,
      NEXT_PUBLIC_OPERATIONS_ENABLED: false,
    });
  });

  it("reports the baked-in commit/timestamp and armed flag booleans", () => {
    const info = computeBuildInfo(
      {
        GIT_COMMIT: "abc1234",
        BUILD_TIMESTAMP: "2026-08-04T00:00:00Z",
        NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED: "1",
      },
      FLAGS,
    );
    expect(info.gitCommit).toBe("abc1234");
    expect(info.buildTimestamp).toBe("2026-08-04T00:00:00Z");
    expect(info.flags.NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED).toBe(true);
    expect(info.flags.NEXT_PUBLIC_OPERATIONS_ENABLED).toBe(false);
  });

  it("carries no secret-shaped value (only booleans + short identifiers)", () => {
    const info = computeBuildInfo({ GIT_COMMIT: "deadbeef" }, FLAGS);
    // Flags are booleans; commit/timestamp are the given/"unknown" values — no credential-shaped
    // `KEY=<value>` pairs anywhere in the payload.
    for (const v of Object.values(info.flags)) expect(typeof v).toBe("boolean");
    expect(JSON.stringify(info)).not.toMatch(/(?:token|secret|password|api[_-]?key)\s*[=:]\s*['"]?[\w-]{8,}/i);
  });
});

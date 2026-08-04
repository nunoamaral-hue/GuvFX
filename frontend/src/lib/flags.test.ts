import { afterEach, describe, expect, it, vi } from "vitest";
import { brokerConnectivityEnabled } from "@/lib/flags";

const FLAG = "NEXT_PUBLIC_BROKER_CONNECTIVITY_ENABLED";

afterEach(() => vi.unstubAllEnvs());

describe("brokerConnectivityEnabled", () => {
  it("is OFF by default (unset)", () => {
    vi.stubEnv(FLAG, "");
    expect(brokerConnectivityEnabled()).toBe(false);
  });
  it("accepts the truthy tokens", () => {
    for (const v of ["1", "true", "TRUE", "yes", "on"]) {
      vi.stubEnv(FLAG, v);
      expect(brokerConnectivityEnabled()).toBe(true);
    }
  });
  it("treats any other value as OFF (fail-safe)", () => {
    for (const v of ["0", "false", "maybe", "2", " "]) {
      vi.stubEnv(FLAG, v);
      expect(brokerConnectivityEnabled()).toBe(false);
    }
  });
});

import { describe, expect, it } from "vitest";
import { customerSafeError } from "./customer-safe-error";

describe("customerSafeError", () => {
  it("never exposes an unknown API or transport message", () => {
    expect(customerSafeError(new Error("network_unreachable"), "Please try again.")).toBe("Please try again.");
    expect(customerSafeError(new Error("WorkerIdentity claim failed"), "Please try again.")).toBe("Please try again.");
  });

  it("only returns explicitly reviewed mappings", () => {
    expect(customerSafeError(new Error("Token has expired."), "fallback", [
      { match: /expired/i, message: "Request a new code." },
    ])).toBe("Request a new code.");
  });
});

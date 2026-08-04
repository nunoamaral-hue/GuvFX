import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "./api";

/** IPR Area D — the arm flow depends on apiFetch surfacing the machine-readable parts of a 4xx body
 * (HTTP status + parsed body incl. the `status` slug) on the thrown error, not just a display string.
 * These tests pin that contract. */

function mockResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => "application/json" },
    text: async () => JSON.stringify(body),
    json: async () => body,
  } as unknown as Response;
}

let target: Response;

beforeEach(() => {
  target = mockResponse(200, {});
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: unknown) => {
      // CSRF preflight (POST) and refresh calls always succeed; the target call is per-test.
      if (String(url).includes("/api/auth/cookie/")) return mockResponse(200, {});
      return target;
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiFetch error metadata", () => {
  it("attaches httpStatus, parsed body, and the status slug on a 409", async () => {
    target = mockResponse(409, { status: "account_not_ready", detail: "Account must be demo and active." });
    await expect(
      apiFetch("/api/strategies/strategies/signal-copy/arm/", { method: "POST", body: "{}" }),
    ).rejects.toMatchObject({
      httpStatus: 409,
      status: 409,
      message: "Account must be demo and active.",
      body: { status: "account_not_ready" },
    });
  });

  it("distinguishes the two readiness reasons by status slug (identical detail)", async () => {
    target = mockResponse(409, { status: "runtime_not_ready", detail: "Account runtime is not ready to trade yet." });
    const err = await apiFetch("/api/x/", { method: "POST", body: "{}" }).catch((e) => e);
    expect((err as { body?: { status?: string } }).body?.status).toBe("runtime_not_ready");
  });

  it("exposes httpStatus 404 so callers can branch without string matching", async () => {
    target = mockResponse(404, { detail: "account not found" });
    const err = await apiFetch("/api/x/", { method: "POST", body: "{}" }).catch((e) => e);
    expect((err as { httpStatus?: number }).httpStatus).toBe(404);
    expect((err as { message?: string }).message).toBe("account not found");
  });

  it("preserves DRF field-error JSON in the message (unchanged for those callers)", async () => {
    target = mockResponse(400, { magic_number: ["bad"] });
    const err = await apiFetch("/api/x/", { method: "POST", body: "{}" }).catch((e) => e);
    expect((err as { message?: string }).message).toBe(JSON.stringify({ magic_number: ["bad"] }));
  });
});

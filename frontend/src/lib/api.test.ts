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
  vi.useRealTimers();
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

describe("apiFetch client deadline (timeoutMs)", () => {
  it("aborts a hanging request after the deadline and rejects as a tagged network error", async () => {
    vi.useFakeTimers();
    // The target call hangs until its abort signal fires (a slow/half-open upstream). Real fetch rejects on
    // abort — mirror that so the deadline path is exercised; the CSRF/refresh preflights still resolve.
    vi.stubGlobal(
      "fetch",
      vi.fn((url: unknown, init?: RequestInit) => {
        if (String(url).includes("/api/auth/cookie/")) return Promise.resolve(mockResponse(200, {}));
        return new Promise<Response>((_resolve, reject) => {
          const sig = init?.signal;
          if (sig) sig.addEventListener("abort", () => {
            const e = new Error("aborted") as Error & { name: string };
            e.name = "AbortError";
            reject(e);
          });
        });
      }),
    );
    const p = apiFetch("/api/analytics/portfolio/open-trades/?scope=ALL", {}, 5_000);
    const assertion = expect(p).rejects.toMatchObject({ kind: "network", message: "network_unreachable" });
    await vi.advanceTimersByTimeAsync(5_000);   // fire the deadline → abort → reject
    await assertion;
  });

  it("never fires the deadline when timeoutMs is omitted (unchanged behaviour)", async () => {
    target = mockResponse(200, { ok: true });
    await expect(apiFetch<{ ok: boolean }>("/api/x/")).resolves.toMatchObject({ ok: true });
  });

  it("a deadline-aborted read NEVER forces a login redirect — even if the upstream answers 401", async () => {
    // Regression for the R1 incident class: a slow fan-out read that the client aborts is a TRANSIENT failure,
    // not an invalid session. Even on the belt-and-suspenders path where the upstream answers 401 concurrently
    // with the abort, apiFetch must reject as network_unreachable and must NOT navigate to /login.
    vi.useFakeTimers();
    vi.stubGlobal(
      "fetch",
      vi.fn((url: unknown, init?: RequestInit) => {
        if (String(url).includes("/api/auth/cookie/")) return Promise.resolve(mockResponse(200, {}));
        const sig = init?.signal;
        // Upstream only ever answers 401, and does so as soon as the deadline aborts (immediately on the retry,
        // whose signal is already aborted).
        if (sig?.aborted) return Promise.resolve(mockResponse(401, { detail: "x" }));
        return new Promise<Response>((resolve) =>
          sig?.addEventListener("abort", () => resolve(mockResponse(401, { detail: "x" }))));
      }),
    );
    const p = apiFetch("/api/analytics/portfolio/summary/?scope=ALL", {}, 5_000);
    // The abort guard rejects as network_unreachable. The redirect path would instead throw Error("Unauthorized")
    // AFTER navigating to /login — so a network_unreachable rejection proves no login redirect was taken.
    const assertion = expect(p).rejects.toMatchObject({ kind: "network", message: "network_unreachable" });
    await vi.advanceTimersByTimeAsync(5_000);
    await assertion;
  });
});

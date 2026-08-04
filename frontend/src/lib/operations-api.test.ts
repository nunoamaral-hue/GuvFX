import { beforeEach, describe, expect, it, vi } from "vitest";

/** WP5.3 — the operations API client is READ-ONLY: one GET, correct query params, no write init. */
const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => ({ apiFetch }));

import { getAccountEvents } from "@/lib/operations-api";

beforeEach(() => {
  apiFetch.mockReset();
  apiFetch.mockResolvedValue({ summary: {}, timeline: [] });
});

const urlOf = () => String(apiFetch.mock.calls[0][0]);

describe("getAccountEvents", () => {
  it("builds the account-events GET with the account id", async () => {
    await getAccountEvents(7);
    expect(urlOf()).toBe("/api/operations/account-events/?account_id=7");
  });

  it("includes limit, offset, and the server-side category filter", async () => {
    await getAccountEvents(7, { limit: 50, offset: 100, category: "HEALTH" });
    const u = urlOf();
    expect(u).toContain("account_id=7");
    expect(u).toContain("limit=50");
    expect(u).toContain("offset=100");
    expect(u).toContain("category=HEALTH");
  });

  it("omits an empty/null category (never sends category=)", async () => {
    await getAccountEvents(7, { category: null });
    expect(urlOf()).not.toContain("category");
    apiFetch.mockClear();
    await getAccountEvents(7, { category: "" });
    expect(urlOf()).not.toContain("category");
  });

  it("is read-only — apiFetch is called with only a URL (no method/body init)", async () => {
    await getAccountEvents(7);
    expect(apiFetch.mock.calls[0]).toHaveLength(1);
  });
});

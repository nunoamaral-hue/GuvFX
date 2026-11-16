import { describe, it, expect } from "vitest";
import { deriveIngestionNoticeLevel } from "./MailboxIngestionNotice";

/** pkt-149 / ADR-0113 — the owner ingestion-notice derivation is truthful and additive:
 * reconnect wins over degraded, healthy/empty shows nothing, and it never invents a warning. */
describe("deriveIngestionNoticeLevel", () => {
  it("returns null for no mailboxes", () => {
    expect(deriveIngestionNoticeLevel([])).toBeNull();
    expect(deriveIngestionNoticeLevel(null)).toBeNull();
    expect(deriveIngestionNoticeLevel(undefined)).toBeNull();
  });

  it("returns null when every mailbox is healthy/connected", () => {
    expect(deriveIngestionNoticeLevel([{ state: "HEALTHY" }, { state: "CONNECTED" }])).toBeNull();
  });

  it("flags reconnect for RECONNECT_REQUIRED / REVOKED / the explicit flag", () => {
    expect(deriveIngestionNoticeLevel([{ state: "RECONNECT_REQUIRED" }])).toBe("reconnect");
    expect(deriveIngestionNoticeLevel([{ state: "REVOKED" }])).toBe("reconnect");
    expect(deriveIngestionNoticeLevel([{ state: "HEALTHY", reconnect_required: true }])).toBe("reconnect");
  });

  it("flags degraded for a late/slow mailbox", () => {
    expect(deriveIngestionNoticeLevel([{ state: "DEGRADED" }])).toBe("degraded");
  });

  it("prefers reconnect over degraded when both are present", () => {
    expect(deriveIngestionNoticeLevel([{ state: "DEGRADED" }, { state: "RECONNECT_REQUIRED" }])).toBe("reconnect");
  });

  it("does not warn on a healthy mailbox that simply has no new mail", () => {
    // A connected+synced mailbox with nothing new is HEALTHY — never a false degraded signal.
    expect(deriveIngestionNoticeLevel([{ state: "HEALTHY" }])).toBeNull();
  });
});

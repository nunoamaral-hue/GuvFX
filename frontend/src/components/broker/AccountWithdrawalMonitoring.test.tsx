/** Unmount-safety regression (WV1 final integration). The PR#531 frontend CI failed because a mount-time apiFetch in
 * AccountWithdrawalMonitoring resolved AFTER the test env tore down and its .then/.catch ran setState on an unmounted
 * component (AccountWithdrawalMonitoring.tsx:104, "window is not defined" → unhandled rejection → workflow exit 1).
 * The mountedRef guard makes those late callbacks no-ops. This test unmounts the component while its mount fetches are
 * still pending, then settles them (one resolve, one reject) and asserts nothing escapes — no console error, no
 * unhandled rejection, no throw. Without the guard the late .then/.catch would run setState after unmount. */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";

const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);

import { AccountWithdrawalMonitoring } from "./AccountWithdrawalMonitoring";

describe("AccountWithdrawalMonitoring — unmount safety", () => {
  beforeEach(() => api.apiFetch.mockReset());
  afterEach(() => cleanup());

  it("does not setState / throw when the mount fetches settle after unmount", async () => {
    // Two deferred promises the test settles AFTER unmount (contacts call, then mailbox-status call).
    let resolveContacts!: (v: unknown) => void;
    let rejectMailboxes!: (e: unknown) => void;
    api.apiFetch
      .mockImplementationOnce(() => new Promise((res) => { resolveContacts = res; }))
      .mockImplementationOnce(() => new Promise((_res, rej) => { rejectMailboxes = rej; }));

    const errSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    const unhandled: unknown[] = [];
    const onUnhandled = (e: PromiseRejectionEvent) => unhandled.push(e.reason);
    window.addEventListener("unhandledrejection", onUnhandled);

    const { unmount } = render(<AccountWithdrawalMonitoring accountId={12} brokerName="IS6" />);
    unmount();                                   // unmount BEFORE either fetch settles
    resolveContacts({ contacts: [] });           // late success → .then must early-return (no setContacts/setHidden)
    rejectMailboxes(new Error("late failure"));  // late failure → .catch must early-return (no setMailboxes)
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));

    window.removeEventListener("unhandledrejection", onUnhandled);
    const logged = errSpy.mock.calls.flat().map(String).join(" ");
    expect(logged).not.toMatch(/unmount|not wrapped in act|window is not defined/i);
    expect(unhandled).toHaveLength(0);
    errSpy.mockRestore();
  });
});

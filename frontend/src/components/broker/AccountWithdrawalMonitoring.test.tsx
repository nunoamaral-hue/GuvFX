/** Unmount-safety regression (WV1 final integration). The PR#531 frontend CI failed because a mount-time apiFetch in
 * AccountWithdrawalMonitoring resolved AFTER the test env tore down and its .then/.catch ran setState on an unmounted
 * component. The mountedRef guard makes those late callbacks no-ops.
 *
 * This test DISCRIMINATES the guard (React 19 silently no-ops setState-after-unmount and emits no console warning, so
 * asserting on console.error / unhandledrejection would pass with OR without the fix — a fake). Instead it wraps
 * React's useState so every state setter is recorded; after unmount it settles the two pending mount fetches and
 * asserts NO setter is invoked. Without the guard the late .then/.catch call setContacts/setHidden/setMailboxes and
 * the assertion fails; with the guard the `if (!mountedRef.current) return;` early-return runs first and it passes.
 * (Verified by reverting AccountWithdrawalMonitoring.tsx to the pre-guard version: this test fails there.) */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";

const rec = vi.hoisted(() => ({ recording: false, calls: [] as string[] }));
const api = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("@/lib/api", () => api);
// Wrap the exported useState so each dispatch is observable. react-dom/testing-library use the internal dispatcher,
// not this export, so only the component-under-test's setState calls are recorded.
vi.mock("react", async (orig) => {
  const actual = await orig<typeof import("react")>();
  const wrapUseState = ((init: unknown) => {
    const [v, s] = (actual.useState as (i: unknown) => [unknown, (...a: unknown[]) => void])(init);
    return [v, (...a: unknown[]) => { if (rec.recording) rec.calls.push("setState-after-unmount"); return s(...a); }];
  }) as typeof actual.useState;
  return { ...actual, default: { ...actual.default, useState: wrapUseState }, useState: wrapUseState };
});

import { AccountWithdrawalMonitoring } from "./AccountWithdrawalMonitoring";

describe("AccountWithdrawalMonitoring — unmount safety", () => {
  beforeEach(() => { api.apiFetch.mockReset(); rec.recording = false; rec.calls.length = 0; });
  afterEach(() => cleanup());

  it("invokes NO setState after unmount when the mount fetches settle late", async () => {
    let resolveContacts!: (v: unknown) => void;
    let rejectMailboxes!: (e: unknown) => void;
    api.apiFetch
      .mockImplementationOnce(() => new Promise((res) => { resolveContacts = res; }))        // contacts
      .mockImplementationOnce(() => new Promise((_res, rej) => { rejectMailboxes = rej; }));  // mailbox status

    const { unmount } = render(<AccountWithdrawalMonitoring accountId={12} brokerName="IS6" />);
    rec.recording = true;                         // from here, any state setter call is a post-unmount call
    unmount();
    resolveContacts({ contacts: [] });            // late success → guard must early-return (no setContacts/setHidden)
    rejectMailboxes(new Error("late failure"));   // late failure → guard must early-return (no setMailboxes)
    await Promise.resolve();
    await Promise.resolve();
    await new Promise((r) => setTimeout(r, 0));
    rec.recording = false;

    expect(rec.calls).toEqual([]);                // the mountedRef guard prevented every late setState
  });
});

import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { LanguageProvider } from "@/components/AppShell";

/**
 * ADR-0034 Customer-Zero keyboard-input corrective (RemoteApp focus management).
 *
 * Guacamole's keyboard handler listens on the embedded iframe's OWN document, so keystrokes only reach MT5
 * while the iframe holds DOM focus (mouse works without focus; keyboard does not — the "mouse works / keyboard
 * dead" symptom). These tests lock in the fix:
 *   1. the RemoteApp iframe is keyboard-focusable (tabIndex 0) and focus is forwarded to it on load and on a
 *      pointer-down over the terminal card;
 *   2. focusing NEVER remounts the iframe (no reconnect loop) — the src/key are stable across focus events;
 *   3. the hosted owner still never sees the legacy full desktop (primary security invariant), and the legacy
 *      "Available Terminals" list is suppressed;
 *   4. no key/telemetry handler is attached to the iframe (no keylogging surface).
 */

const { apiFetch } = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock("@/lib/api", () => ({ apiFetch }));
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/trading/terminal-access",
  useSearchParams: () => new URLSearchParams(),
}));
vi.mock("next/link", () => ({ default: ({ children }: { children: React.ReactNode }) => <>{children}</> }));

// Route apiFetch by URL to drive the hosted-owner RemoteApp path; everything else is benign/empty.
function routeApi(url: string) {
  if (url.startsWith("/api/trading/accounts/")) {
    return Promise.resolve([{ id: 1, name: "IS6 Demo", account_number: "1302561", is_active: true }]);
  }
  if (url.startsWith("/api/hosted-workspace/delivery-state/")) {
    return Promise.resolve({ is_owner: true });
  }
  if (url.startsWith("/api/hosted-workspace/delivery-connect/")) {
    return Promise.resolve({
      transport_type: "rdp_remoteapp",
      embed_url: "https://guvfx.com/guacamole/#/client/abc?data=xyz",
      session_token: "",
      expiry: null,
    });
  }
  // Legacy/other surfaces — resolve to inert values so their effects settle without error.
  if (url.startsWith("/api/mt5-interaction/sessions/active")) return Promise.resolve(null);
  if (url.startsWith("/api/mt5-interaction/terminal-bindings")) return Promise.resolve([]);
  if (url.startsWith("/api/mt5/status")) return Promise.resolve(null);
  if (url.startsWith("/api/mt5/desktop-link")) return Promise.resolve(null);
  if (url.startsWith("/api/reliability/trading-health")) return Promise.resolve(null);
  return Promise.resolve(null);
}

import TerminalAccessPage from "./page";

async function renderConnected(lang: "en" | "ja" = "en") {
  const focusSpy = vi.spyOn(HTMLIFrameElement.prototype, "focus").mockImplementation(() => {});
  render(<LanguageProvider lang={lang}><TerminalAccessPage /></LanguageProvider>);
  // Hosted detection resolves is_owner -> the RemoteApp card offers "Open MT5 Terminal".
  const openBtn = await screen.findByRole("button", { name: lang === "ja" ? "MT5ターミナルを開く" : /open mt5 terminal/i });
  focusSpy.mockClear();
  fireEvent.click(openBtn);
  const iframe = (await screen.findByTitle(lang === "ja" ? "MT5ターミナル" : "MT5 Terminal")) as HTMLIFrameElement;
  return { iframe, focusSpy };
}

describe("RemoteApp keyboard focus management", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    apiFetch.mockImplementation((url: string) => routeApi(url));
  });

  it("renders a keyboard-focusable RemoteApp iframe and forwards focus on load", async () => {
    const { iframe, focusSpy } = await renderConnected();
    expect(iframe.getAttribute("tabindex")).toBe("0");
    expect(iframe.getAttribute("sandbox")).toBe("allow-same-origin allow-scripts allow-forms allow-popups");
    // Clipboard Permissions-Policy so Guacamole can read the local clipboard for browser->MT5 paste.
    expect(iframe.getAttribute("allow")).toBe("clipboard-read; clipboard-write");
    // onLoad focuses the iframe so the first keystroke reaches Guacamole without a click.
    fireEvent.load(iframe);
    expect(focusSpy).toHaveBeenCalled();
  });

  it("forwards focus to the iframe on a pointer-down over the terminal card", async () => {
    const { iframe, focusSpy } = await renderConnected();
    focusSpy.mockClear();
    // The card wrapper is the iframe's grandparent (card > header/iframe wrapper). Pointer-down anywhere on it
    // must focus the iframe (capture-phase handler).
    const card = iframe.parentElement as HTMLElement;
    fireEvent.pointerDown(card);
    expect(focusSpy).toHaveBeenCalled();
  });

  it("does not remount/reconnect the iframe when focus events fire (stable src + key)", async () => {
    const { iframe } = await renderConnected();
    const srcBefore = iframe.getAttribute("src");
    fireEvent.load(iframe);
    fireEvent.pointerDown(iframe.parentElement as HTMLElement);
    const iframeAfter = screen.getByTitle("MT5 Terminal") as HTMLIFrameElement;
    // Same node identity + same src => React did not remount it (no reconnect loop).
    expect(iframeAfter).toBe(iframe);
    expect(iframeAfter.getAttribute("src")).toBe(srcBefore);
  });

  it("maximizes and restores the same iframe without reconnecting the RemoteApp session", async () => {
    const { iframe } = await renderConnected();
    const srcBefore = iframe.getAttribute("src");
    const connectCallsBefore = apiFetch.mock.calls.filter(([url]) => String(url).includes("delivery-connect")).length;

    fireEvent.click(screen.getByRole("button", { name: "Full Screen" }));
    expect(screen.getByRole("button", { name: "Exit Full Screen" })).toBeInTheDocument();
    expect(iframe.closest("[data-terminal-maximized='true']")).toBeTruthy();
    expect(screen.getByTitle("MT5 Terminal")).toBe(iframe);
    expect(iframe.getAttribute("src")).toBe(srcBefore);

    fireEvent.click(screen.getByRole("button", { name: "Exit Full Screen" }));
    expect(screen.getByRole("button", { name: "Full Screen" })).toBeInTheDocument();
    expect(iframe.closest("[data-terminal-maximized='false']")).toBeTruthy();
    expect(screen.getByTitle("MT5 Terminal")).toBe(iframe);
    const connectCallsAfter = apiFetch.mock.calls.filter(([url]) => String(url).includes("delivery-connect")).length;
    expect(connectCallsAfter).toBe(connectCallsBefore);
  });

  it("renders the full-screen controls in Japanese", async () => {
    await renderConnected("ja");
    fireEvent.click(screen.getByRole("button", { name: "全画面表示" }));
    expect(screen.getByRole("button", { name: "全画面表示を終了" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Exit Full Screen" })).toBeNull();
  });

  it("keeps the exit control and same session available at the 390px beta viewport", async () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    try {
      const { iframe } = await renderConnected("ja");
      fireEvent.click(screen.getByRole("button", { name: "全画面表示" }));
      const shell = iframe.closest("[data-terminal-maximized='true']") as HTMLElement;
      expect(shell).toBeTruthy();
      expect(shell.style.width).toBe("100vw");
      expect(shell.style.height).toBe("100dvh");
      expect(screen.getByRole("button", { name: "全画面表示を終了" })).toBeVisible();
      expect(screen.getByTitle("MT5ターミナル")).toBe(iframe);
    } finally {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: originalWidth });
    }
  });

  it("keeps the hosted owner on RemoteApp only — no legacy desktop / Available Terminals", async () => {
    await renderConnected();
    expect(screen.queryByText(/Available Terminals/i)).toBeNull();
    expect(screen.queryByRole("button", { name: /launch mt5 desktop/i })).toBeNull();
  });

  it("attaches no key/telemetry handlers to the RemoteApp iframe (no keylogging surface)", async () => {
    const { iframe } = await renderConnected();
    // React does NOT reflect event handlers as DOM attributes (root delegation + __reactProps$ fiber storage),
    // so getAttribute("onkeydown") is always null and would be a vacuous guard. Read the actual React props
    // off the fiber so this test genuinely fails if a future edit adds a keyboard handler to the iframe.
    const propsKey = Object.keys(iframe).find((k) => k.startsWith("__reactProps$"));
    expect(propsKey, "React fiber props key not found on iframe").toBeTruthy();
    const props = (iframe as unknown as Record<string, Record<string, unknown>>)[propsKey as string];
    for (const handler of ["onKeyDown", "onKeyUp", "onKeyPress", "onKeyDownCapture", "onBeforeInput", "onInput"]) {
      expect(props[handler], `iframe must not attach ${handler}`).toBeUndefined();
    }
  });
});

import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a>,
}));

import PrivacyPage from "./page";

afterEach(() => cleanup());
// Language is forced via the cookie (detectLang checks the cookie first). This avoids the
// Node-vs-jsdom global `localStorage` shadow that is flaky in some local runs.
beforeEach(() => {
  document.cookie = "guvfx_lang=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

describe("public /privacy policy", () => {
  it("renders without authentication and states the Gmail read-only + never-withdrawal guarantees (EN)", () => {
    render(<PrivacyPage />); // no auth provider — proves the route is public
    expect(screen.getByRole("heading", { level: 1, name: "Privacy Policy" })).toBeTruthy();
    // GuvFX-never-acts guarantee
    expect(screen.getByTestId("privacy-never-statement").textContent).toMatch(
      /never initiates, confirms, cancels or moves a withdrawal/i,
    );
    // minimum read-only scope named exactly
    expect(screen.getAllByText(/www\.googleapis\.com\/auth\/gmail\.readonly/).length).toBeGreaterThan(0);
    // Google Limited Use disclosure present
    expect(screen.getByTestId("privacy-limited-use-heading").textContent).toMatch(/Limited Use/);
    expect(screen.getAllByText(/Limited Use requirements/).length).toBeGreaterThan(0);
    // deterministic, no-human / no-LLM processing claim (audited true vs implementation)
    expect(screen.getAllByText(/no large language model or other AI processes your email/i).length).toBeGreaterThan(0);
    // encrypted-off-DB token handling claim (audited true vs credential_store.py)
    expect(screen.getAllByText(/encrypted and stored separately from our application database/i).length).toBeGreaterThan(0);
    // disconnect + deletion path
    expect(screen.getAllByText(/myaccount\.google\.com/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/support@guvfx\.com/).length).toBeGreaterThan(0);
    // unresolved legal fields are clearly flagged for Sponsor review, not invented
    expect(screen.getByTestId("privacy-draft-notice").textContent).toMatch(/draft pending legal review/i);
    expect(screen.getAllByText(/Sponsor to confirm/).length).toBeGreaterThan(0);
  });

  it("localizes to Japanese", () => {
    document.cookie = "guvfx_lang=ja; path=/";
    render(<PrivacyPage />);
    expect(screen.getByRole("heading", { level: 1, name: "プライバシーポリシー" })).toBeTruthy();
    expect(screen.getByTestId("privacy-never-statement").textContent).toMatch(
      /GuvFXが出金を開始・確認・取消・送金することはありません/,
    );
    expect(screen.getByTestId("privacy-limited-use-heading").textContent).toMatch(/限定使用/);
  });
});

import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a>,
}));

import TermsPage from "./page";

afterEach(() => cleanup());
// Language is forced via the cookie (detectLang checks the cookie first). This avoids the
// Node-vs-jsdom global `localStorage` shadow that is flaky in some local runs.
beforeEach(() => {
  document.cookie = "guvfx_lang=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
});

describe("public /terms", () => {
  it("renders without authentication and states no-advice + never-withdrawal + draft status (EN)", () => {
    render(<TermsPage />); // no auth provider — proves the route is public
    expect(screen.getByRole("heading", { level: 1, name: "Terms of Service" })).toBeTruthy();
    expect(screen.getByTestId("terms-never-statement").textContent).toMatch(
      /never initiates, confirms, cancels or moves a withdrawal/i,
    );
    expect(screen.getAllByText(/does not provide investment, financial, legal or tax advice/i).length).toBeGreaterThan(0);
    // read-only email connection described
    expect(screen.getAllByText(/read-only Gmail access only/i).length).toBeGreaterThan(0);
    // contractual facts not invented — flagged for Sponsor review
    expect(screen.getByTestId("terms-draft-notice").textContent).toMatch(/draft pending legal review/i);
    // approved UK values applied; liability resolved with conservative standard wording; effective date set
    expect(screen.getAllByText(/governed by the laws of England and Wales/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/total liability for any claim is limited to the amount/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/24 October 2026/).length).toBeGreaterThan(0);
    // corporate operator resolved; personal/residential wording must NOT be published
    expect(screen.getAllByText(/Virtus Logic UK Ltd/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/13318959/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Nuno Rodolfo Ferreira Do Amaral/)).toBeNull();
    expect(screen.getAllByText(/support@guvfx\.com/).length).toBeGreaterThan(0);
  });

  it("localizes to Japanese", () => {
    document.cookie = "guvfx_lang=ja; path=/";
    render(<TermsPage />);
    expect(screen.getByRole("heading", { level: 1, name: "利用規約" })).toBeTruthy();
    expect(screen.getByTestId("terms-never-statement").textContent).toMatch(
      /GuvFXが出金を開始・確認・取消・送金することはありません/,
    );
  });
});

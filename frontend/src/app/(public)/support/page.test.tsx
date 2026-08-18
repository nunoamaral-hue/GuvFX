import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a>,
}));

import SupportPage from "./page";

describe("beta support route", () => {
  beforeEach(() => localStorage.setItem("guvfx_lang", "ja"));

  it("provides a Japanese, actionable and secret-safe recovery path", () => {
    render(<SupportPage />);
    expect(screen.getByRole("heading", { name: "どのようなことでお困りですか？" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "サポートにメール" }).getAttribute("href")).toContain("mailto:support@guvfx.com");
    expect(screen.getByText(/パスワード/)).toBeTruthy();
    expect(screen.getByRole("link", { name: "マイ戦略に戻る" }).getAttribute("href")).toBe("/strategies");
  });
});

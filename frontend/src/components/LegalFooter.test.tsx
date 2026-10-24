import { render, screen, cleanup, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a>,
}));

import { LegalFooter } from "./LegalFooter";

afterEach(() => cleanup());

describe("LegalFooter legal links", () => {
  it("links to /privacy and /terms (EN labels)", () => {
    render(<LegalFooter lang="en" />);
    const footer = screen.getByRole("contentinfo");
    const privacy = within(footer).getByRole("link", { name: "Privacy Policy" });
    const terms = within(footer).getByRole("link", { name: "Terms of Service" });
    expect(privacy.getAttribute("href")).toBe("/privacy");
    expect(terms.getAttribute("href")).toBe("/terms");
  });

  it("localizes the legal links (JA labels, same hrefs)", () => {
    render(<LegalFooter lang="ja" />);
    const footer = screen.getByRole("contentinfo");
    expect(within(footer).getByRole("link", { name: "プライバシーポリシー" }).getAttribute("href")).toBe("/privacy");
    expect(within(footer).getByRole("link", { name: "利用規約" }).getAttribute("href")).toBe("/terms");
  });
});

import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/navigation", () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock("@/components/LegalFooter", () => ({ LegalFooter: () => null }));
vi.mock("@/components/LanguageDropdown", () => ({ LanguageDropdown: () => null }));

import RegisterPage from "./page";

describe("registration language and mobile-critical fields", () => {
  beforeEach(() => {
    document.cookie = "guvfx_lang=ja; path=/";
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
  });

  it("renders every registration field label in Japanese", () => {
    render(<RegisterPage />);
    expect(screen.getByLabelText("メールアドレス")).toBeTruthy();
    expect(screen.getByLabelText("名")).toBeTruthy();
    expect(screen.getByLabelText("姓")).toBeTruthy();
    expect(screen.getByLabelText("パスワード")).toBeTruthy();
    expect(screen.getByLabelText("ユーザー名（任意）")).toBeTruthy();
    expect(screen.queryByText("First name")).toBeNull();
    expect(screen.queryByText("Last name")).toBeNull();
  });
});

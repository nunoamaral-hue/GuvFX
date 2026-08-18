import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ push: vi.fn(), apiFetch: vi.fn() }));
vi.mock("next/navigation", () => ({ useRouter: () => ({ push: mocks.push }) }));
vi.mock("@/lib/api", () => ({ apiFetch: mocks.apiFetch }));
vi.mock("@/components/LegalFooter", () => ({ LegalFooter: () => null }));
vi.mock("@/components/LanguageDropdown", () => ({ LanguageDropdown: () => null }));

import LoginPage from "./page";

async function submitLogin() {
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "beta@example.test" } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: "secret123" } });
  fireEvent.click(screen.getByRole("button", { name: "Continue" }));
  await act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

describe("login return journey", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.push.mockReset();
    mocks.apiFetch.mockReset();
    window.history.replaceState({}, "", "/login");
    localStorage.setItem("guvfx_lang", "en");
  });
  afterEach(() => vi.useRealTimers());

  it("resumes the durable setup route after login", async () => {
    mocks.apiFetch.mockImplementation(async (path: string) =>
      path.includes("setup-status") ? { next_route: "/onboarding/hosted" } : { ok: true });
    render(<LoginPage />);
    await submitLogin();
    expect(mocks.apiFetch).toHaveBeenCalledWith("/api/onboarding/setup-status/");
    await act(async () => { await vi.advanceTimersByTimeAsync(700); });
    expect(mocks.push).toHaveBeenCalledWith("/onboarding/hosted");
  });

  it("preserves a validated explicit return destination", async () => {
    window.history.replaceState({}, "", "/login?returnTo=%2Fstrategies%2Fmarketplace");
    mocks.apiFetch.mockResolvedValue({ ok: true });
    render(<LoginPage />);
    await act(async () => { await Promise.resolve(); });
    await submitLogin();
    expect(mocks.apiFetch).not.toHaveBeenCalledWith("/api/onboarding/setup-status/");
    await act(async () => { await vi.advanceTimersByTimeAsync(700); });
    expect(mocks.push).toHaveBeenCalledWith("/strategies/marketplace");
  });

  it("does not display raw login errors", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    mocks.apiFetch.mockRejectedValue(new Error("WorkerIdentity claim failed"));
    render(<LoginPage />);
    await submitLogin();
    expect(screen.getByText("Login failed. Please check your credentials.")).toBeTruthy();
    expect(screen.queryByText(/WorkerIdentity/)).toBeNull();
  });
});

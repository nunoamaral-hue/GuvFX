/** Withdrawal Intelligence page — composes the owner-scoped sections + persistent evidence/empty copy. */
import React from "react";
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";

const h = vi.hoisted(() => ({ lang: "en" as "en" | "ja" }));
vi.mock("@/components/AppShell", () => ({ useLang: () => h.lang }));
vi.mock("@/components/accounts/ConnectedEmailAccounts", () => ({
  ConnectedEmailAccounts: () => <div data-testid="m-connected" />,
}));
vi.mock("@/components/dashboard/WithdrawalsPanel", () => ({
  WithdrawalsPanel: () => <div data-testid="m-panel" />,
}));
vi.mock("@/components/dashboard/WithdrawalsHistory", () => ({
  WithdrawalsHistory: () => <div data-testid="m-history" />,
}));

import WithdrawalIntelligencePage from "./page";

afterEach(() => { cleanup(); h.lang = "en"; });

describe("WithdrawalIntelligencePage", () => {
  it("renders the genuine feature chrome: title, the never-initiates statement, empty prompt, evidence limits", () => {
    render(<WithdrawalIntelligencePage />);
    expect(screen.getByText("Withdrawal Intelligence")).toBeTruthy();
    // GuvFX-never-acts guarantee is prominent
    expect(screen.getByText(/never initiates, confirms, cancels or moves a withdrawal/i)).toBeTruthy();
    // prescribed empty / onboarding copy
    expect(screen.getByText(/No withdrawal activity has been observed yet\. Connect your supported email account/i)).toBeTruthy();
    // evidence limitation: broker-reported completion is not receipt confirmation
    expect(screen.getByText(/Broker-reported completion is not independent confirmation of fund receipt/i)).toBeTruthy();
    // account-unresolved explanation present
    expect(screen.getByText(/Account unresolved:/i)).toBeTruthy();
  });

  it("composes the owner-scoped sections (connection, overview, history)", () => {
    render(<WithdrawalIntelligencePage />);
    expect(screen.getByTestId("m-connected")).toBeTruthy();
    expect(screen.getByTestId("m-panel")).toBeTruthy();
    expect(screen.getByTestId("m-history")).toBeTruthy();
  });

  it("shows no demonstration/synthetic banner in the genuine production page chrome", () => {
    render(<WithdrawalIntelligencePage />);
    expect(screen.queryByText(/DEMONSTRATION/i)).toBeNull();
    expect(screen.queryByText(/synthetic/i)).toBeNull();
  });

  it("localizes to Japanese", () => {
    h.lang = "ja";
    render(<WithdrawalIntelligencePage />);
    expect(screen.getByText("出金インテリジェンス")).toBeTruthy();
    expect(screen.getByText(/GuvFXが出金を開始・確認・取消・送金することはありません/)).toBeTruthy();
  });
});

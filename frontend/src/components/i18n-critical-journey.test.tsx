import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "@/components/AppShell";
import { EnableStrategyModal } from "@/components/strategy/EnableStrategyModal";
import { HostedWorkspaceStatus } from "@/components/accounts/HostedWorkspaceStatus";
import { EmailVerificationStep } from "@/components/onboarding/steps/EmailVerificationStep";
import { TwoFactorStep } from "@/components/onboarding/steps/TwoFactorStep";
import { RiskAcceptanceStep } from "@/components/onboarding/steps/RiskAcceptanceStep";
import type { HostedJourney } from "@/lib/hosted-journey";
import type { OnboardingState } from "@/types/onboarding";

vi.mock("next/link", () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => <a href={href}>{children}</a>,
}));

const readyJourney = {
  phase: "WORKSPACE_READY",
  delivery: "DELIVERY_READY",
  strategy_eligible: true,
  execution_armed: false,
  can_enable_automated_trading: true,
  active_login_masked: "•••587",
} as HostedJourney;

describe("critical beta journey in Japanese", () => {
  it("renders meaningful Japanese consent copy and actions", () => {
    render(
      <LanguageProvider lang="ja">
        <EnableStrategyModal
          open accountLabel="デモ口座 (1302587)" strategyName="Wayond WIM"
          busy={false} error={null} onConfirm={vi.fn()} onCancel={vi.fn()}
        />
      </LanguageProvider>,
    );
    expect(screen.getByRole("dialog", { name: "自動売買を有効にする" })).toBeTruthy();
    expect(screen.getByText("自動売買を有効にしますか？")).toBeTruthy();
    expect(screen.getByRole("button", { name: "戦略を有効にする" })).toBeTruthy();
    expect(screen.queryByText("Enable Strategy")).toBeNull();
  });

  it("renders the ready account and automated-trading state in Japanese", () => {
    render(
      <LanguageProvider lang="ja">
        <HostedWorkspaceStatus
          journey={readyJourney}
          accounts={[{ id: 25, name: "デモ口座", account_number: "1302587", is_active: true, is_demo: true }]}
        />
      </LanguageProvider>,
    );
    expect(screen.getByText("ホステッドワークスペース")).toBeTruthy();
    expect(screen.getByText("自動売買を有効にする")).toBeTruthy();
    expect(screen.getByText("戦略を選ぶ →")).toBeTruthy();
    expect(screen.queryByText("Enable automated trading")).toBeNull();
  });

  it("renders the current email-verification step without English leakage", () => {
    render(
      <LanguageProvider lang="ja">
        <EmailVerificationStep
          state={{ email_verified: false } as OnboardingState}
          onComplete={vi.fn()}
        />
      </LanguageProvider>,
    );
    expect(screen.getByRole("heading", { name: "メールアドレスを確認" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "確認コードを送信" })).toBeTruthy();
    expect(screen.queryByText("Verify Your Email")).toBeNull();
  });

  it("renders the optional two-factor step without English leakage", () => {
    render(
      <LanguageProvider lang="ja">
        <TwoFactorStep
          state={{ two_factor_enabled: false } as OnboardingState}
          onComplete={vi.fn()}
          onSkip={vi.fn()}
        />
      </LanguageProvider>,
    );
    expect(screen.getByRole("heading", { name: "二要素認証" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "二要素認証を設定" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "今はスキップ" })).toBeTruthy();
    expect(screen.queryByText("Two-Factor Authentication")).toBeNull();
  });

  it("renders the risk consent and disclosure in reviewed Japanese", () => {
    render(
      <LanguageProvider lang="ja">
        <RiskAcceptanceStep
          state={{ risk_accepted: false } as OnboardingState}
          onComplete={vi.fn()}
        />
      </LanguageProvider>,
    );
    expect(screen.getByRole("heading", { name: "リスクに関する重要事項" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "リスクを理解し、同意します" })).toBeTruthy();
    expect(screen.queryByText("Risk Disclosure")).toBeNull();
  });
});

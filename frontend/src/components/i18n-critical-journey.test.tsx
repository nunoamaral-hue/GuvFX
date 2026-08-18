import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "@/components/AppShell";
import { EnableStrategyModal } from "@/components/strategy/EnableStrategyModal";
import { HostedWorkspaceStatus } from "@/components/accounts/HostedWorkspaceStatus";
import type { HostedJourney } from "@/lib/hosted-journey";

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
    expect(screen.getByText("ホステッド・ワークスペース")).toBeTruthy();
    expect(screen.getByText("自動売買を有効にする")).toBeTruthy();
    expect(screen.getByText("戦略を選ぶ →")).toBeTruthy();
    expect(screen.queryByText("Enable automated trading")).toBeNull();
  });
});

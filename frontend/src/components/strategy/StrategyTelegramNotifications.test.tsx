import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { StrategyTelegramNotifications } from "./StrategyTelegramNotifications";

const api = vi.hoisted(() => ({
  getStrategyNotificationSettings: vi.fn(),
  updateStrategyNotificationSettings: vi.fn(),
}));

vi.mock("@/lib/customer-notifications", () => api);

describe("StrategyTelegramNotifications", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getStrategyNotificationSettings.mockResolvedValue({
      assignment_id: 12,
      enabled: false,
      pending_enable: false,
      telegram_connected: true,
    });
  });

  it("changes only the assignment notification preference", async () => {
    api.updateStrategyNotificationSettings.mockResolvedValue({
      assignment_id: 12,
      enabled: true,
      pending_enable: false,
      telegram_connected: true,
    });
    render(<StrategyTelegramNotifications assignmentId={12} lang="en" />);
    fireEvent.click(await screen.findByRole("checkbox", { name: "Enable Telegram notifications for this strategy" }));
    await waitFor(() => expect(api.updateStrategyNotificationSettings).toHaveBeenCalledWith(12, true));
    expect(screen.getByRole("checkbox")).toBeChecked();
  });

  it("shows an intentional connect CTA instead of a failing toggle for a disconnected customer", async () => {
    api.getStrategyNotificationSettings.mockResolvedValue({
      assignment_id: 12,
      enabled: false,
      pending_enable: false,
      telegram_connected: false,
    });
    render(<StrategyTelegramNotifications assignmentId={12} lang="en" />);
    expect(await screen.findByText("Connect Telegram to enable notifications.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Connect Telegram" })).toHaveAttribute(
      "href", "/profile#telegram-notifications",
    );
    // No inert/confusing toggle is rendered while disconnected — the CTA replaces it.
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("renders the Japanese customer control", async () => {
    render(<StrategyTelegramNotifications assignmentId={12} lang="ja" />);
    expect(await screen.findByRole("checkbox", { name: "このストラテジーのTelegram通知を有効にする" })).toBeInTheDocument();
  });
});

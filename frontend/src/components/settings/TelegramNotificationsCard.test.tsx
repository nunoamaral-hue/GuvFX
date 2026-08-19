import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { LanguageProvider } from "@/components/AppShell";
import { TelegramNotificationsCard } from "./TelegramNotificationsCard";

const api = vi.hoisted(() => ({
  getTelegramSettings: vi.fn(),
  createTelegramConnection: vi.fn(),
  disconnectTelegram: vi.fn(),
  updateTelegramPreferences: vi.fn(),
}));

vi.mock("@/lib/customer-notifications", () => api);

const connected = (language: "en" | "ja" = "en") => ({
  available: true,
  connected: true,
  display: { username: "guvfx_customer", first_name: "Customer" },
  preferences: {
    telegram_enabled: true,
    trade_opened: true,
    trade_updated: true,
    trade_closed: true,
    strategy_changed: true,
    execution_problem: true,
    workspace_ready: true,
    language,
  },
});

describe("TelegramNotificationsCard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getTelegramSettings.mockResolvedValue(connected());
    api.updateTelegramPreferences.mockResolvedValue(connected());
    api.disconnectTelegram.mockResolvedValue({ ...connected(), connected: false, display: { username: "", first_name: "" } });
  });

  it("renders the connected English preference contract without a chat id", async () => {
    render(<LanguageProvider lang="en"><TelegramNotificationsCard /></LanguageProvider>);
    expect(await screen.findByText("Connected")).toBeInTheDocument();
    expect(screen.getByText("Trade opened")).toBeInTheDocument();
    expect(screen.getByText("Trade updates")).toBeInTheDocument();
    expect(screen.getByText("Trading needs attention")).toBeInTheDocument();
    expect(screen.queryByText(/chat.?id/i)).not.toBeInTheDocument();
  });

  it("renders native Japanese notification copy", async () => {
    api.getTelegramSettings.mockResolvedValue(connected("ja"));
    render(<LanguageProvider lang="ja"><TelegramNotificationsCard /></LanguageProvider>);
    expect(await screen.findByText("接続済み")).toBeInTheDocument();
    expect(screen.getByText("取引開始")).toBeInTheDocument();
    expect(screen.getByText("ストラテジーの有効化・無効化")).toBeInTheDocument();
    expect(screen.getByText("ワークスペース準備完了")).toBeInTheDocument();
  });

  it("persists the selected app language without requiring another preference change", async () => {
    api.getTelegramSettings.mockResolvedValue(connected("en"));
    api.updateTelegramPreferences.mockResolvedValue(connected("ja"));
    render(<LanguageProvider lang="ja"><TelegramNotificationsCard /></LanguageProvider>);
    expect(await screen.findByText("接続済み")).toBeInTheDocument();
    await waitFor(() => expect(api.updateTelegramPreferences).toHaveBeenCalledWith({ language: "ja" }));
  });

  it("persists an individual preference without changing the master", async () => {
    render(<LanguageProvider lang="en"><TelegramNotificationsCard /></LanguageProvider>);
    await screen.findByText("Connected");
    const boxes = screen.getAllByRole("checkbox");
    fireEvent.click(boxes[3]); // Trade closed (master is index 0).
    await waitFor(() => expect(api.updateTelegramPreferences).toHaveBeenCalledWith({
      trade_closed: false,
      language: "en",
    }));
  });

  it("shows a simple not-connected state", async () => {
    api.getTelegramSettings.mockResolvedValue({ ...connected(), connected: false, display: { username: "", first_name: "" } });
    render(<LanguageProvider lang="en"><TelegramNotificationsCard /></LanguageProvider>);
    expect(await screen.findByText("Not connected")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Connect Telegram" })).toBeEnabled();
  });

  it("shows a dark unavailable state without an actionable connect control", async () => {
    api.getTelegramSettings.mockResolvedValue({
      ...connected(), available: false, connected: false,
      display: { username: "", first_name: "" },
    });
    render(<LanguageProvider lang="en"><TelegramNotificationsCard /></LanguageProvider>);
    expect(await screen.findByText("Telegram connection is not available yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect Telegram" })).not.toBeInTheDocument();
  });

  it("shows the connecting state after opening the private-bot handshake", async () => {
    api.getTelegramSettings.mockResolvedValue({ ...connected(), connected: false, display: { username: "", first_name: "" } });
    api.createTelegramConnection.mockResolvedValue({
      url: "https://t.me/GuvFXCustomerBot?start=opaque-token",
      expires_at: "2026-08-19T13:00:00Z",
    });
    const open = vi.spyOn(window, "open").mockReturnValue({} as Window);
    render(<LanguageProvider lang="en"><TelegramNotificationsCard /></LanguageProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Connect Telegram" }));
    expect(await screen.findByLabelText("Connecting…")).toBeInTheDocument();
    expect(open).toHaveBeenCalledWith(
      "https://t.me/GuvFXCustomerBot?start=opaque-token", "_blank", "noopener,noreferrer",
    );
    open.mockRestore();
  });
});

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
    winning_trades: true,
    losing_trades: false,
    tp_progress: true,
    system_messages: true,
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
    expect(screen.queryByText("Trade opened")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Notification preferences" }));
    expect(screen.getByRole("dialog", { name: "Notification preferences" })).toBeInTheDocument();
    expect(screen.getByText("Winning trades")).toBeInTheDocument();
    expect(screen.getByText("Losing trades")).toBeInTheDocument();
    expect(screen.getByText("Take-profit progress")).toBeInTheDocument();
    expect(screen.getByText("System/account messages")).toBeInTheDocument();
    expect(screen.queryByText(/trade opened/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/chat.?id/i)).not.toBeInTheDocument();
  });

  it("renders native Japanese notification copy", async () => {
    api.getTelegramSettings.mockResolvedValue(connected("ja"));
    render(<LanguageProvider lang="ja"><TelegramNotificationsCard /></LanguageProvider>);
    expect(await screen.findByText("接続済み")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "通知設定" }));
    expect(screen.getByText("利益になった取引")).toBeInTheDocument();
    expect(screen.getByText("損失になった取引")).toBeInTheDocument();
    expect(screen.getByText("テイクプロフィットの進捗")).toBeInTheDocument();
    expect(screen.queryByText("取引開始")).not.toBeInTheDocument();
  });

  it("persists the selected app language without requiring another preference change", async () => {
    api.getTelegramSettings.mockResolvedValue(connected("en"));
    api.updateTelegramPreferences.mockResolvedValue(connected("ja"));
    render(<LanguageProvider lang="ja"><TelegramNotificationsCard /></LanguageProvider>);
    expect(await screen.findByText("接続済み")).toBeInTheDocument();
    await waitFor(() => expect(api.updateTelegramPreferences).toHaveBeenCalledWith({ language: "ja" }));
  });

  it("persists an individual preference from the modal", async () => {
    render(<LanguageProvider lang="en"><TelegramNotificationsCard /></LanguageProvider>);
    await screen.findByText("Connected");
    fireEvent.click(screen.getByRole("button", { name: "Notification preferences" }));
    fireEvent.click(screen.getByRole("checkbox", { name: /Winning trades/ }));
    await waitFor(() => expect(api.updateTelegramPreferences).toHaveBeenCalledWith({
      winning_trades: false,
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

  it("opens a placeholder tab synchronously then points it at the deep link (never navigates GuvFX away)", async () => {
    api.getTelegramSettings.mockResolvedValue({ ...connected(), connected: false, display: { username: "", first_name: "" } });
    api.createTelegramConnection.mockResolvedValue({
      url: "https://t.me/GuvFXCustomerBot?start=opaque-token",
      expires_at: "2026-08-19T13:00:00Z",
    });
    const stub = { location: { href: "" }, opener: {}, closed: false } as unknown as Window;
    const open = vi.spyOn(window, "open").mockReturnValue(stub);
    render(<LanguageProvider lang="en"><TelegramNotificationsCard /></LanguageProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Connect Telegram" }));
    expect(await screen.findByLabelText("Connecting…")).toBeInTheDocument();
    // A placeholder tab is opened synchronously inside the gesture (popup-blocker-safe),
    // then redirected to the deep link — GuvFX itself is never navigated away.
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    await waitFor(() => expect((stub.location as { href: string }).href).toBe(
      "https://t.me/GuvFXCustomerBot?start=opaque-token",
    ));
    open.mockRestore();
  });

  it("falls back to a direct new-tab open when the placeholder is popup-blocked (still never navigates away)", async () => {
    api.getTelegramSettings.mockResolvedValue({ ...connected(), connected: false, display: { username: "", first_name: "" } });
    api.createTelegramConnection.mockResolvedValue({
      url: "https://t.me/GuvFXCustomerBot?start=opaque-token",
      expires_at: "2026-08-19T13:00:00Z",
    });
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    render(<LanguageProvider lang="en"><TelegramNotificationsCard /></LanguageProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Connect Telegram" }));
    expect(await screen.findByLabelText("Connecting…")).toBeInTheDocument();
    // Placeholder blocked → best-effort direct new-tab open; NEVER window.location.assign.
    await waitFor(() => expect(open).toHaveBeenCalledWith(
      "https://t.me/GuvFXCustomerBot?start=opaque-token", "_blank", "noopener,noreferrer",
    ));
    open.mockRestore();
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LanguageProvider } from "@/components/AppShell";
import { WorkspaceReadyNotifyControl } from "./HostedWorkspaceJourney";

const api = vi.hoisted(() => ({
  getWorkspaceReadinessSettings: vi.fn(),
  requestWorkspaceReadinessNotification: vi.fn(),
}));

vi.mock("@/lib/customer-notifications", () => api);

const waiting = {
  available: true,
  has_workspace: true,
  requested: false,
  fulfilled: false,
  workspace_ready: false,
  telegram_connected: true,
};

describe("WorkspaceReadyNotifyControl", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.getWorkspaceReadinessSettings.mockResolvedValue(waiting);
  });

  it("persists the connected one-shot readiness request", async () => {
    api.requestWorkspaceReadinessNotification.mockResolvedValue({ ...waiting, requested: true });
    render(<LanguageProvider lang="en"><WorkspaceReadyNotifyControl /></LanguageProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "Notify me when it's ready" }));
    await waitFor(() => expect(api.requestWorkspaceReadinessNotification).toHaveBeenCalledWith("en"));
    expect(await screen.findByText("We'll notify you once this workspace is ready.")).toBeInTheDocument();
  });

  it("starts secure Telegram connection while preserving intent", async () => {
    api.requestWorkspaceReadinessNotification.mockResolvedValue({
      ...waiting,
      requested: true,
      telegram_connected: false,
      connect_url: "https://t.me/GuvFXCustomerBot?start=opaque-token",
    });
    const open = vi.spyOn(window, "open").mockReturnValue({} as Window);
    render(<LanguageProvider lang="ja"><WorkspaceReadyNotifyControl /></LanguageProvider>);
    fireEvent.click(await screen.findByRole("button", { name: "準備ができたら通知する" }));
    await waitFor(() => expect(open).toHaveBeenCalledWith(
      "https://t.me/GuvFXCustomerBot?start=opaque-token", "_blank", "noopener,noreferrer",
    ));
    expect(await screen.findByText("Telegramの接続を完了してください。1回限りの通知リクエストは保存されています。")).toBeInTheDocument();
    open.mockRestore();
  });

  it("does not offer readiness notifications without a workspace", async () => {
    api.getWorkspaceReadinessSettings.mockResolvedValue({ ...waiting, has_workspace: false });
    const { container } = render(
      <LanguageProvider lang="en"><WorkspaceReadyNotifyControl /></LanguageProvider>,
    );
    await waitFor(() => expect(api.getWorkspaceReadinessSettings).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});

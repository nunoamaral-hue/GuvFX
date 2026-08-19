import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { LanguageProvider } from "@/components/AppShell";
import { LocalizedBetaSurface } from "@/components/i18n/LocalizedBetaSurface";
import {
  getActiveBetaCatalogue,
  localizeBackendCustomerText,
  localizeControlledEnum,
} from "@/lib/active-beta-i18n";

const ACTIVE_ROUTE_EXPECTATIONS: Record<string, readonly string[]> = {
  "/dashboard": ["Here's your edge today.", "Trading", "Performance Trend", "Market Focus", "Your Strategies", "Opportunity Radar", "Key Events", "Research Evidence"],
  "/strategies/marketplace": ["Breakout", "Trend", "Reversion", "Patterns", "System-grade"],
  "/trading/live-trading": ["Demo Trading Available", "Run Demo Trade"],
  "/strategies/create": ["2) Symbols", "3) Market & timeframe", "5) Indicators & patterns", "7) Risk limits & trade management", "Create strategy"],
  "/strategies/:id": ["Daily R:", "Trades today:", "Paused until"],
  "/backtests": ["Research Mode — results are simulated, not live execution", "Parameters"],
  "/trading/terminal-access": ["Active Session", "Reconnect viewer", "Session details"],
  "/trading/trade-history": ["Daily PnL & Win Rate (Observed)", "No daily data for this period."],
  "/analytics/strategy-metrics": ["Strategy Metrics", "By Strategy", "No metrics yet."],
  "/analytics/strategy-lab": ["Strategy Lab", "Market State & Strategy Selection", "Trade Quality", "Research Knowledge Base"],
  "/charts": ["Market charts and visualisation.", "Open Terminal Access"],
  "/account/billing": ["Billing & Plans", "Subscription details", "Platform capabilities", "Change Plan"],
  "/account/invoices": ["View your billing invoices.", "No invoices available yet."],
  "/account/usage": ["Monitor your platform usage and resource consumption."],
  "/profile": ["Account Details", "Change Password", "Update password"],
};

function Surface({ lang, accountId = "A-1302587" }: { lang: "en" | "ja"; accountId?: string }) {
  return (
    <LanguageProvider lang={lang}>
      <LocalizedBetaSurface lang={lang}>
        <main data-account-id={accountId}>
          <h1>Good evening, Nuno 👋</h1>
          <p>Here&apos;s your edge today.</p>
          <section aria-label="Market Focus">
            <h2>Performance Trend</h2>
            <button title="Research direction only. Not a recommendation to trade.">Research →</button>
          </section>
          <iframe title="MT5 Terminal" src="https://viewer.invalid/session/immutable" />
        </main>
      </LocalizedBetaSurface>
    </LanguageProvider>
  );
}

describe("active closed-beta EN/JA rendering", () => {
  it("keeps an explicit non-empty JA catalogue for every migrated active route", () => {
    const catalogue = getActiveBetaCatalogue();
    for (const [route, entries] of Object.entries(ACTIVE_ROUTE_EXPECTATIONS)) {
      for (const english of entries) {
        expect(catalogue[english], `${route}: ${english}`).toBeTruthy();
        expect(catalogue[english], `${route}: ${english}`).not.toBe(english);
      }
    }
  });

  it("renders dashboard primary chrome in Japanese without the known English labels", () => {
    const { container } = render(<Surface lang="ja" />);
    expect(screen.getByRole("heading", { name: "こんばんは、Nunoさん 👋" })).toBeTruthy();
    expect(screen.getByRole("region", { name: "注目市場" })).toBeTruthy();
    expect(screen.getByTitle("調査の方向性のみを示し、取引を推奨するものではありません。")).toBeTruthy();
    const customerText = container.textContent || "";
    for (const english of ["Good evening", "Here's your edge today.", "Performance Trend", "Market Focus", "Research direction only"]) {
      expect(customerText).not.toContain(english);
    }
  });

  it("maps controlled values and fails closed for unknown backend prose", () => {
    expect(localizeControlledEnum("ja", "marketState", "NEWS_SHOCK")).toBe("ニュースによる急変");
    expect(localizeControlledEnum("ja", "status", "brand_new_state")).toBe("不明");
    expect(localizeBackendCustomerText("ja", "Uncatalogued English warning from API", "warning"))
      .toBe("現在の調査には追加確認が必要な注意事項があります。");
    expect(localizeBackendCustomerText("ja", "Uncatalogued English rationale from API", "research"))
      .toBe("この項目には追加の調査情報があります。");
    expect(localizeBackendCustomerText("en", "Original API detail", "error")).toBe("Original API detail");
  });

  it("switches EN to JA in place without changing account data or recreating MT5", async () => {
    const view = render(<Surface lang="en" />);
    const mainBefore = screen.getByRole("main");
    const iframeBefore = screen.getByTitle("MT5 Terminal");
    expect(screen.getByText("Here's your edge today.")).toBeTruthy();
    expect(document.documentElement.lang).toBe("en");

    view.rerender(<Surface lang="ja" />);
    await waitFor(() => expect(document.documentElement.lang).toBe("ja"));
    expect(screen.getByText("今日の取引リサーチを確認しましょう。")).toBeTruthy();
    expect(screen.getByRole("main")).toBe(mainBefore);
    expect(screen.getByRole("main").getAttribute("data-account-id")).toBe("A-1302587");
    expect(screen.getByTitle("MT5ターミナル")).toBe(iframeBefore);
    expect((iframeBefore as HTMLIFrameElement).src).toBe("https://viewer.invalid/session/immutable");
  });

  it("leaves English complete and preserves intentional identifiers", () => {
    render(<Surface lang="en" accountId="ACC-42" />);
    expect(screen.getByText("Here's your edge today.")).toBeTruthy();
    expect(screen.getByTitle("MT5 Terminal")).toBeTruthy();
    expect(screen.getByRole("main").getAttribute("data-account-id")).toBe("ACC-42");
  });
});

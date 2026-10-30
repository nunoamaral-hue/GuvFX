import { describe, expect, it } from "vitest";
import { realizedMoneyView } from "@/lib/realized-fx";
import type { PortfolioMetrics } from "@/types/portfolio";

// A single GBP account's summary metrics AFTER conversion (ADR-0066): money fields are USD, basis USD, fx_* disclose.
function convertedGbp(over: Partial<PortfolioMetrics> = {}): PortfolioMetrics {
  return {
    total_trades: 6, wins: 4, losses: 2, breakeven: 0, win_rate_pct: 66.67,
    profit_factor: 1.339, profit_factor_infinite: false, expectancy: 4.04, net_pnl_total: 24.23,
    gross_profit: 95, gross_loss: 71, max_drawdown_money: 71.57, breakeven_rule: "",
    basis: "USD", excluded_accounts: [], state: "OK", reason: null, included_count: 1, excluded_count: 0,
    fx_converted: true, fx_currency: "GBP", fx_rate: 1.3301, fx_rate_as_of: "2026-10-28",
    fx_rate_source: "BOE:XUDLUSS rev1", ...over,
  };
}

describe("realizedMoneyView (ADR-0066 single-account realized FX)", () => {
  it("#49 positive GBP: primary USD, secondary native GBP, BoE disclosure", () => {
    const v = realizedMoneyView(convertedGbp(), 18.22, "GBP");
    expect(v.netPnl).toBe(24.23);            // USD primary
    expect(v.currency).toBe("");             // "" -> "$"
    expect(v.nativeNetPnl).toBe(18.22);      // native secondary
    expect(v.unavailable).toBe(false);
    expect(v.fx).toEqual({ ccy: "GBP", rate: 1.3301, asOf: "2026-10-28", source: "BoE" });
  });

  it("#52 negative GBP converts with sign preserved", () => {
    const v = realizedMoneyView(convertedGbp({ net_pnl_total: -27.95, win_rate_pct: 0, profit_factor: 0 }), -21.01, "GBP");
    expect(v.netPnl).toBe(-27.95);
    expect(v.nativeNetPnl).toBe(-21.01);
    expect(v.fx?.rate).toBe(1.3301);
    expect(v.fx?.asOf).toBe("2026-10-28");
  });

  it("a new closed trade changes the converted + native values (pass-through of fresh payload)", () => {
    const v = realizedMoneyView(convertedGbp({ net_pnl_total: 30.0, total_trades: 7 }), 22.55, "GBP");
    expect(v.netPnl).toBe(30.0);
    expect(v.nativeNetPnl).toBe(22.55);
  });

  it("rate/source/effective-date come from the payload, and FED_H10 maps to 'Fed H.10'", () => {
    const v = realizedMoneyView(convertedGbp({ fx_rate: 1.2987, fx_rate_as_of: "2026-10-24", fx_rate_source: "FED_H10:DEXUSUK rev2" }), 10, "GBP");
    expect(v.fx).toEqual({ ccy: "GBP", rate: 1.2987, asOf: "2026-10-24", source: "Fed H.10" });
  });

  it("unavailable rate (non-USD, not converted): native shown + unavailable flag, no fabricated fx", () => {
    const m = convertedGbp({ fx_converted: false, state: "UNAVAILABLE", net_pnl_total: null });
    delete (m as Partial<PortfolioMetrics>).fx_rate;
    const v = realizedMoneyView(m, 17.06, "GBP");
    expect(v.netPnl).toBe(17.06);            // native
    expect(v.currency).toBe("GBP");
    expect(v.nativeNetPnl).toBeNull();
    expect(v.fx).toBeNull();
    expect(v.unavailable).toBe(true);
  });

  it("fx_converted true but no numeric rate -> NOT converted (no blank-rate disclosure); falls to native unavailable", () => {
    const m = convertedGbp();
    delete (m as Partial<PortfolioMetrics>).fx_rate;   // malformed: converted flag set, rate absent
    const v = realizedMoneyView(m, 17.06, "GBP");
    expect(v.fx).toBeNull();                 // never emit "using BoE GBP/USD <blank>"
    expect(v.netPnl).toBe(17.06);            // honest native instead
    expect(v.currency).toBe("GBP");
    expect(v.unavailable).toBe(true);
  });

  it("USD account is unchanged (native == USD, '$', no disclosure, not unavailable)", () => {
    const m = convertedGbp({ fx_converted: false, fx_currency: undefined, fx_rate: undefined, net_pnl_total: 163.92 });
    const v = realizedMoneyView(m, 163.92, "USD");
    expect(v.netPnl).toBe(163.92);
    expect(v.currency).toBe("");             // USD -> "$"
    expect(v.fx).toBeNull();
    expect(v.unavailable).toBe(false);
  });

  it("no metrics yet (loading) falls back to native, no crash", () => {
    const v = realizedMoneyView(null, null, "GBP");
    expect(v.netPnl).toBeNull();
    expect(v.unavailable).toBe(true);        // non-USD, not converted
    expect(v.fx).toBeNull();
  });
});

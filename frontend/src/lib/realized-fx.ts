/** ADR-0066 — single-account realized-P&L currency view.
 *
 * The portfolio summary CONVERTS a single non-USD account's realized metrics to USD via the governed prior-day rate
 * (fx_converted). The dashboard's per-account performance strip historically read the account's NATIVE trade-history
 * only, so it showed native GBP with no BoE disclosure. This pure helper decides what the strip should render for a
 * single account: the converted USD primary + native secondary + conversion disclosure when a vetted rate applied,
 * the honest native + "conversion unavailable" when a non-USD account has no vetted rate, or the plain native figure
 * for a USD account. Language-agnostic: it returns the rate/currency/date/source; the component formats them.
 *
 * ALL scope never uses this (that aggregate stays PARTIAL and excludes non-USD realized — unchanged).
 */
import type { PortfolioMetrics } from "@/types/portfolio";

export type RealizedMoneyView = {
  /** Primary realized net P/L: converted USD when a vetted rate applied, otherwise the native amount. */
  netPnl: number | null;
  /** Currency code for the PRIMARY figure: "" => USD ("$"); otherwise the native code (e.g. "GBP"). */
  currency: string;
  /** Secondary native amount, present ONLY when converted (so the UI can show e.g. "GBP 17.06" beside the USD). */
  nativeNetPnl: number | null;
  /** Conversion disclosure, present ONLY when converted. ``source`` is the human feed name (e.g. "BoE"). */
  fx: { ccy: string; rate: number | null; asOf: string; source: string } | null;
  /** True for a non-USD account whose realized P&L could NOT be converted (no vetted rate) — show it native + a
   *  "conversion unavailable" note, never a fabricated/ungoverned conversion. */
  unavailable: boolean;
};

function humanSource(raw: string): string {
  const head = (raw || "").split(":")[0];
  return head === "BOE" ? "BoE" : head === "FED_H10" ? "Fed H.10" : head;
}

/**
 * @param scopedMetrics the single account's summary metrics (``portfolioForScope.aggregate.metrics``), or null.
 * @param acctNetPnl    the account's NATIVE realized net P/L (from the per-account trade-history endpoint).
 * @param perfCurrency  the account's native currency code (from the per-account trade-history), "" treated as USD.
 */
export function realizedMoneyView(
  scopedMetrics: PortfolioMetrics | null | undefined,
  acctNetPnl: number | null | undefined,
  perfCurrency: string | null | undefined,
): RealizedMoneyView {
  const native = acctNetPnl ?? null;
  const ccy = (perfCurrency || "").toUpperCase();
  const converted = !!scopedMetrics && scopedMetrics.fx_converted === true && typeof scopedMetrics.net_pnl_total === "number";
  if (converted) {
    return {
      netPnl: scopedMetrics!.net_pnl_total,           // USD
      currency: "",                                   // -> "$"
      nativeNetPnl: native,                           // native secondary
      fx: {
        ccy: scopedMetrics!.fx_currency || ccy || "",
        rate: scopedMetrics!.fx_rate ?? null,
        asOf: scopedMetrics!.fx_rate_as_of || "",
        source: humanSource(scopedMetrics!.fx_rate_source || ""),
      },
      unavailable: false,
    };
  }
  // Not converted: a USD account renders native-as-USD unchanged; a non-USD account renders native + "unavailable".
  return {
    netPnl: native,
    currency: ccy === "USD" ? "" : (perfCurrency || ""),
    nativeNetPnl: null,
    fx: null,
    unavailable: !!ccy && ccy !== "USD",
  };
}

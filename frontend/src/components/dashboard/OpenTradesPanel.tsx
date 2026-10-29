"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { OpenTradesResult, OpenTrade } from "@/types/portfolio";

/** OPEN TRADES — read-only, live-refreshing monitoring panel (replaces the old "Your Strategies" card).
 *
 * Monitoring ONLY: it renders open positions and their floating P/L; it exposes NO close/modify/SL/TP/partial
 * action and calls only the read-only portfolio open-trades endpoint. It polls every 30s with:
 *   • single-flight (never two concurrent fetches);
 *   • scope-race safety (a response for a superseded scope can never overwrite the current scope);
 *   • unmount cleanup (interval cleared, in-flight result ignored);
 *   • no blank/flash on refresh — values update in place; a transient failure keeps the last-known data and shows
 *     a restrained "stale" indicator rather than erasing valid data;
 *   • pause while the tab is hidden.
 * The list has a fixed height (~5 rows) and scrolls internally so the page never jumps on refresh.
 */
const POLL_MS = 30_000;
const PATH = "/api/analytics/portfolio/open-trades/";
// Client-side deadline for the open-trades read. The backend fan-out is bounded server-side (portfolio
// aggregation budget), but a slow/half-open upstream must never leave this panel on "Loading positions…"
// forever — on timeout the fetch aborts, the catch marks it stale, and the panel settles to "unavailable".
const FETCH_TIMEOUT_MS = 18_000;

type Lang = "en" | "ja";
const STR = {
  title: { en: "Open Trades", ja: "オープン取引" },
  open: { en: (n: number) => `${n} open`, ja: (n: number) => `${n} 件` },
  floating: { en: "Floating P/L", ja: "含み損益" },
  none: { en: "No open trades", ja: "オープン取引はありません" },
  loading: { en: "Loading positions…", ja: "ポジションを読み込み中…" },
  unavailable: { en: "Positions unavailable", ja: "ポジションを取得できません" },
  partial: { en: "partial", ja: "一部" },
  // Truthful wording (Sponsor direction): a stale account's live POSITIONS could not be read; the account itself
  // may be reachable (its balance/equity read can succeed) — so this is "live positions unavailable", not the
  // broader, misleading "not reachable".
  staleAccts: { en: (n: number) => `${n} account${n === 1 ? "" : "s"}: live positions unavailable`, ja: (n: number) => `${n}口座: ポジション取得不可` },
  showing: { en: (x: number, y: number) => `Showing ${x} of ${y}`, ja: (x: number, y: number) => `${y}件中${x}件を表示` },
  updated: { en: (s: number) => `Updated ${s}s ago`, ja: (s: number) => `${s}秒前に更新` },
  reconnecting: { en: "reconnecting", ja: "再接続中" },
  waiting: { en: "…", ja: "…" },
  listLabel: { en: "Open trades list", ja: "オープン取引リスト" },
};

function money(n: number | null | undefined, currency = "USD"): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const sign = n > 0 ? "+" : n < 0 ? "−" : "";
  const abs = Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  // USD renders with a leading "$"; any other currency is labelled with its code (e.g. "+12.45 GBP") so a
  // non-USD account's figure is never presented without a currency, nor mislabelled as USD.
  return currency === "USD" ? `${sign}$${abs}` : `${sign}${abs} ${currency}`;
}

const card: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 14, padding: "1.1rem 1.2rem",
  background: "rgba(12,18,40,0.6)",
};
const rowStyle: React.CSSProperties = {
  display: "grid", gridTemplateColumns: "minmax(120px,1.4fr) 1fr auto", gap: 10, alignItems: "center",
  padding: "8px 0", borderTop: "1px solid rgba(255,255,255,0.06)", flexShrink: 0,
};
const meta: React.CSSProperties = { fontSize: "0.72rem", color: "#8fa0b7" };
// Truncate long broker/account/symbol values so a row can never widen or wrap the tile taller.
const trunc: React.CSSProperties = { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" };
const emptyState: React.CSSProperties = { ...meta, padding: "18px 0", textAlign: "center", margin: "auto" };

export const OpenTradesPanel: React.FC<{ scope: string; lang?: Lang }> = ({ scope, lang = "en" }) => {
  const L = lang === "ja" ? "ja" : "en";
  const [data, setData] = useState<OpenTradesResult | null>(null);
  const [stale, setStale] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [, force] = useState(0);

  const genRef = useRef(0);         // single-flight + scope-race token
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const inFlight = useRef(false);

  const load = useCallback(async (force = false) => {
    // Single-flight for the 30s POLL (skip an overlapping tick); a scope change forces a load so a pending
    // old-scope fetch never blocks the new scope (its late response is dropped by the gen/scope guards below).
    if (inFlight.current && !force) return;
    const gen = ++genRef.current;
    const forScope = scope;
    inFlight.current = true;
    try {
      const res = await apiFetch<OpenTradesResult>(`${PATH}?scope=${encodeURIComponent(scope)}`, {}, FETCH_TIMEOUT_MS);
      // Drop if superseded (a newer load/scope started) OR the scope changed under us (race safety).
      if (gen !== genRef.current || scopeRef.current !== forScope) return;
      if (res && Array.isArray(res.trades)) {
        setData(res);
        setStale(false);
        setUpdatedAt(Date.now());
      } else {
        setStale(true);              // keep last-known data; mark stale
      }
    } catch {
      if (gen === genRef.current && scopeRef.current === forScope) setStale(true);
    } finally {
      inFlight.current = false;
    }
  }, [scope]);

  // Reset visible data on a scope change so a stale-scope render can't linger, then load.
  useEffect(() => {
    setData(null);
    setStale(false);
    setUpdatedAt(null);
    void load(true);                 // force: a scope change must supersede any in-flight old-scope fetch
  }, [scope, load]);

  // 30s polling, paused while hidden; interval cleared on unmount.
  useEffect(() => {
    const tick = () => {
      if (typeof document !== "undefined" && document.hidden) return;
      void load();
    };
    const id = setInterval(tick, POLL_MS);
    return () => clearInterval(id);
  }, [load]);

  // Tick the "updated Xs ago" label every 5s without refetching.
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), 5_000);
    return () => clearInterval(id);
  }, []);

  const trades: OpenTrade[] = data?.trades ?? [];
  const floating = data?.open_pl_usd;
  // Single non-USD account scope: render the floating total in the account's OWN native currency (sum of the
  // per-position native P/L) instead of asserting a USD figure the NullFxRateSource cannot produce. For ALL / USD
  // scope keep the honest USD aggregate (null → "—", never "$0.00 USD"). Fixes a GBP account showing "$0.00 USD".
  const reportingCcy = data?.reporting_currency || "USD";
  const singleNonUsd = scope !== "ALL" && reportingCcy !== "USD";
  const nativeTotal = singleNonUsd ? trades.reduce((s, t) => s + (t.pl_native ?? 0), 0) : null;
  const floatTotalForColor = singleNonUsd ? nativeTotal : (floating?.total_usd ?? null);
  const agoSec = updatedAt ? Math.max(0, Math.round((Date.now() - updatedAt) / 1000)) : null;
  // M4: never-loaded (mount / just after a scope change) is NOT the same as a confirmed empty result.
  const loading = data === null && !stale;                 // fetch in flight, nothing to show yet
  const failedFirst = data === null && stale;              // first fetch for this scope failed, no prior data
  const fullCount = data?.count ?? trades.length;          // full open count (list may be capped server-side)
  const staleAccts = data?.stale_accounts?.length ?? 0;    // accounts whose positions could not be read (M1)
  const truncated = !!data?.truncated;                     // row list capped (M3)

  return (
    <div style={{ ...card, flex: "1 1 auto", minWidth: 0 }} className="open-trades-tile" data-testid="open-trades-panel">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 6, flexShrink: 0 }}>
        <div style={{ color: "#e9f4ff", fontSize: "0.95rem", fontWeight: 600 }}>{STR.title[L]}</div>
        <div style={meta}>{STR.open[L](fullCount)}</div>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 8, flexShrink: 0 }}>
        <div style={meta}>
          {STR.floating[L]}{" "}
          <span style={{ color: floatTotalForColor == null ? "#9fb4d6" : floatTotalForColor >= 0 ? "#5ad19a" : "#e88", fontWeight: 600 }}>
            {singleNonUsd
              ? money(nativeTotal, reportingCcy)
              : (floating?.total_usd == null ? "—" : `${money(floating.total_usd)} USD`)}
          </span>
          {floating?.basis === "PARTIAL" && (
            // Attribute the PARTIAL basis to its ACTUAL reason: an unreadable account (stale_read — live positions
            // could not be read) vs a currency that could not be converted to USD. Previously hardcoded to currency.
            <span title={staleAccts > 0
              ? "Some accounts' live positions could not be read and are excluded from the total"
              : "Some accounts use a currency that could not be converted to USD"}> · {STR.partial[L]}</span>
          )}
        </div>
        <div style={meta}>
          {agoSec === null ? STR.waiting[L] : STR.updated[L](agoSec)}{stale ? ` · ${STR.reconnecting[L]}` : ""}
        </div>
      </div>

      {/* Fixed-height flex body: ONLY this list scrolls (.open-trades-body), so the tile matches the Market Focus
          row height on desktop/tablet and never grows on refresh or trade-count change; on mobile it is capped and
          scrolls internally. Keyboard-accessible, named scroll region. */}
      <div className="open-trades-body" data-testid="open-trades-list" tabIndex={0} role="region" aria-label={STR.listLabel[L]}>
        {loading ? (
          <div style={emptyState}>{STR.loading[L]}</div>
        ) : failedFirst ? (
          <div style={emptyState}>{STR.unavailable[L]}</div>
        ) : trades.length === 0 ? (
          <div style={emptyState}>{STR.none[L]}</div>
        ) : (
          trades.map((t) => (
            <div key={t.position_id} style={rowStyle}>
              <div style={{ minWidth: 0 }}>
                <div style={{ color: "#cfe0f5", fontSize: "0.82rem", ...trunc }}>
                  {t.broker} {t.account_masked}
                </div>
                <div style={{ ...meta, ...trunc }}>{t.symbol} · {t.side} {String(t.volume ?? "")}
                  {t.strategy ? ` · ${t.strategy}` : ""}</div>
              </div>
              <div style={meta}>
                {t.open_price != null ? `@ ${t.open_price}` : ""}{t.current_price != null ? ` → ${t.current_price}` : ""}
              </div>
              <div style={{ textAlign: "right", fontWeight: 600,
                            color: (t.pl_usd ?? t.pl_native ?? 0) >= 0 ? "#5ad19a" : "#e88" }}>
                {t.pl_usd != null ? money(t.pl_usd) : money(t.pl_native, t.pl_native_currency)}
              </div>
            </div>
          ))
        )}
      </div>
      {(staleAccts > 0 || truncated) && (
        <div style={{ ...meta, marginTop: 6, flexShrink: 0 }}>
          {staleAccts > 0 ? STR.staleAccts[L](staleAccts) : ""}
          {staleAccts > 0 && truncated ? " · " : ""}
          {truncated ? STR.showing[L](trades.length, fullCount) : ""}
        </div>
      )}
    </div>
  );
};

export default OpenTradesPanel;

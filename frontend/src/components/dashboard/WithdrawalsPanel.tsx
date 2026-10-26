"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { WithdrawalMetrics } from "@/types/withdrawals";

/** WITHDRAWALS — read-only member summary of broker withdrawals (WP6).
 *
 * Observation only: renders the member's own withdrawal statistics from the owner-scoped metrics API. It exposes NO
 * action (GuvFX never initiates, confirms or modifies a withdrawal). DARK-safe: the endpoint returns 404 while
 * BROKER_WITHDRAWAL_UX_ENABLED is off, so the panel HIDES itself (renders null) until the Sponsor arms the feature —
 * no half-built UI ever appears. Truthful: PENDING (in flight) is shown separately from failed; completed amounts are
 * per-currency (never summed across currencies); processing duration shows "N/A" when unknown, never a fabricated 0.
 */
type Lang = "en" | "ja";
const PATH = "/api/broker-intelligence/withdrawals/metrics/";

const STR = {
  title: { en: "Withdrawals", ja: "出金" },
  none: { en: "No withdrawals observed yet", ja: "出金はまだ検出されていません" },
  inflight: { en: "In progress", ja: "進行中" },
  completed: { en: "Completed", ja: "完了" },
  failed: { en: "Failed", ja: "失敗" },
  withdrawn: { en: "Total withdrawn", ja: "出金総額" },
  typical: { en: "Typical processing time", ja: "通常の処理時間" },
  unresolved: { en: "Unresolved", ja: "未解決" },
  na: { en: "N/A", ja: "該当なし" },
  sampleCaveat: {
    en: "Based on a single observed withdrawal — not yet statistically representative.",
    ja: "観測された出金が1件のみ — まだ統計的に代表的ではありません。",
  },
  genuine: { en: "Genuine samples", ja: "実サンプル数" },
  summary: { en: "Summary", ja: "概要" },
};

const card: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 14, padding: "1.1rem 1.2rem",
  background: "rgba(12,18,40,0.6)",
};
const meta: React.CSSProperties = { fontSize: "0.72rem", color: "#8fa0b7" };
const statLabel: React.CSSProperties = { ...meta, marginBottom: 2 };

function humanDuration(seconds: number, lang: Lang): string {
  if (seconds < 90) return lang === "ja" ? `${seconds}秒` : `${seconds}s`;
  const mins = Math.round(seconds / 60);
  if (mins < 90) return lang === "ja" ? `${mins}分` : `${mins} min`;
  const hours = Math.round(seconds / 3600);
  if (hours < 48) return lang === "ja" ? `${hours}時間` : `${hours} h`;
  const days = Math.round(seconds / 86400);
  return lang === "ja" ? `${days}日` : `${days} d`;
}

export const WithdrawalsPanel: React.FC<{ lang?: Lang; embedded?: boolean; metrics?: WithdrawalMetrics | null }> = ({
  lang = "en", embedded = false, metrics,
}) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  // A parent may supply already-fetched metrics (the cohesive dashboard section gates the whole section's visibility
  // from the SAME payload, so it passes it down to avoid a second request and guarantee they agree). When provided,
  // use it directly and skip the self-fetch; standalone (prop absent) keeps the original owner-scoped self-fetch +
  // DARK-hide behaviour unchanged.
  const injected = metrics !== undefined;
  const [fetched, setFetched] = useState<WithdrawalMetrics | null>(null);
  const [hidden, setHidden] = useState(false);   // DARK (404) or error -> hide the whole panel
  const hasLoaded = useRef(false);

  const load = useCallback(() => {
    if (injected) return;                  // parent-provided metrics: never self-fetch
    apiFetch<WithdrawalMetrics>(PATH, {})
      .then((m) => {
        // Render ONLY a well-formed metrics payload; any non-conforming response (e.g. a DARK stub, a shape change,
        // or a test catch-all) hides the panel rather than crashing the dashboard. Defensive by contract.
        if (m && typeof (m as WithdrawalMetrics).total === "number") {
          setFetched(m);
          setHidden(false);
          hasLoaded.current = true;
        } else if (!hasLoaded.current) setHidden(true);
      })
      .catch((e: unknown) => {
        // 404 => feature DARK (BROKER_WITHDRAWAL_UX_ENABLED off); any other error => also hide (never a broken card).
        if (!hasLoaded.current) setHidden(true);
        void e;
      });
  }, [injected]);

  useEffect(() => {
    if (injected) return;
    load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, 15_000);
    return () => window.clearInterval(timer);
  }, [load, injected]);

  if (hidden) return null;                 // DARK or unavailable -> render nothing
  const data = injected ? metrics : fetched;
  if (!data || typeof data.total !== "number") return null;   // not loaded / malformed -> render nothing (no flash)

  // Embedded: the cohesive section provides the outer card chrome + heading, so render transparently as a column.
  const rootStyle: React.CSSProperties = embedded ? { padding: 0, background: "transparent", border: "none" } : card;

  const amounts = Object.entries(data.completed_amount_by_currency || {});
  const dur = data.processing_duration;

  return (
    <div style={rootStyle} data-testid="withdrawals-panel">
      {embedded ? (
        <div style={{ ...statLabel, color: "#9db0c7", fontWeight: 600, marginBottom: 10, textTransform: "uppercase", letterSpacing: "0.04em" }}>
          {STR.summary[L]}
        </div>
      ) : (
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 10 }}>
          <div style={{ color: "#e9f4ff", fontSize: "0.95rem", fontWeight: 600 }}>
            <i className="ti ti-cash-banknote-off" aria-hidden="true" style={{ marginRight: 6 }} />{STR.title[L]}
          </div>
          <div style={meta}>{data.total}</div>
        </div>
      )}

      {data.total === 0 ? (
        <div style={{ ...meta, padding: "10px 0", textAlign: "center" }}>{STR.none[L]}</div>
      ) : (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 10, marginBottom: 12 }}>
            <div>
              <div style={statLabel}>{STR.inflight[L]}</div>
              <div style={{ fontSize: "1.1rem", fontWeight: 600, color: "#fbbf24" }}>{data.pending_count}</div>
            </div>
            <div>
              <div style={statLabel}>{STR.completed[L]}</div>
              <div style={{ fontSize: "1.1rem", fontWeight: 600, color: "#5ad19a" }}>{data.completed_count}</div>
            </div>
            <div>
              <div style={statLabel}>{STR.failed[L]}</div>
              <div style={{ fontSize: "1.1rem", fontWeight: 600, color: data.failed_count > 0 ? "#e88" : "#cfe0f5" }}>
                {data.failed_count}
              </div>
            </div>
          </div>

          <div style={{ marginBottom: 8 }}>
            <div style={statLabel}>{STR.withdrawn[L]}</div>
            <div style={{ color: "#e9f4ff", fontSize: "0.9rem" }}>
              {amounts.length === 0
                ? STR.na[L]
                : amounts.map(([ccy, amt]) => `${amt} ${ccy}`).join(" · ")}
            </div>
          </div>

          <div>
            <div style={statLabel}>{STR.typical[L]}</div>
            <div style={{ color: "#cfe0f5", fontSize: "0.9rem" }} title={dur
              ? `n=${dur.count}` : undefined}>
              {dur ? humanDuration(dur.median_seconds, L) : STR.na[L]}
            </div>
          </div>

          {/* One-sample caveat (§10): never present a figure resting on a single observation as representative. */}
          {data.sample_caveat && (
            <div style={{ ...meta, color: "#c9b56b", marginTop: 8 }} data-testid="withdrawals-sample-caveat">
              {STR.sampleCaveat[L]}
            </div>
          )}

          {typeof data.genuine_count === "number" && (
            <div style={{ ...meta, marginTop: 8 }} data-testid="withdrawals-genuine-count">
              {STR.genuine[L]}: {data.genuine_count}
            </div>
          )}

          {data.unresolved_count > 0 && (
            <div style={{ marginTop: 8 }} data-testid="withdrawals-unresolved">
              <div style={statLabel}>{STR.unresolved[L]}</div>
              <div style={{ color: "#9fb0c7", fontSize: "0.9rem" }}>{data.unresolved_count}</div>
            </div>
          )}
        </>
      )}
    </div>
  );
};

export default WithdrawalsPanel;

"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { WithdrawalsPanel } from "@/components/dashboard/WithdrawalsPanel";
import { WithdrawalsHistory } from "@/components/dashboard/WithdrawalsHistory";
import type { WithdrawalMetrics, WithdrawalHistory } from "@/types/withdrawals";

/** WITHDRAWAL INTELLIGENCE SECTION — the cohesive, corporate dashboard presentation of the (already proven) withdrawal
 * surface. Composition/presentation ONLY: it does not change withdrawal detection, parsing, lifecycle, correlation,
 * ingestion, OAuth or retention — it reuses the existing owner-scoped, DARK-safe read components and arranges them as
 * one panel (left: summary + observed processing speed · right: recent history), with a link to the full
 * /withdrawal-intelligence page for filtering, connected mailboxes and analytics.
 *
 * DARK-safe: it gates the WHOLE section's visibility from a single metrics probe (same endpoint the panel uses). While
 * the feature is DARK (404), malformed, or errored, the section renders NOTHING — so no empty frame ever appears. When
 * live it passes the already-fetched metrics down to the embedded summary (no second request; they always agree).
 *
 * Observation only: GuvFX never initiates, confirms, cancels or moves a withdrawal. The processing-speed figure is an
 * observed email-interval, presented as evidence-based monitoring — never a guarantee. The single-sample disclosure
 * (surfaced inside the summary) stays visible but deliberately understated.
 */
type Lang = "en" | "ja";
const METRICS_PATH = "/api/broker-intelligence/withdrawals/metrics/";
const HISTORY_PATH = "/api/broker-intelligence/withdrawals/history/";
const DETAIL_HREF = "/withdrawal-intelligence";
const RECENT_LIMIT = 5;

const STR = {
  title: { en: "Withdrawal Intelligence", ja: "出金インテリジェンス" },
  viewAll: { en: "View all", ja: "すべて表示" },
  caption: {
    en: "Observed from your authorized withdrawal-notification emails — observation only, not a guarantee of processing speed.",
    ja: "許可された出金通知メールから観測 — 観測のみであり、処理スピードを保証するものではありません。",
  },
  none: { en: "No withdrawals observed yet", ja: "出金はまだ検出されていません" },
};

// Section frame (matches the dashboard's institutional "glass" section cards: subtle blue border, dark gradient).
const panel: React.CSSProperties = {
  borderRadius: 14, border: "1px solid rgba(74,179,255,0.12)",
  background: "linear-gradient(135deg, rgba(10,15,40,0.95) 0%, rgba(5,8,22,0.98) 100%)",
  padding: "1.1rem 1.25rem",
};
const headRow: React.CSSProperties = {
  display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap",
};
const titleStyle: React.CSSProperties = {
  margin: 0, fontSize: "0.95rem", fontWeight: 600, color: "#e9f4ff", display: "inline-flex", alignItems: "center", gap: 7,
};
const countStyle: React.CSSProperties = {
  fontSize: "0.68rem", color: "#9db0c7", border: "1px solid rgba(255,255,255,0.12)", borderRadius: 999, padding: "1px 8px",
};
const linkStyle: React.CSSProperties = { fontSize: "0.72rem", color: "#4ab3ff", textDecoration: "none", whiteSpace: "nowrap" };
const captionStyle: React.CSSProperties = { fontSize: "0.68rem", color: "#8fa0b7", lineHeight: 1.5, margin: "6px 0 0", maxWidth: 680 };

/** Reusable presentational shell for an "intelligence" dashboard section: a titled corporate panel with an optional
 * count, a "View all" link, a caption, and a responsive two-column body. Kept dataset-agnostic on purpose so a future
 * dimension (e.g. broker execution speed) can reuse the exact visual system without coupling to withdrawal data. */
export const IntelligenceSectionShell: React.FC<{
  headingId: string; icon: string; title: string; viewAllLabel: string; href: string;
  count?: React.ReactNode; caption?: string; children: React.ReactNode;
}> = ({ headingId, icon, title, viewAllLabel, href, count, caption, children }) => (
  <section style={panel} aria-labelledby={headingId} data-testid="withdrawal-intelligence-section">
    <div style={headRow}>
      <h2 id={headingId} style={titleStyle}>
        <i className={`ti ti-${icon}`} aria-hidden="true" />
        {title}
        {count != null && <span style={countStyle}>{count}</span>}
      </h2>
      <Link href={href} style={linkStyle}>{viewAllLabel} →</Link>
    </div>
    {caption && <p style={captionStyle}>{caption}</p>}
    <div style={{ marginTop: "0.95rem" }}>{children}</div>
  </section>
);

export const WithdrawalIntelligenceSection: React.FC<{ lang?: Lang }> = ({ lang = "en" }) => {
  const L: Lang = lang === "ja" ? "ja" : "en";
  // The section owns ONE load and feeds both columns, so summary and history appear together and always agree. The
  // METRICS probe is the visibility gate (DARK 404 / malformed / error -> the whole section renders nothing — no empty
  // frame). History loads in parallel and is injected into the right column; a history-only failure degrades to the
  // summary alone rather than hiding the section.
  const [metrics, setMetrics] = useState<WithdrawalMetrics | null>(null);
  const [hidden, setHidden] = useState(false);
  const [history, setHistory] = useState<WithdrawalHistory | null>(null);
  const [historyDone, setHistoryDone] = useState(false);

  useEffect(() => {
    let live = true;
    apiFetch<WithdrawalMetrics>(METRICS_PATH, {})
      .then((m) => {
        if (!live) return;
        if (m && typeof (m as WithdrawalMetrics).total === "number") setMetrics(m);
        else setHidden(true);   // non-conforming payload: hide, never a broken frame
      })
      .catch(() => { if (live) setHidden(true); });   // 404 => feature DARK; any error => hide
    apiFetch<WithdrawalHistory>(HISTORY_PATH, {})
      .then((h) => {
        if (!live) return;
        if (h && Array.isArray((h as WithdrawalHistory).withdrawals)) setHistory(h);
        setHistoryDone(true);
      })
      .catch(() => { if (live) setHistoryDone(true); });   // history-only failure: right column simply absent
    return () => { live = false; };
  }, []);

  if (hidden) return null;                 // DARK or unavailable -> render nothing (no empty frame)
  if (!metrics || !historyDone) return null;   // wait for the gate AND history to settle -> columns appear together

  return (
    <IntelligenceSectionShell
      headingId="wi-section-heading" icon="cash-banknote" title={STR.title[L]}
      viewAllLabel={STR.viewAll[L]} href={DETAIL_HREF} count={metrics.total} caption={STR.caption[L]}
    >
      {metrics.total === 0 ? (
        // Honest, single empty state — never two duplicate "nothing yet" messages, never an oversized empty region.
        <div style={{ fontSize: "0.78rem", color: "#8fa0b7", textAlign: "center", padding: "14px 0" }}
          data-testid="wi-section-empty">
          {STR.none[L]}
        </div>
      ) : (
        <div style={{ display: "flex", flexWrap: "wrap", gap: "1.1rem", alignItems: "stretch" }}>
          {/* LEFT — compact summary + observed processing-speed metric (reuses the gated metrics; no 2nd fetch) */}
          <div style={{ flex: "1 1 280px", minWidth: 260 }}>
            <WithdrawalsPanel lang={L} embedded metrics={metrics} />
          </div>
          {/* RIGHT — recent withdrawal history (full filtering/history lives on the detail page via "View all") */}
          <div style={{ flex: "1.6 1 360px", minWidth: 300 }}>
            <WithdrawalsHistory lang={L} embedded limit={RECENT_LIMIT} data={history} />
          </div>
        </div>
      )}
    </IntelligenceSectionShell>
  );
};

export default WithdrawalIntelligenceSection;

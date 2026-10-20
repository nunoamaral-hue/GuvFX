"use client";

/**
 * WAYOND Trader Match (WTM) — broker-performance metrics PREVIEW (investor demo).
 *
 * Unauthenticated, read-only. Consumes the DARK endpoint GET /api/public/wtm-preview/ (broker_public), which 404s
 * until BROKER_PUBLIC_WTM_PREVIEW_ENABLED is armed (isolated demo only) — so this page is DARK-graceful (404/network
 * error → calm "not yet available"). Every scorecard is a clearly-labelled SIMULATED illustration; the authentic
 * public state is shown as INSUFFICIENT_VERIFIED_DATA. NEVER a member identity/email/account/transaction. This is a
 * PREVIEW, not a deployed WAYOND integration, and publishes no public rating.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { type Lang, detectLang, setLang as persistLang } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";
import { LanguageDropdown } from "@/components/LanguageDropdown";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "https://api.guvfx.com";
const PAGE_BG = "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)";
const CARD_BG = "rgba(255,255,255,0.03)";
const BORDER = "1px solid rgba(74, 179, 255, 0.15)";

type Hist = { bucket: string; count: number };
type Scorecard = {
  slug: string; display_name: string; simulated: boolean; sample_size: number; contributor_count: number;
  methodology_version: number;
  withdrawal_processing: {
    median_minutes: number; avg_minutes: number; p90_minutes: number; min_minutes: number; max_minutes: number;
    histogram: Hist[]; lifecycle_pct: { completed: number; pending: number; rejected: number };
  };
};
type Payload = {
  preview: boolean; not_a_deployed_integration: boolean; title: string;
  authentic_state: string; authentic_note: string; simulated_brokers: Scorecard[];
  methodology: string; privacy: string; source: string; flow: string[];
};
type LoadState = "loading" | "unavailable" | "ready";

const S = {
  previewBadge: { en: "PREVIEW", ja: "プレビュー" },
  notDeployed: {
    en: "Illustration only — not a deployed WAYOND integration, and no public rating is published.",
    ja: "図示のみ — 導入済みのWAYOND統合ではなく、公開評価も公表していません。",
  },
  simulated: { en: "SIMULATED", ja: "シミュレーション" },
  authentic: { en: "Authentic public state", ja: "実データの公開状態" },
  samples: { en: "samples", ja: "サンプル" },
  contributors: { en: "contributors", ja: "貢献者" },
  median: { en: "Median", ja: "中央値" },
  avg: { en: "Average", ja: "平均" },
  p90: { en: "90th pct", ja: "90パーセンタイル" },
  range: { en: "Range", ja: "範囲" },
  processing: { en: "Request → completion processing time", ja: "申請→完了の処理時間" },
  distribution: { en: "Processing-time distribution", ja: "処理時間の分布" },
  lifecycle: { en: "Lifecycle", ja: "ライフサイクル" },
  completed: { en: "Completed", ja: "完了" },
  pending: { en: "In progress", ja: "進行中" },
  rejected: { en: "Rejected", ja: "却下" },
  methodologyH: { en: "Methodology", ja: "方法論" },
  privacyH: { en: "Privacy", ja: "プライバシー" },
  sourceH: { en: "Data source", ja: "データソース" },
  flowH: { en: "GuvFX evidence → aggregate preview → WAYOND broker page", ja: "GuvFX証跡 → 集計プレビュー → WAYONDブローカーページ" },
  unavailable: { en: "The WAYOND Trader Match preview is not available in this environment.", ja: "この環境ではWAYOND Trader Matchプレビューは利用できません。" },
  broker: { en: "Broker", ja: "ブローカー" },
};

function minutes(n: number, L: Lang) {
  if (n < 90) return L === "ja" ? `${n}分` : `${n} min`;
  const h = Math.round(n / 60);
  return L === "ja" ? `${h}時間` : `${h} h`;
}

export default function WtmPreviewPage() {
  const [lang, setLangState] = useState<Lang>(() => (typeof window === "undefined" ? "en" : detectLang()));
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [state, setState] = useState<LoadState>("loading");
  const [data, setData] = useState<Payload | null>(null);
  const [slug, setSlug] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/public/wtm-preview/`, { method: "GET" })
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) { setState("unavailable"); return; }
        const d = (await res.json()) as Payload;
        if (cancelled) return;
        setData(d);
        setSlug(d.simulated_brokers?.[0]?.slug ?? "");
        setState("ready");
      })
      .catch(() => { if (!cancelled) setState("unavailable"); });
    return () => { cancelled = true; };
  }, []);

  const onLang = useCallback((next: Lang) => { persistLang(next); setLangState(next); }, []);
  const selected = useMemo(
    () => data?.simulated_brokers.find((b) => b.slug === slug) ?? data?.simulated_brokers[0],
    [data, slug],
  );
  const maxHist = useMemo(
    () => Math.max(1, ...(selected?.withdrawal_processing.histogram.map((h) => h.count) ?? [1])),
    [selected],
  );

  return (
    <div style={{ minHeight: "100vh", width: "100%", display: "flex", flexDirection: "column", background: PAGE_BG,
                  color: "#e5f4ff", fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif" }}>
      <nav style={{ padding: "1rem 2rem", display: "flex", alignItems: "center", justifyContent: "space-between",
                    borderBottom: "1px solid rgba(74, 179, 255, 0.1)" }}>
        <Link href="/brokers" style={{ color: "#9db6d6", textDecoration: "none", fontSize: "0.9rem" }}>← GuvFX Brokers</Link>
        <LanguageDropdown lang={lang} onChange={onLang} />
      </nav>

      <main style={{ flex: 1, width: "100%", maxWidth: 920, margin: "0 auto", padding: "2rem 1rem 3rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <h1 style={{ margin: 0, fontSize: "1.6rem" }}>WAYOND Trader Match</h1>
          <span style={{ padding: "2px 10px", borderRadius: 999, fontSize: "0.7rem", fontWeight: 700,
                         background: "rgba(63,224,255,0.14)", border: "1px solid rgba(63,224,255,0.5)", color: "#8fe9ff" }}>
            {S.previewBadge[L]}
          </span>
        </div>
        <p style={{ color: "#c9a24a", fontSize: "0.82rem", marginTop: 6 }}>⚠ {S.notDeployed[L]}</p>

        {state === "loading" && <p style={{ color: "#9db6d6", marginTop: 24 }}>…</p>}
        {state === "unavailable" && (
          <div style={{ marginTop: 24, border: BORDER, borderRadius: 12, padding: "1.25rem 1.5rem", background: CARD_BG }}>
            <p style={{ color: "#9db6d6", margin: 0 }}>{S.unavailable[L]}</p>
          </div>
        )}

        {state === "ready" && data && selected && (
          <>
            {/* Authentic public state */}
            <div data-testid="wtm-authentic" style={{ marginTop: 20, border: BORDER, borderRadius: 12,
                  padding: "0.9rem 1.2rem", background: "rgba(159,176,183,0.08)" }}>
              <div style={{ fontSize: "0.72rem", color: "#9fb0c7", textTransform: "uppercase", letterSpacing: "0.05em" }}>
                {S.authentic[L]}
              </div>
              <div style={{ fontSize: "0.95rem", fontWeight: 600, marginTop: 2 }}>{data.authentic_state}</div>
              <div style={{ fontSize: "0.82rem", color: "#9db6d6", marginTop: 4, lineHeight: 1.5 }}>{data.authentic_note}</div>
            </div>

            {/* Broker filter */}
            <div style={{ marginTop: 20, display: "flex", alignItems: "center", gap: 10 }}>
              <label style={{ color: "#9db6d6", fontSize: "0.85rem" }}>{S.broker[L]}:</label>
              <select data-testid="wtm-broker-select" value={selected.slug} onChange={(e) => setSlug(e.target.value)}
                      style={{ padding: "0.35rem 0.6rem", borderRadius: 8, border: BORDER,
                               background: "rgba(8,12,28,0.7)", color: "#e9f4ff", fontSize: "0.85rem" }}>
                {data.simulated_brokers.map((b) => <option key={b.slug} value={b.slug}>{b.display_name}</option>)}
              </select>
            </div>

            {/* Scorecard */}
            <div data-testid="wtm-scorecard" style={{ marginTop: 14, border: BORDER, borderRadius: 14, padding: "1.3rem 1.5rem", background: CARD_BG }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
                <h2 style={{ margin: 0, fontSize: "1.25rem" }}>{selected.display_name}</h2>
                <span style={{ padding: "2px 10px", borderRadius: 999, fontSize: "0.68rem", fontWeight: 700,
                               background: "rgba(201,162,74,0.16)", border: "1px solid rgba(201,162,74,0.55)", color: "#e7c77a" }}>
                  {S.simulated[L]}
                </span>
              </div>
              <div style={{ color: "#9db6d6", fontSize: "0.82rem", marginTop: 4 }}>
                {selected.sample_size} {S.samples[L]} · {selected.contributor_count} {S.contributors[L]} · v{selected.methodology_version}
              </div>

              <div style={{ marginTop: 14, fontSize: "0.8rem", color: "#9fb0c7" }}>{S.processing[L]}</div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(90px, 1fr))", gap: 10, marginTop: 8 }}>
                {[[S.median[L], selected.withdrawal_processing.median_minutes],
                  [S.avg[L], selected.withdrawal_processing.avg_minutes],
                  [S.p90[L], selected.withdrawal_processing.p90_minutes]].map(([k, v]) => (
                  <div key={String(k)} style={{ border: BORDER, borderRadius: 10, padding: "0.6rem 0.7rem" }}>
                    <div style={{ fontSize: "0.68rem", color: "#9fb0c7" }}>{k}</div>
                    <div style={{ fontSize: "1.1rem", fontWeight: 700, color: "#8fe9ff" }}>{minutes(Number(v), L)}</div>
                  </div>
                ))}
                <div style={{ border: BORDER, borderRadius: 10, padding: "0.6rem 0.7rem" }}>
                  <div style={{ fontSize: "0.68rem", color: "#9fb0c7" }}>{S.range[L]}</div>
                  <div style={{ fontSize: "0.95rem", fontWeight: 600 }}>
                    {minutes(selected.withdrawal_processing.min_minutes, L)}–{minutes(selected.withdrawal_processing.max_minutes, L)}
                  </div>
                </div>
              </div>

              {/* Histogram */}
              <div style={{ marginTop: 16, fontSize: "0.8rem", color: "#9fb0c7" }}>{S.distribution[L]}</div>
              <div style={{ marginTop: 8, display: "grid", gap: 6 }}>
                {selected.withdrawal_processing.histogram.map((h) => (
                  <div key={h.bucket} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <div style={{ width: 54, fontSize: "0.72rem", color: "#9db6d6" }}>{h.bucket}</div>
                    <div style={{ flex: 1, background: "rgba(255,255,255,0.05)", borderRadius: 6, height: 16 }}>
                      <div style={{ width: `${(h.count / maxHist) * 100}%`, height: "100%", borderRadius: 6,
                                    background: "linear-gradient(90deg, #2979ff, #3fe0ff)" }} />
                    </div>
                    <div style={{ width: 24, textAlign: "right", fontSize: "0.72rem", color: "#9db6d6" }}>{h.count}</div>
                  </div>
                ))}
              </div>

              {/* Lifecycle */}
              <div style={{ marginTop: 16, fontSize: "0.8rem", color: "#9fb0c7" }}>{S.lifecycle[L]}</div>
              <div style={{ display: "flex", height: 18, borderRadius: 6, overflow: "hidden", marginTop: 8, fontSize: "0.65rem" }}>
                {([["#5ad19a", selected.withdrawal_processing.lifecycle_pct.completed, S.completed[L]],
                   ["#fbbf24", selected.withdrawal_processing.lifecycle_pct.pending, S.pending[L]],
                   ["#e88", selected.withdrawal_processing.lifecycle_pct.rejected, S.rejected[L]]] as const).map(([c, pct, lbl]) => (
                  <div key={lbl} title={`${lbl} ${pct}%`} style={{ width: `${pct}%`, background: c, color: "#05121f",
                        display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 700 }}>
                    {pct >= 10 ? `${pct}%` : ""}
                  </div>
                ))}
              </div>
            </div>

            {/* Flow */}
            <div style={{ marginTop: 20, fontSize: "0.72rem", color: "#9fb0c7", textTransform: "uppercase", letterSpacing: "0.05em" }}>{S.flowH[L]}</div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
              {data.flow.map((step, i) => (
                <div key={i} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <div style={{ border: BORDER, borderRadius: 10, padding: "0.5rem 0.7rem", fontSize: "0.76rem",
                                background: CARD_BG, maxWidth: 200 }}>{step}</div>
                  {i < data.flow.length - 1 && <span style={{ color: "#4ab3ff" }}>→</span>}
                </div>
              ))}
            </div>

            {/* Badges */}
            <div style={{ marginTop: 20, display: "grid", gap: 12 }}>
              {[[S.sourceH[L], data.source], [S.methodologyH[L], data.methodology], [S.privacyH[L], data.privacy]].map(([h, body]) => (
                <div key={h} style={{ border: BORDER, borderRadius: 10, padding: "0.8rem 1rem", background: CARD_BG }}>
                  <div style={{ fontSize: "0.72rem", color: "#9fb0c7", textTransform: "uppercase", letterSpacing: "0.05em" }}>{h}</div>
                  <div style={{ fontSize: "0.82rem", color: "#c7d6e8", marginTop: 4, lineHeight: 1.5 }}>{body}</div>
                </div>
              ))}
            </div>
          </>
        )}
      </main>

      <LegalFooter lang={lang} />
    </div>
  );
}

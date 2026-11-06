"use client";

/**
 * Public broker withdrawal-intelligence page (Stream C / ADR-0080).
 *
 * Unauthenticated, read-only. Consumes GET /api/public/brokers/<slug>/withdrawal-intelligence/ (DARK — the backend
 * 404s until the flag is armed AND the broker is published). Renders ONLY the aggregate-only, reason-coded projection
 * the API returns (three separate lifecycle-stage durations, lifecycle outcome rates, sample sufficiency, methodology,
 * limitations) and shows a calm SUPPRESSED / PARTIAL / UNAVAILABLE state otherwise — NEVER a member identity, amount,
 * exact timestamp, account number or reference. DARK-graceful: on 404/error it shows a localized "not available" panel,
 * so the payload is never shipped in the client bundle until the backend serves it. en/ja.
 */
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type Lang, detectLang, setLang as persistLang } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";
import { LanguageDropdown } from "@/components/LanguageDropdown";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "https://api.guvfx.com";
const PAGE_BG = "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)";
const CARD_BG = "rgba(255,255,255,0.03)";
const BORDER = "1px solid rgba(74, 179, 255, 0.15)";

type Stage = { state: string; reason?: string; count?: number; median_seconds?: number };
type Rates = {
  state?: string;
  reason?: string;
  denominator?: number;
  denominator_definition?: string;
  completion_pct?: number;
  failed_pct?: number;
  expired_pct?: number;
  suppressed_pct?: number;
};
type Contract = {
  category: string;
  methodology_version: number;
  broker: { slug: string; display_name: string };
  evidence_status: { state: string; reason: string };
  methodology: string;
  limitations: string[];
  observation_window?: { start: string | null; end: string | null };
  data_freshness?: string | null;
  sample_sufficiency?: { contributors: number; observations: number };
  withdrawal_speed?: { request_to_confirmation: Stage; confirmation_to_completion: Stage; total_request_to_completion: Stage };
  lifecycle_outcomes?: { counts: Record<string, number>; rates: Rates; confirmation_required_flow: Record<string, number> };
  per_currency_counts?: Record<string, number>;
};
type LoadState = "loading" | "unavailable" | "ready";

const STR: Record<Lang, Record<string, string>> = {
  en: {
    back: "← All brokers",
    loading: "Loading…",
    naTitle: "Not available in this environment",
    naBody: "This broker withdrawal-intelligence page is not published here.",
    heading: "Withdrawal intelligence",
    aggregate: "Aggregate evidence across contributing members — never an individual withdrawal.",
    stage1: "Request → confirmation",
    stage2: "Confirmation → completion",
    stage3: "Request → completion (total)",
    outcomes: "Lifecycle outcomes",
    completion: "Completion rate",
    failed: "Failed rate",
    expired: "Expired rate",
    otherWithheld: "Other outcomes (withheld)",
    ofResolved: "Rates over {n} resolved outcomes; in-flight withdrawals excluded.",
    sample: "Sample",
    contributors: "contributing accounts",
    observations: "observations",
    freshness: "Evidence as of",
    methodology: "Methodology",
    limitations: "Limitations",
    unavailable: "Insufficient verified evidence to publish this figure.",
    withheld: "Withheld to protect member privacy (insufficient sample).",
  },
  ja: {
    back: "← すべてのブローカー",
    loading: "読み込み中…",
    naTitle: "この環境では利用できません",
    naBody: "このブローカーの出金インテリジェンスページはここでは公開されていません。",
    heading: "出金インテリジェンス",
    aggregate: "参加メンバー全体の集計された証拠であり、個々の出金ではありません。",
    stage1: "申請 → 確認",
    stage2: "確認 → 完了",
    stage3: "申請 → 完了（合計）",
    outcomes: "ライフサイクルの結果",
    completion: "完了率",
    failed: "失敗率",
    expired: "期限切れ率",
    otherWithheld: "その他の結果（非公開）",
    ofResolved: "{n}件の確定した結果に対する割合。進行中の出金は除外。",
    sample: "サンプル",
    contributors: "貢献アカウント",
    observations: "観測数",
    freshness: "証拠の時点",
    methodology: "方法論",
    limitations: "制限事項",
    unavailable: "この数値を公開するための検証済み証拠が不十分です。",
    withheld: "メンバーのプライバシー保護のため非公開（サンプル不足）。",
  },
};

function fmtDuration(seconds?: number): string {
  if (seconds === undefined || seconds === null) return "—";
  if (seconds < 60) return `${seconds}s`;   // a real sub-minute median must not render as the misleading '0m'
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

export default function PublicBrokerWithdrawalIntelligencePage() {
  const params = useParams<{ slug: string }>();
  const slug = typeof params?.slug === "string" ? params.slug : Array.isArray(params?.slug) ? params.slug[0] : "";
  const [lang, setLangState] = useState<Lang>(() => (typeof window === "undefined" ? "en" : detectLang()));
  const [state, setState] = useState<LoadState>("loading");
  const [data, setData] = useState<Contract | null>(null);
  const s = STR[lang];

  useEffect(() => {
    if (!slug) return;
    let cancelled = false;
    fetch(`${API_BASE}/api/public/brokers/${encodeURIComponent(slug)}/withdrawal-intelligence/`, { method: "GET" })
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) { setState("unavailable"); return; } // DARK or missing — calm, not an error
        setData(await res.json());
        setState("ready");
      })
      .catch(() => { if (!cancelled) setState("unavailable"); });
    return () => { cancelled = true; };
  }, [slug]);

  const onLang = useCallback((next: Lang) => { persistLang(next); setLangState(next); }, []);
  const published = data?.evidence_status?.state === "PUBLISHED";

  return (
    <div style={{ minHeight: "100vh", width: "100%", display: "flex", flexDirection: "column", background: PAGE_BG,
                  color: "#e5f4ff", fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif" }}>
      <nav style={{ padding: "1rem 2rem", display: "flex", alignItems: "center", justifyContent: "space-between",
                    borderBottom: "1px solid rgba(74, 179, 255, 0.1)" }}>
        <Link href="/" style={{ fontSize: "1.25rem", fontWeight: 600, textDecoration: "none", color: "#4ab3ff" }}>GuvFX</Link>
        <LanguageDropdown lang={lang} onChange={onLang} variant="compact" />
      </nav>

      <main style={{ flex: 1, padding: "2rem", maxWidth: 820, margin: "0 auto", width: "100%" }}>
        {slug && (
          <Link href={`/brokers/${encodeURIComponent(slug)}`}
                style={{ color: "#4ab3ff", fontSize: "0.85rem", textDecoration: "none" }}>{s.back}</Link>
        )}

        {state === "loading" && <p style={{ color: "#9db6d6", marginTop: "1.5rem" }}>{s.loading}</p>}

        {state === "unavailable" && <Panel title={s.naTitle} body={s.naBody} />}

        {state === "ready" && data && (
          <>
            <h1 style={{ fontSize: "1.9rem", fontWeight: 700, margin: "1.25rem 0 0.25rem" }}>{data.broker.display_name}</h1>
            <div style={{ color: "#7af0ff", fontWeight: 600, marginBottom: "0.5rem" }}>{s.heading}</div>
            <p style={{ color: "#9db6d6", lineHeight: 1.6, margin: "0 0 1.5rem" }}>{s.aggregate}</p>

            {!published ? (
              <Panel title={s.naTitle}
                     body={`${s.withheld} (${data.evidence_status.state} · ${data.evidence_status.reason})`} />
            ) : (
              <>
                <Card>
                  <Row label={s.stage1} value={stageText(data.withdrawal_speed?.request_to_confirmation, s)} />
                  <Row label={s.stage2} value={stageText(data.withdrawal_speed?.confirmation_to_completion, s)} />
                  <Row label={s.stage3} value={stageText(data.withdrawal_speed?.total_request_to_completion, s)} />
                </Card>
                <Card title={s.outcomes}>
                  {(() => {
                    const r = data.lifecycle_outcomes?.rates;
                    const hasRate = !!r && [r.completion_pct, r.failed_pct, r.expired_pct, r.suppressed_pct]
                      .some((v) => v !== undefined);
                    if (!r || !hasRate) return <div style={{ color: "#9db6d6" }}>{s.unavailable}</div>;
                    return (
                      <>
                        {r.completion_pct !== undefined && <Row label={s.completion} value={`${r.completion_pct}%`} />}
                        {r.failed_pct !== undefined && <Row label={s.failed} value={`${r.failed_pct}%`} />}
                        {r.expired_pct !== undefined && <Row label={s.expired} value={`${r.expired_pct}%`} />}
                        {r.suppressed_pct !== undefined && <Row label={s.otherWithheld} value={`${r.suppressed_pct}%`} />}
                        {r.denominator !== undefined && (
                          <div style={{ color: "#6f86a6", fontSize: "0.78rem", marginTop: "0.5rem" }}>
                            {s.ofResolved.replace("{n}", String(r.denominator))}
                          </div>
                        )}
                      </>
                    );
                  })()}
                </Card>
                <div style={{ color: "#6f86a6", fontSize: "0.8rem", lineHeight: 1.7, marginTop: "1rem" }}>
                  <div>{s.sample}: {data.sample_sufficiency?.contributors} {s.contributors}, {data.sample_sufficiency?.observations} {s.observations}</div>
                  {data.data_freshness && <div>{s.freshness}: {data.data_freshness}</div>}
                  <div style={{ marginTop: "0.75rem", fontWeight: 600, color: "#9db6d6" }}>{s.methodology}</div>
                  <div>{data.methodology}</div>
                  <div style={{ marginTop: "0.75rem", fontWeight: 600, color: "#9db6d6" }}>{s.limitations}</div>
                  <ul style={{ margin: "0.25rem 0 0", paddingLeft: "1.1rem" }}>
                    {(data.limitations || []).map((l, i) => <li key={i}>{l}</li>)}
                  </ul>
                </div>
              </>
            )}
          </>
        )}
      </main>
      <LegalFooter lang={lang} />
    </div>
  );
}

function stageText(stage: Stage | undefined, s: Record<string, string>): string {
  if (!stage || stage.state !== "PUBLISHED") return s.unavailable;
  return `${fmtDuration(stage.median_seconds)} (n=${stage.count})`;
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: "1rem", fontSize: "0.95rem", padding: "0.35rem 0" }}>
      <span style={{ color: "#9db6d6" }}>{label}</span>
      <span style={{ fontVariantNumeric: "tabular-nums" }}>{value}</span>
    </div>
  );
}

function Card({ title, children }: { title?: string; children: React.ReactNode }) {
  return (
    <div style={{ background: CARD_BG, border: BORDER, borderRadius: 12, padding: "1.5rem", marginBottom: "1rem" }}>
      {title && <div style={{ fontSize: "1.05rem", fontWeight: 600, marginBottom: "0.5rem" }}>{title}</div>}
      {children}
    </div>
  );
}

function Panel({ title, body }: { title: string; body: string }) {
  return (
    <div style={{ background: CARD_BG, border: BORDER, borderRadius: 12, padding: "2rem", marginTop: "1.5rem" }}>
      <div style={{ fontSize: "1.1rem", fontWeight: 600, marginBottom: "0.5rem" }}>{title}</div>
      <div style={{ color: "#9db6d6", lineHeight: 1.6 }}>{body}</div>
    </div>
  );
}

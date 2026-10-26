"use client";

import React, { useEffect, useMemo, useState } from "react";
import { getInstrumentMatrix } from "@/lib/copy-trading-api";
import type {
  InstrumentMatrix, MatrixDirection, MatrixCandidate, MappingClassification,
} from "@/types/copy-trading";

/** Founder-only, owner-scoped, READ-ONLY multi-broker instrument-mapping matrix (/copy-trading/instruments).
 *
 * Shows every ORDERED master→follower pair of the owner's own accounts with coverage counts, and a per-pair
 * drill-down of native source→destination symbols, classification, verification time/source and the material
 * differences that keep two symbols from being equated by name (e.g. XAUUSD contract 100 / USD vs XAUUSD_SB
 * contract 1 / GBP). Catalogue coverage is NOT execution readiness: the page writes nothing, maps nothing and
 * keeps Copy Trading execution OFF — the quiet observation-mode treatment, never a red banner. */
type Lang = "en" | "ja";

const T = {
  title: { en: "Instrument Mapping Matrix", ja: "銘柄マッピング マトリクス" },
  notAvailable: { en: "The instrument matrix is not available for your account.",
                  ja: "お客様の口座では銘柄マトリクスは利用できません。" },
  loading: { en: "Loading…", ja: "読み込み中…" },
  coverageNote: {
    en: "Catalogue coverage is not execution readiness. A verified mapping and a valid size do not activate trading; execution requires a separate production authorisation and live safety gate.",
    ja: "カタログのカバレッジは実行準備完了ではありません。検証済みマッピングと有効なサイズがあってもトレードは開始しません。実行には別途の本番承認とライブ安全ゲートが必要です。",
  },
  catalogue: { en: "Catalogue", ja: "カタログ" },
  synced: { en: "synced", ja: "同期済み" },
  empty: { en: "not synced", ja: "未同期" },
  stale: { en: "stale", ja: "古い" },
  pair: { en: "Master → Follower", ja: "マスター → フォロワー" },
  coverage: { en: "Coverage", ja: "カバレッジ" },
  eligible: { en: "Source instruments", ja: "ソース銘柄" },
  inspect: { en: "Inspect", ja: "検査" },
  source: { en: "Source symbol", ja: "ソース銘柄" },
  dest: { en: "Follower symbol", ja: "フォロワー銘柄" },
  state: { en: "State", ja: "状態" },
  verifiedAt: { en: "Verified", ja: "検証日時" },
  differences: { en: "Material differences", ja: "重要な相違" },
  none: { en: "none", ja: "なし" },
  contractSize: { en: "contract size", ja: "契約サイズ" },
  settlement: { en: "P&L currency", ja: "損益通貨" },
  contractType: { en: "contract type", ja: "契約種別" },
  noCandidates: { en: "No mappable source instruments for this direction.", ja: "この方向にマッピング可能なソース銘柄はありません。" },
} as const;

const CLS: Record<MappingClassification, { en: string; ja: string; color: string }> = {
  VERIFIED: { en: "Verified", ja: "検証済み", color: "#86efac" },
  UNVERIFIED: { en: "Unverified — needs verification", ja: "未検証 — 要検証", color: "#fcd34d" },
  AMBIGUOUS: { en: "Ambiguous — fail closed", ja: "曖昧 — フェイルクローズ", color: "#fca5a5" },
  STALE: { en: "Stale catalogue", ja: "古いカタログ", color: "#9db6d6" },
  INELIGIBLE: { en: "Ineligible", ja: "対象外", color: "#9db6d6" },
  ABSENT: { en: "Absent", ja: "なし", color: "#64748b" },
};

const panel: React.CSSProperties = {
  border: "1px solid rgba(74,179,255,0.15)", borderRadius: 12, padding: "1.1rem 1.3rem",
  background: "rgba(255,255,255,0.02)", marginTop: 14,
};
const muted: React.CSSProperties = { color: "#9db6d6", fontSize: "0.85rem" };
const cell: React.CSSProperties = { padding: "6px 10px", textAlign: "left", fontSize: "0.85rem" };

function Chip({ c, L }: { c: MappingClassification; L: Lang }) {
  const m = CLS[c];
  return (
    <span data-testid={`matrix-classification-${c}`} style={{
      color: m.color, border: `1px solid ${m.color}55`, borderRadius: 999,
      padding: "1px 9px", fontSize: "0.78rem", whiteSpace: "nowrap",
    }}>{m[L]}</span>
  );
}

function MaterialDiffs({ c, L }: { c: MatrixCandidate; L: Lang }) {
  const md = c.material_differences || {};
  const parts: string[] = [];
  if (md.contract_size?.differs) parts.push(`${T.contractSize[L]} ${md.contract_size.master}→${md.contract_size.follower}`);
  if (md.settlement_currency?.differs) parts.push(`${T.settlement[L]} ${md.settlement_currency.master}→${md.settlement_currency.follower}`);
  if (md.contract_type?.differs) parts.push(`${T.contractType[L]} ${md.contract_type.master}→${md.contract_type.follower}`);
  return <span style={muted}>{parts.length ? parts.join(" · ") : T.none[L]}</span>;
}

function DirectionDetail({ d, L }: { d: MatrixDirection; L: Lang }) {
  if (!d.candidates.length) return <p style={muted}>{T.noCandidates[L]}</p>;
  return (
    <table data-testid={`matrix-detail-${d.master_account_id}-${d.follower_account_id}`}
           style={{ width: "100%", borderCollapse: "collapse", marginTop: 8 }}>
      <thead>
        <tr style={{ color: "#9db6d6", borderBottom: "1px solid rgba(74,179,255,0.15)" }}>
          <th style={cell}>{T.source[L]}</th>
          <th style={cell}>{T.dest[L]}</th>
          <th style={cell}>{T.state[L]}</th>
          <th style={cell}>{T.verifiedAt[L]}</th>
          <th style={cell}>{T.differences[L]}</th>
        </tr>
      </thead>
      <tbody>
        {d.candidates.map((c) => (
          <tr key={`${c.master_symbol}->${c.follower_symbol}`}
              data-testid={`matrix-candidate-${d.master_account_id}-${d.follower_account_id}-${c.master_symbol}`}
              style={{ borderBottom: "1px solid rgba(74,179,255,0.06)" }}>
            <td style={cell}><code>{c.master_symbol}</code> <span style={muted}>· {c.canonical.display}</span></td>
            <td style={cell}>{c.follower_symbol ? <code>{c.follower_symbol}</code> : <span style={muted}>—</span>}</td>
            <td style={cell}><Chip c={c.classification} L={L} /></td>
            <td style={cell}>{c.verified_at ? new Date(c.verified_at).toISOString().slice(0, 10) : <span style={muted}>—</span>}</td>
            <td style={cell}><MaterialDiffs c={c} L={L} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function InstrumentMatrixContent({ lang }: { lang: Lang }) {
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [data, setData] = useState<InstrumentMatrix | null>(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    getInstrumentMatrix()
      .then((d) => { if (alive) setData(d); })
      .catch(() => { if (alive) setData(null); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, []);

  const accountsById = useMemo(() => {
    const m = new Map<number, string>();
    (data?.accounts || []).forEach((a) => m.set(a.account_id, a.broker));
    return m;
  }, [data]);

  if (loading) return <p style={muted}>{T.loading[L]}</p>;
  if (!data || !data.enabled) {
    return <p data-testid="matrix-not-available" style={muted}>{T.notAvailable[L]}</p>;
  }

  const es = data.execution_status;

  return (
    <div data-testid="instrument-matrix">
      <h1 style={{ fontSize: "1.3rem", margin: "0 0 4px" }}>{T.title[L]}</h1>

      {es && (
        <div data-testid="matrix-status" role="status" style={{
          display: "inline-flex", gap: 8, alignItems: "center", marginTop: 6,
          color: "#9db6d6", border: "1px solid rgba(157,182,214,0.3)", borderRadius: 999, padding: "2px 12px",
          fontSize: "0.82rem",
        }}>
          <span style={{ width: 7, height: 7, borderRadius: 999, background: "#9db6d6" }} />
          <strong style={{ color: "#c7d7ee", fontWeight: 600 }}>{es.label}</strong>
          <span>· {es.detail}</span>
        </div>
      )}

      <p data-testid="matrix-coverage-note" style={{ ...muted, marginTop: 10, maxWidth: 720 }}>
        {T.coverageNote[L]}
      </p>

      <div style={panel}>
        <strong>{T.catalogue[L]}</strong>
        <ul style={{ ...muted, margin: "6px 0 0", paddingLeft: 18 }}>
          {data.accounts.map((a) => (
            <li key={a.account_id} data-testid={`matrix-catalogue-${a.account_id}`}>
              <code>{a.broker}</code> — {a.catalogue.state === "synced" ? T.synced[L]
                : a.catalogue.state === "stale" ? T.stale[L] : T.empty[L]}
              {a.catalogue.state !== "empty" && ` (${a.catalogue.enabled_count}/${a.catalogue.symbol_count})`}
            </li>
          ))}
        </ul>
      </div>

      <table style={{ width: "100%", borderCollapse: "collapse", marginTop: 14 }}>
        <thead>
          <tr style={{ color: "#9db6d6", borderBottom: "1px solid rgba(74,179,255,0.2)" }}>
            <th style={cell}>{T.pair[L]}</th>
            <th style={cell}>{T.eligible[L]}</th>
            <th style={cell}>✔</th>
            <th style={cell}>?</th>
            <th style={cell}>⚠</th>
            <th style={cell}>{T.coverage[L]}</th>
            <th style={cell} />
          </tr>
        </thead>
        <tbody>
          {data.directions.map((d) => {
            const key = `${d.master_account_id}-${d.follower_account_id}`;
            const isOpen = open === key;
            return (
              <React.Fragment key={key}>
                <tr data-testid={`matrix-pair-${key}`} style={{ borderBottom: "1px solid rgba(74,179,255,0.08)" }}>
                  <td style={cell}>
                    <code>{accountsById.get(d.master_account_id) || d.master_account_id}</code>
                    {" → "}
                    <code>{accountsById.get(d.follower_account_id) || d.follower_account_id}</code>
                  </td>
                  <td style={cell}>{d.eligible_source}</td>
                  <td style={{ ...cell, color: CLS.VERIFIED.color }}>{d.counts.VERIFIED}</td>
                  <td style={{ ...cell, color: CLS.UNVERIFIED.color }}>{d.counts.UNVERIFIED}</td>
                  <td style={{ ...cell, color: CLS.AMBIGUOUS.color }}>{d.counts.AMBIGUOUS}</td>
                  <td data-testid={`matrix-coverage-${key}`} style={cell}>
                    {d.coverage_pct === null ? "—" : `${d.coverage_pct}%`}
                  </td>
                  <td style={cell}>
                    <button type="button" aria-expanded={isOpen}
                            aria-label={`${T.inspect[L]} ${d.master_broker} ${d.follower_broker}`}
                            data-testid={`matrix-inspect-${key}`}
                            onClick={() => setOpen(isOpen ? null : key)}
                            style={{ background: "transparent", color: "#4ab3ff", border: "1px solid rgba(74,179,255,0.3)",
                                     borderRadius: 8, padding: "2px 10px", cursor: "pointer", fontSize: "0.8rem" }}>
                      {T.inspect[L]}
                    </button>
                  </td>
                </tr>
                {isOpen && (
                  <tr>
                    <td colSpan={7} style={{ padding: "0 10px 10px" }}>
                      <DirectionDetail d={d} L={L} />
                    </td>
                  </tr>
                )}
              </React.Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

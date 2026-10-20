"use client";

/**
 * Genuine TradersWay withdrawal — PRIVATE evidence case study (WV1 §2, Monday demo screen D).
 *
 * A static, sanitized, credential-free summary of the ONE authenticated real pilot withdrawal. It shows ONLY the
 * verified, redacted facts (amount, rail, email timestamps/interval, authentication, attribution) — NEVER a raw
 * email, account number, confirmation link, wallet address or evidence pointer. The real evidence lives immutably in
 * the production evidence store; this page does not read it (it reproduces reviewed, redacted facts). It is kept
 * deliberately SEPARATE from the synthetic demo dashboard totals and from the WTM aggregate preview.
 */
import { useCallback, useState } from "react";
import Link from "next/link";
import { type Lang, detectLang, setLang as persistLang } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";
import { LanguageDropdown } from "@/components/LanguageDropdown";

const PAGE_BG = "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)";
const CARD_BG = "rgba(255,255,255,0.03)";
const BORDER = "1px solid rgba(74, 179, 255, 0.15)";

const S = {
  title: { en: "Genuine Withdrawal — Private Case Study", ja: "実際の出金 — 非公開ケーススタディ" },
  realBadge: { en: "REAL · AUTHENTICATED ON PRODUCTION", ja: "実データ · 本番で認証済み" },
  lede: {
    en: "This is the single genuine pilot withdrawal GuvFX observed from authenticated broker emails. It is real "
      + "evidence held immutably on production — not synthetic demo data, and not part of the dashboard or WTM "
      + "aggregate figures. All identifying details are redacted.",
    ja: "これは、GuvFXが認証済みブローカーメールから観測した唯一の実際のパイロット出金です。本番に不変で保持される実データであり、"
      + "合成デモデータでも、ダッシュボードやWTM集計の数値の一部でもありません。識別情報はすべて秘匿されています。",
  },
  facts: { en: "Verified facts", ja: "検証済みの事実" },
  authH: { en: "Authentication & integrity", ja: "認証と完全性" },
  attributionH: { en: "Attribution", ja: "帰属" },
  limitsH: { en: "What is NOT claimed", ja: "主張しないこと" },
  redactionH: { en: "Redaction", ja: "秘匿" },
  back: { en: "← GuvFX Brokers", ja: "← GuvFXブローカー" },
};

const FACTS: { k: { en: string; ja: string }; v: string }[] = [
  { k: { en: "Broker", ja: "ブローカー" }, v: "TradersWay" },
  { k: { en: "Type", ja: "種別" }, v: "External withdrawal" },
  { k: { en: "External method", ja: "外部手段" }, v: "Ethereum" },
  { k: { en: "Amount", ja: "金額" }, v: "24.12 USD" },
  { k: { en: "Request observed", ja: "申請の観測" }, v: "2026-10-19 07:55:04 UTC" },
  { k: { en: "Completion observed", ja: "完了の観測" }, v: "2026-10-19 08:28:05 UTC" },
  { k: { en: "Email-to-email interval", ja: "メール間隔" }, v: "33m01s (1981s)" },
  { k: { en: "Notifications", ja: "通知" }, v: "Request + completion emails, linked by one broker reference" },
];

const AUTH = {
  en: ["Sender authenticated: DKIM pass + SPF pass, from the broker's registrable domain.",
       "Content-hash verified on both messages (immutable evidence; re-read reproduces the same bytes).",
       "Broker-reported completion only — wallet/funds receipt is NOT independently certified by GuvFX."],
  ja: ["送信者認証: DKIM合格 + SPF合格、ブローカーの登録ドメインから。",
       "両メッセージで内容ハッシュを検証（不変の証跡。再読込で同一バイト列を再現）。",
       "ブローカー報告の完了のみ — ウォレット/着金はGuvFXが独立に証明していません。"],
};
const ATTRIBUTION = {
  en: ["Owner-attributed to the member whose connected mailbox received the broker emails.",
       "Account UNRESOLVED: the TradersWay account the withdrawal was made from is not yet registered in GuvFX — "
       + "so it is surfaced as owner-attributed / account-unresolved, never bound to an unrelated account."],
  ja: ["ブローカーメールを受信した接続メールボックスの会員に帰属。",
       "口座は未特定: 出金元のTradersWay口座はGuvFXに未登録のため、所有者帰属/口座未特定として表示し、"
       + "無関係な口座には決して紐付けません。"],
};
const LIMITS = {
  en: ["No claim that GuvFX initiated, confirmed, or cancelled the withdrawal — GuvFX never does.",
       "No claim of independent wallet/funds receipt.",
       "This single real case is not used in any public broker ranking or aggregate."],
  ja: ["GuvFXが出金を開始・承認・取消したという主張はありません — GuvFXは決して行いません。",
       "ウォレット/着金の独立確認の主張はありません。",
       "この唯一の実ケースは公開ブローカー評価や集計には一切使用しません。"],
};

export default function EvidenceCaseStudyPage() {
  const [lang, setLangState] = useState<Lang>(() => (typeof window === "undefined" ? "en" : detectLang()));
  const L: Lang = lang === "ja" ? "ja" : "en";
  const onLang = useCallback((next: Lang) => { persistLang(next); setLangState(next); }, []);

  const section = (title: string, items: string[]) => (
    <div style={{ marginTop: 16, border: BORDER, borderRadius: 12, padding: "1rem 1.2rem", background: CARD_BG }}>
      <div style={{ fontSize: "0.72rem", color: "#9fb0c7", textTransform: "uppercase", letterSpacing: "0.05em" }}>{title}</div>
      <ul style={{ margin: "8px 0 0", paddingLeft: 18, color: "#c7d6e8", fontSize: "0.85rem", lineHeight: 1.6 }}>
        {items.map((t, i) => <li key={i}>{t}</li>)}
      </ul>
    </div>
  );

  return (
    <div style={{ minHeight: "100vh", width: "100%", display: "flex", flexDirection: "column", background: PAGE_BG,
                  color: "#e5f4ff", fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif" }}>
      <nav style={{ padding: "1rem 2rem", display: "flex", alignItems: "center", justifyContent: "space-between",
                    borderBottom: "1px solid rgba(74, 179, 255, 0.1)" }}>
        <Link href="/brokers" style={{ color: "#9db6d6", textDecoration: "none", fontSize: "0.9rem" }}>{S.back[L]}</Link>
        <LanguageDropdown lang={lang} onChange={onLang} variant="compact" />
      </nav>

      <main style={{ flex: 1, width: "100%", maxWidth: 820, margin: "0 auto", padding: "2rem 1rem 3rem" }} data-testid="case-study">
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <h1 style={{ margin: 0, fontSize: "1.55rem" }}>{S.title[L]}</h1>
          <span style={{ padding: "2px 10px", borderRadius: 999, fontSize: "0.68rem", fontWeight: 700,
                         background: "rgba(90,209,154,0.14)", border: "1px solid rgba(90,209,154,0.5)", color: "#7ee3b0" }}>
            {S.realBadge[L]}
          </span>
        </div>
        <p style={{ color: "#9db6d6", fontSize: "0.9rem", marginTop: 8, lineHeight: 1.6 }}>{S.lede[L]}</p>

        <div style={{ marginTop: 16, border: BORDER, borderRadius: 12, padding: "1rem 1.2rem", background: CARD_BG }}>
          <div style={{ fontSize: "0.72rem", color: "#9fb0c7", textTransform: "uppercase", letterSpacing: "0.05em" }}>{S.facts[L]}</div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px 18px", marginTop: 10 }}>
            {FACTS.map((f, i) => (
              <div key={i}>
                <div style={{ fontSize: "0.7rem", color: "#9fb0c7" }}>{f.k[L]}</div>
                <div style={{ fontSize: "0.92rem", fontWeight: 600 }}>{f.v}</div>
              </div>
            ))}
          </div>
        </div>

        {section(S.authH[L], AUTH[L])}
        {section(S.attributionH[L], ATTRIBUTION[L])}
        {section(S.limitsH[L], LIMITS[L])}

        <div style={{ marginTop: 16, border: "1px dashed rgba(201,162,74,0.5)", borderRadius: 12, padding: "0.8rem 1.2rem",
                      background: "rgba(201,162,74,0.07)" }}>
          <div style={{ fontSize: "0.72rem", color: "#e7c77a", textTransform: "uppercase", letterSpacing: "0.05em" }}>{S.redactionH[L]}</div>
          <div style={{ fontSize: "0.82rem", color: "#d9c79a", marginTop: 4, lineHeight: 1.5 }}>
            {L === "ja"
              ? "生メール、口座番号、確認リンク、ウォレットアドレス、証跡ポインタは一切表示していません。"
              : "No raw email, account number, confirmation link, wallet address or evidence pointer is shown."}
          </div>
        </div>
      </main>

      <LegalFooter lang={lang} />
    </div>
  );
}

"use client";

/**
 * Genuine TradersWay withdrawal — PRIVATE evidence case study (WV1 §2, Monday demo screen D).
 *
 * DARK-graceful: it fetches GET /api/public/case-study/ (broker_public), which 404s until
 * BROKER_PUBLIC_CASE_STUDY_ENABLED is armed (isolated demo only) — so the redacted real-withdrawal facts are not even
 * served (or shipped in the client bundle) until the demo is armed. The payload carries ONLY verified, redacted facts
 * (amount, rail, timestamps, interval, authentication, attribution) — NEVER a raw email, account number, confirmation
 * link, wallet address or evidence pointer. The real evidence lives immutably on production; this page does not read
 * it. Kept deliberately SEPARATE from the synthetic demo dashboard totals and the WTM aggregate preview.
 */
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { type Lang, detectLang, setLang as persistLang } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";
import { LanguageDropdown } from "@/components/LanguageDropdown";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "https://api.guvfx.com";
const PAGE_BG = "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)";
const CARD_BG = "rgba(255,255,255,0.03)";
const BORDER = "1px solid rgba(74, 179, 255, 0.15)";

type Bi = { en: string; ja: string };
type Payload = {
  real: boolean; authenticated_on_production: boolean; title: Bi; badge: Bi; lede: Bi;
  facts: { k: Bi; v: string }[]; authentication: { en: string[]; ja: string[] };
  attribution: { en: string[]; ja: string[] }; limits: { en: string[]; ja: string[] }; redaction: Bi;
};
type LoadState = "loading" | "unavailable" | "ready";

const S = {
  back: { en: "← GuvFX Brokers", ja: "← GuvFXブローカー" },
  facts: { en: "Verified facts", ja: "検証済みの事実" },
  authH: { en: "Authentication & integrity", ja: "認証と完全性" },
  attributionH: { en: "Attribution", ja: "帰属" },
  limitsH: { en: "What is NOT claimed", ja: "主張しないこと" },
  redactionH: { en: "Redaction", ja: "秘匿" },
  unavailable: { en: "The evidence case study is not available in this environment.", ja: "この環境では証跡ケーススタディは利用できません。" },
};

export default function EvidenceCaseStudyPage() {
  const [lang, setLangState] = useState<Lang>(() => (typeof window === "undefined" ? "en" : detectLang()));
  const L: Lang = lang === "ja" ? "ja" : "en";
  const [state, setState] = useState<LoadState>("loading");
  const [data, setData] = useState<Payload | null>(null);
  const onLang = useCallback((next: Lang) => { persistLang(next); setLangState(next); }, []);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/public/case-study/`, { method: "GET" })
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) { setState("unavailable"); return; }
        const d = (await res.json()) as Payload;
        if (cancelled) return;
        setData(d);
        setState("ready");
      })
      .catch(() => { if (!cancelled) setState("unavailable"); });
    return () => { cancelled = true; };
  }, []);

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
        {state === "loading" && <p style={{ color: "#9db6d6" }}>…</p>}
        {state === "unavailable" && (
          <div style={{ border: BORDER, borderRadius: 12, padding: "1.25rem 1.5rem", background: CARD_BG }}>
            <p style={{ color: "#9db6d6", margin: 0 }}>{S.unavailable[L]}</p>
          </div>
        )}

        {state === "ready" && data && (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
              <h1 style={{ margin: 0, fontSize: "1.55rem" }}>{data.title[L]}</h1>
              <span style={{ padding: "2px 10px", borderRadius: 999, fontSize: "0.68rem", fontWeight: 700,
                             background: "rgba(90,209,154,0.14)", border: "1px solid rgba(90,209,154,0.5)", color: "#7ee3b0" }}>
                {data.badge[L]}
              </span>
            </div>
            <p style={{ color: "#9db6d6", fontSize: "0.9rem", marginTop: 8, lineHeight: 1.6 }}>{data.lede[L]}</p>

            <div style={{ marginTop: 16, border: BORDER, borderRadius: 12, padding: "1rem 1.2rem", background: CARD_BG }}>
              <div style={{ fontSize: "0.72rem", color: "#9fb0c7", textTransform: "uppercase", letterSpacing: "0.05em" }}>{S.facts[L]}</div>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "10px 18px", marginTop: 10 }}>
                {data.facts.map((f, i) => (
                  <div key={i}>
                    <div style={{ fontSize: "0.7rem", color: "#9fb0c7" }}>{f.k[L]}</div>
                    <div style={{ fontSize: "0.92rem", fontWeight: 600 }}>{f.v}</div>
                  </div>
                ))}
              </div>
            </div>

            {section(S.authH[L], data.authentication[L])}
            {section(S.attributionH[L], data.attribution[L])}
            {section(S.limitsH[L], data.limits[L])}

            <div style={{ marginTop: 16, border: "1px dashed rgba(201,162,74,0.5)", borderRadius: 12, padding: "0.8rem 1.2rem",
                          background: "rgba(201,162,74,0.07)" }}>
              <div style={{ fontSize: "0.72rem", color: "#e7c77a", textTransform: "uppercase", letterSpacing: "0.05em" }}>{S.redactionH[L]}</div>
              <div style={{ fontSize: "0.82rem", color: "#d9c79a", marginTop: 4, lineHeight: 1.5 }}>{data.redaction[L]}</div>
            </div>
          </>
        )}
      </main>

      <LegalFooter lang={lang} />
    </div>
  );
}

"use client";

/**
 * Public broker profile + aggregate metrics (MULTI_ACCOUNT §E/§G).
 *
 * Unauthenticated, read-only. Consumes GET /api/public/brokers/<slug>/ (DARK — 404 until armed). Renders ONLY the
 * aggregate-only projection the API returns (sample size, contributor count, window, freshness, methodology version,
 * and computed stats) and shows the truthful INSUFFICIENT_DATA state when a metric is below threshold — NEVER a member
 * identity, email, account number, transaction, or amount. en/ja via t().
 */
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { type Lang, detectLang, setLang as persistLang, t } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";
import { LanguageDropdown } from "@/components/LanguageDropdown";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "https://api.guvfx.com";

type Metric = {
  category: string;
  state: string;
  methodology_version?: string;
  sample_size?: number;
  contributor_count?: number;
  observation_window?: { start: string | null; end: string | null };
  last_update?: string | null;
  data_freshness?: string | null;
  stats?: Record<string, unknown>;
};
type Profile = { slug: string; display_name: string; summary: string; website: string; metrics: Metric[] };
type LoadState = "loading" | "unavailable" | "notfound" | "ready";

const PAGE_BG = "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)";
const CARD_BG = "rgba(255,255,255,0.03)";
const BORDER = "1px solid rgba(74, 179, 255, 0.15)";

function categoryLabel(lang: Lang, category: string): string {
  if (category === "WITHDRAWAL_PROCESSING") return t(lang, "brokerMetrics.withdrawalProcessing");
  return category; // unknown category: show its raw key rather than inventing a label
}

export default function PublicBrokerProfilePage() {
  const params = useParams<{ slug: string }>();
  const slug = typeof params?.slug === "string" ? params.slug : Array.isArray(params?.slug) ? params.slug[0] : "";
  const [lang, setLangState] = useState<Lang>(() => (typeof window === "undefined" ? "en" : detectLang()));
  const [state, setState] = useState<LoadState>("loading");
  const [profile, setProfile] = useState<Profile | null>(null);

  useEffect(() => {
    if (!slug) return;
    let cancelled = false;
    fetch(`${API_BASE}/api/public/brokers/${encodeURIComponent(slug)}/`, { method: "GET" })
      .then(async (res) => {
        if (cancelled) return;
        if (res.status === 404) { setState("unavailable"); return; } // DARK or missing — calm, not an error
        if (!res.ok) { setState("unavailable"); return; }
        const data = await res.json();
        setProfile(data);
        setState("ready");
      })
      .catch(() => { if (!cancelled) setState("unavailable"); });
    return () => { cancelled = true; };
  }, [slug]);

  const onLang = useCallback((next: Lang) => { persistLang(next); setLangState(next); }, []);

  return (
    <div style={{ minHeight: "100vh", width: "100%", display: "flex", flexDirection: "column",
                  background: PAGE_BG, color: "#e5f4ff",
                  fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif" }}>
      <nav style={{ padding: "1rem 2rem", display: "flex", alignItems: "center", justifyContent: "space-between",
                    borderBottom: "1px solid rgba(74, 179, 255, 0.1)" }}>
        <Link href="/" style={{ display: "flex", alignItems: "center", gap: "0.5rem", textDecoration: "none" }}>
          <div style={{ width: 32, height: 32, borderRadius: 8,
                        background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)", display: "flex",
                        alignItems: "center", justifyContent: "center", fontWeight: 700, fontSize: "0.9rem",
                        color: "#fff" }}>G</div>
          <span style={{ fontSize: "1.25rem", fontWeight: 600,
                         background: "linear-gradient(120deg, #4ab3ff 0%, #7af0ff 40%, #4ab3ff 100%)",
                         WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent" }}>GuvFX</span>
        </Link>
        <LanguageDropdown lang={lang} onChange={onLang} variant="compact" />
      </nav>

      <main style={{ flex: 1, padding: "2rem", maxWidth: 820, margin: "0 auto", width: "100%" }}>
        <Link href="/brokers" style={{ color: "#4ab3ff", fontSize: "0.85rem", textDecoration: "none" }}>
          {t(lang, "brokerDir.back")}
        </Link>

        {state === "loading" && <p style={{ color: "#9db6d6", marginTop: "1.5rem" }}>{t(lang, "brokerDir.loading")}</p>}

        {state === "unavailable" && (
          <Panel title={t(lang, "brokerDir.notFoundTitle")} body={t(lang, "brokerDir.notFoundBody")} />
        )}

        {state === "ready" && profile && (
          <>
            <h1 style={{ fontSize: "1.9rem", fontWeight: 700, margin: "1.25rem 0 0.5rem" }}>{profile.display_name}</h1>
            {profile.summary && <p style={{ color: "#9db6d6", lineHeight: 1.6, margin: "0 0 1.5rem" }}>{profile.summary}</p>}

            {(!profile.metrics || profile.metrics.length === 0) && (
              <Panel title={t(lang, "brokerMetrics.noneTitle")} body={t(lang, "brokerDir.emptyBody")} />
            )}

            {(profile.metrics || []).map((m) => (
              <div key={m.category} style={{ background: CARD_BG, border: BORDER, borderRadius: 12,
                                             padding: "1.5rem", marginBottom: "1rem" }}>
                <div style={{ fontSize: "1.05rem", fontWeight: 600, marginBottom: "0.75rem" }}>
                  {categoryLabel(lang, m.category)}
                </div>
                {m.state === "PUBLISHED" ? (
                  <>
                    <Stats stats={m.stats || {}} />
                    <Meta lang={lang} m={m} />
                  </>
                ) : (
                  <>
                    <div style={{ fontWeight: 600, color: "#ffd27a", marginBottom: "0.4rem" }}>
                      {t(lang, "brokerMetrics.insufficientTitle")}
                    </div>
                    <div style={{ color: "#9db6d6", lineHeight: 1.6 }}>{t(lang, "brokerMetrics.insufficientBody")}</div>
                  </>
                )}
              </div>
            ))}
          </>
        )}

        <p style={{ color: "#6f86a6", fontSize: "0.8rem", lineHeight: 1.6, marginTop: "2rem",
                    borderTop: "1px solid rgba(74,179,255,0.1)", paddingTop: "1.25rem" }}>
          {t(lang, "brokerDir.disclaimer")}
        </p>
      </main>

      <LegalFooter lang={lang} />
    </div>
  );
}

function Stats({ stats }: { stats: Record<string, unknown> }) {
  const entries = Object.entries(stats || {});
  if (entries.length === 0) return null;
  return (
    <div style={{ display: "grid", gap: "0.4rem", marginBottom: "1rem" }}>
      {entries.map(([k, v]) => (
        <div key={k} style={{ display: "flex", justifyContent: "space-between", gap: "1rem", fontSize: "0.9rem" }}>
          <span style={{ color: "#9db6d6" }}>{k}</span>
          <span style={{ fontVariantNumeric: "tabular-nums" }}>{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
        </div>
      ))}
    </div>
  );
}

function Meta({ lang, m }: { lang: Lang; m: Metric }) {
  const row = (label: string, value?: string | number | null) =>
    value === undefined || value === null || value === "" ? null : (
      <div style={{ display: "flex", justifyContent: "space-between", gap: "1rem" }}>
        <span>{label}</span><span>{value}</span>
      </div>
    );
  const win = m.observation_window;
  const windowText = win && (win.start || win.end) ? `${win.start || "—"} → ${win.end || "—"}` : undefined;
  return (
    <div style={{ color: "#6f86a6", fontSize: "0.8rem", display: "grid", gap: "0.3rem",
                  borderTop: "1px solid rgba(74,179,255,0.08)", paddingTop: "0.75rem" }}>
      {row(t(lang, "brokerMetrics.sampleSize"), m.sample_size)}
      {row(t(lang, "brokerMetrics.contributors"), m.contributor_count)}
      {row(t(lang, "brokerMetrics.window"), windowText)}
      {row(t(lang, "brokerMetrics.lastUpdate"), m.data_freshness || m.last_update || undefined)}
      {row(t(lang, "brokerMetrics.methodology"), m.methodology_version)}
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

"use client";

/**
 * Public broker performance directory (MULTI_ACCOUNT §E/§G).
 *
 * Unauthenticated, read-only. Consumes the DARK aggregate API GET /api/public/brokers/ (broker_public), which 404s
 * until BROKER_PUBLIC_DIRECTORY_ENABLED is armed — so this page is written DARK-graceful: a 404 or network error shows
 * a calm "not yet available" panel, an empty list shows "no brokers published yet", and it NEVER renders a member
 * identity, email, account number, transaction, or amount (the API returns aggregate-only data). en/ja via t().
 */
import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { type Lang, detectLang, setLang as persistLang, t } from "@/lib/i18n";
import { LegalFooter } from "@/components/LegalFooter";
import { LanguageDropdown } from "@/components/LanguageDropdown";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "https://api.guvfx.com";

type PublicBroker = { slug: string; display_name: string; summary: string; website: string };
type LoadState = "loading" | "unavailable" | "ready";

const PAGE_BG = "radial-gradient(circle at 0 0, #12263f 0, #050816 40%, #050816 100%)";
const CARD_BG = "rgba(255,255,255,0.03)";
const BORDER = "1px solid rgba(74, 179, 255, 0.15)";

export default function PublicBrokerDirectoryPage() {
  const [lang, setLangState] = useState<Lang>(() => (typeof window === "undefined" ? "en" : detectLang()));
  const [state, setState] = useState<LoadState>("loading");
  const [brokers, setBrokers] = useState<PublicBroker[]>([]);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/public/brokers/`, { method: "GET" })
      .then(async (res) => {
        if (cancelled) return;
        if (!res.ok) {
          setState("unavailable"); // DARK (404) or server error — feature not published yet
          return;
        }
        const data = await res.json();
        setBrokers(Array.isArray(data?.brokers) ? data.brokers : []);
        setState("ready");
      })
      .catch(() => {
        if (!cancelled) setState("unavailable"); // network error — fail to the calm "unavailable" state
      });
    return () => { cancelled = true; };
  }, []);

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

      <main style={{ flex: 1, padding: "3rem 2rem 4rem", maxWidth: 960, margin: "0 auto", width: "100%" }}>
        <h1 style={{ fontSize: "2rem", fontWeight: 700, margin: "0 0 0.75rem" }}>{t(lang, "brokerDir.title")}</h1>
        <p style={{ color: "#9db6d6", maxWidth: 680, lineHeight: 1.6, margin: "0 0 2rem" }}>
          {t(lang, "brokerDir.subtitle")}
        </p>

        {state === "loading" && <p style={{ color: "#9db6d6" }}>{t(lang, "brokerDir.loading")}</p>}

        {state === "unavailable" && (
          <Panel title={t(lang, "brokerDir.unavailableTitle")} body={t(lang, "brokerDir.unavailableBody")} />
        )}

        {state === "ready" && brokers.length === 0 && (
          <Panel title={t(lang, "brokerDir.emptyTitle")} body={t(lang, "brokerDir.emptyBody")} />
        )}

        {state === "ready" && brokers.length > 0 && (
          <div style={{ display: "grid", gap: "1rem" }}>
            {brokers.map((b) => (
              <Link key={b.slug} href={`/brokers/${encodeURIComponent(b.slug)}`}
                    style={{ textDecoration: "none", color: "inherit" }}>
                <div style={{ background: CARD_BG, border: BORDER, borderRadius: 12, padding: "1.25rem 1.5rem",
                              display: "flex", justifyContent: "space-between", alignItems: "center", gap: "1rem" }}>
                  <div>
                    <div style={{ fontSize: "1.1rem", fontWeight: 600 }}>{b.display_name}</div>
                    {b.summary && <div style={{ color: "#9db6d6", fontSize: "0.9rem", marginTop: 4 }}>{b.summary}</div>}
                  </div>
                  <span style={{ color: "#4ab3ff", fontSize: "0.85rem", whiteSpace: "nowrap" }}>
                    {t(lang, "brokerDir.viewMetrics")} →
                  </span>
                </div>
              </Link>
            ))}
          </div>
        )}

        <p style={{ color: "#6f86a6", fontSize: "0.8rem", lineHeight: 1.6, marginTop: "2.5rem",
                    borderTop: "1px solid rgba(74,179,255,0.1)", paddingTop: "1.25rem" }}>
          {t(lang, "brokerDir.disclaimer")}
        </p>
      </main>

      <LegalFooter lang={lang} />
    </div>
  );
}

function Panel({ title, body }: { title: string; body: string }) {
  return (
    <div style={{ background: CARD_BG, border: BORDER, borderRadius: 12, padding: "2rem" }}>
      <div style={{ fontSize: "1.1rem", fontWeight: 600, marginBottom: "0.5rem" }}>{title}</div>
      <div style={{ color: "#9db6d6", lineHeight: 1.6 }}>{body}</div>
    </div>
  );
}

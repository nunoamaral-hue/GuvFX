"use client";

import Link from "next/link";
import { useState } from "react";
import { detectLang, type Lang } from "@/lib/i18n";

const copy = {
  en: {
    eyebrow: "Closed beta support",
    title: "How can we help?",
    body: "If your workspace or strategy needs attention, send us a short description and we’ll help you find the next step.",
    includeTitle: "Please include",
    include: ["The page you were using", "What you expected to happen", "What the page currently shows"],
    safety: "Never include your broker password, GuvFX password, or verification code.",
    email: "Email support",
    setup: "Return to hosted setup",
    strategies: "Return to My Strategies",
  },
  ja: {
    eyebrow: "クローズドベータ・サポート",
    title: "どのようなことでお困りですか？",
    body: "ワークスペースや戦略に確認が必要と表示された場合は、状況を簡単にお知らせください。次の手順をご案内します。",
    includeTitle: "次の内容をお知らせください",
    include: ["操作していたページ", "行おうとしていたこと", "現在ページに表示されている内容"],
    safety: "取引口座のパスワード、GuvFXのパスワード、確認コードは送らないでください。",
    email: "サポートにメール",
    setup: "ホステッド設定に戻る",
    strategies: "マイ戦略に戻る",
  },
} as const;

export default function SupportPage() {
  const [lang] = useState<Lang>(() => typeof window === "undefined" ? "en" : detectLang());
  const c = copy[lang];
  return (
    <main style={{ minHeight: "100vh", padding: "clamp(1rem, 5vw, 4rem)", boxSizing: "border-box",
      background: "radial-gradient(circle at top left, #0b1930, #050816 58%, #030610)", color: "#e9f4ff" }}>
      <div style={{ width: "100%", maxWidth: 680, margin: "0 auto" }}>
        <Link href="/" style={{ color: "#7dd3fc", textDecoration: "none", fontWeight: 700 }}>GuvFX</Link>
        <section style={{ marginTop: "clamp(2rem, 8vw, 5rem)", padding: "clamp(1.25rem, 5vw, 2.5rem)",
          borderRadius: 18, border: "1px solid rgba(74,179,255,0.18)", background: "rgba(6,11,29,0.92)",
          boxShadow: "0 20px 60px rgba(0,0,0,0.45)" }}>
          <p style={{ margin: 0, color: "#4ab3ff", fontSize: "0.75rem", textTransform: "uppercase", letterSpacing: "0.12em" }}>
            {c.eyebrow}
          </p>
          <h1 style={{ margin: "0.6rem 0 0", fontSize: "clamp(1.7rem, 6vw, 2.5rem)" }}>{c.title}</h1>
          <p style={{ color: "#b7c5dd", lineHeight: 1.7 }}>{c.body}</p>
          <h2 style={{ marginTop: "1.5rem", fontSize: "1rem" }}>{c.includeTitle}</h2>
          <ul style={{ color: "#b7c5dd", lineHeight: 1.8, paddingLeft: "1.25rem" }}>
            {c.include.map((item) => <li key={item}>{item}</li>)}
          </ul>
          <p style={{ padding: "0.75rem 0.9rem", borderRadius: 10, background: "rgba(251,191,36,0.07)",
            border: "1px solid rgba(251,191,36,0.2)", color: "#fde68a", lineHeight: 1.55 }}>
            {c.safety}
          </p>
          <a href="mailto:support@guvfx.com?subject=GuvFX%20beta%20support" style={{ display: "inline-flex", marginTop: "0.5rem",
            padding: "0.75rem 1.3rem", borderRadius: 999, color: "white", textDecoration: "none", fontWeight: 700,
            background: "linear-gradient(135deg,#2979ff,#3fe0ff,#2979ff)" }}>{c.email}</a>
          <nav style={{ display: "flex", flexWrap: "wrap", gap: "0.75rem 1.25rem", marginTop: "1.5rem" }}>
            <Link href="/onboarding/hosted" style={{ color: "#93c5fd" }}>{c.setup}</Link>
            <Link href="/strategies" style={{ color: "#93c5fd" }}>{c.strategies}</Link>
          </nav>
        </section>
      </div>
    </main>
  );
}

"use client";

import { type Lang, t } from "@/lib/i18n";

type LegalFooterProps = {
  lang: Lang;
};

/**
 * Small legal disclaimer footer.
 * Renders two lines of legal text (i18n-aware).
 * Use this on landing, login, register, and inside AppShell.
 */
export function LegalFooter({ lang }: LegalFooterProps) {
  return (
    <footer
      style={{
        width: "100%",
        padding: "1rem 1.5rem",
        textAlign: "center",
        borderTop: "1px solid rgba(255, 255, 255, 0.05)",
        background: "transparent",
      }}
    >
      <p
        style={{
          margin: 0,
          fontSize: "0.7rem",
          color: "#5a6a7e",
          lineHeight: 1.6,
          maxWidth: 600,
          marginLeft: "auto",
          marginRight: "auto",
        }}
      >
        {t(lang, "legal.footerLine1")}
      </p>
      <p
        style={{
          margin: "0.25rem 0 0",
          fontSize: "0.7rem",
          color: "#5a6a7e",
          lineHeight: 1.6,
          maxWidth: 600,
          marginLeft: "auto",
          marginRight: "auto",
        }}
      >
        {t(lang, "legal.footerLine2")}
      </p>
    </footer>
  );
}

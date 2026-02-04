"use client";

import { useEffect, useRef, useState } from "react";
import { type Lang } from "@/lib/i18n";

// =============================================================================
// LANGUAGE DROPDOWN — reusable across public pages + AppShell topbar
//
// Props:
//   lang      — current language ("en" | "ja")
//   onChange  — called when user picks a new language
//   variant   — "compact" (topbar) | "full" (standalone CTA row)
//
// Features:
//   - Emoji flags (🇬🇧 / 🇯🇵) — no extra assets
//   - Click-outside + Escape to close
//   - Active-item checkmark
//   - Chevron rotation animation
//   - Dark neon theme consistent with the rest of the UI
// =============================================================================

type LanguageDropdownProps = {
  lang: Lang;
  onChange: (next: Lang) => void;
  variant?: "compact" | "full";
};

type LangOption = {
  code: Lang;
  flag: string;
  label: string;
  shortLabel: string;
};

const LANG_OPTIONS: LangOption[] = [
  { code: "en", flag: "🇬🇧", label: "English", shortLabel: "EN" },
  { code: "ja", flag: "🇯🇵", label: "日本語", shortLabel: "JP" },
];

export function LanguageDropdown({ lang, onChange, variant = "full" }: LanguageDropdownProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  const current = LANG_OPTIONS.find((o) => o.code === lang) ?? LANG_OPTIONS[0];

  // Close on outside click or Escape
  useEffect(() => {
    if (!open) return;

    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function handleEscape(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }

    document.addEventListener("mousedown", handleClickOutside);
    document.addEventListener("keydown", handleEscape);
    return () => {
      document.removeEventListener("mousedown", handleClickOutside);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [open]);

  const isCompact = variant === "compact";

  return (
    <div ref={ref} style={{ position: "relative" }}>
      {/* Trigger button */}
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        style={{
          display: "flex",
          alignItems: "center",
          gap: isCompact ? "0.3rem" : "0.45rem",
          padding: isCompact ? "0.35rem 0.6rem" : "0.7rem 1rem",
          borderRadius: isCompact ? 6 : 999,
          border: "1px solid rgba(255,255,255,0.12)",
          fontSize: isCompact ? "0.75rem" : "0.9rem",
          fontWeight: 500,
          cursor: "pointer",
          background: "rgba(255,255,255,0.04)",
          color: "#c2d5ff",
          transition: "background 150ms ease, color 150ms ease",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.background = "rgba(255,255,255,0.08)";
          e.currentTarget.style.color = "#e5f4ff";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.background = "rgba(255,255,255,0.04)";
          e.currentTarget.style.color = "#c2d5ff";
        }}
      >
        <span style={{ fontSize: isCompact ? "0.85rem" : "1rem" }}>{current.flag}</span>
        <span>{isCompact ? current.shortLabel : current.label}</span>
        <svg
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          style={{
            transform: open ? "rotate(180deg)" : "rotate(0deg)",
            transition: "transform 200ms ease",
            marginLeft: "0.1rem",
            flexShrink: 0,
          }}
        >
          <path d="M6 9l6 6 6-6" />
        </svg>
      </button>

      {/* Dropdown menu */}
      {open && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 6px)",
            right: isCompact ? 0 : undefined,
            left: isCompact ? undefined : 0,
            minWidth: 160,
            background: "rgba(10, 15, 30, 0.95)",
            backdropFilter: "blur(12px)",
            border: "1px solid rgba(255,255,255,0.12)",
            borderRadius: 10,
            boxShadow: "0 10px 40px rgba(0,0,0,0.6)",
            zIndex: 1000,
            overflow: "hidden",
          }}
        >
          {LANG_OPTIONS.map((opt) => {
            const isActive = opt.code === lang;
            return (
              <button
                key={opt.code}
                type="button"
                onClick={() => {
                  onChange(opt.code);
                  setOpen(false);
                }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "0.5rem",
                  width: "100%",
                  padding: "0.6rem 0.9rem",
                  background: isActive ? "rgba(74, 179, 255, 0.1)" : "transparent",
                  border: "none",
                  color: isActive ? "#e5f4ff" : "#9ca3af",
                  fontSize: "0.85rem",
                  cursor: "pointer",
                  transition: "background 150ms ease, color 150ms ease",
                }}
                onMouseEnter={(e) => {
                  if (!isActive) {
                    e.currentTarget.style.background = "rgba(255,255,255,0.05)";
                    e.currentTarget.style.color = "#e5f4ff";
                  }
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.background = isActive
                    ? "rgba(74, 179, 255, 0.1)"
                    : "transparent";
                  e.currentTarget.style.color = isActive ? "#e5f4ff" : "#9ca3af";
                }}
              >
                <span style={{ fontSize: "1.1rem" }}>{opt.flag}</span>
                <span>{opt.label}</span>
                {isActive && (
                  <svg
                    width="14"
                    height="14"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="#4ab3ff"
                    strokeWidth="2.5"
                    style={{ marginLeft: "auto" }}
                  >
                    <path d="M20 6L9 17l-5-5" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

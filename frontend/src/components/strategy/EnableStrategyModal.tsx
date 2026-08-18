"use client";

import { Button } from "@/components/ui/Button";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";

// AJ#7 — the Enable Strategy confirmation modal. This IS the customer's EXPLICIT consent for automated
// execution (folding ADR-0047 workspace authorization into the single Enable action). It never authorizes
// or arms on its own — it only surfaces the consent; the parent runs the orchestration on onConfirm, so the
// authorization write happens strictly from this explicit click, never on page load.

export type EnableStrategyModalProps = {
  open: boolean;
  /** Customer-facing account label, e.g. "IS6 Technologies LTD (1302587)". */
  accountLabel: string;
  strategyName: string;
  /** True while the authorize→arm orchestration is running. */
  busy: boolean;
  /** A retryable, customer-safe error from a previous attempt (partial failure). Null when clean. */
  error: string | null;
  onConfirm: () => void;
  onCancel: () => void;
};

export function EnableStrategyModal({
  open,
  accountLabel,
  strategyName,
  busy,
  error,
  onConfirm,
  onCancel,
}: EnableStrategyModalProps) {
  const lang = useLang();
  if (!open) return null;
  return (
    <div
      role="presentation"
      onClick={() => { if (!busy) onCancel(); }}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(2,6,20,0.72)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "1rem",
        zIndex: 1000,
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t(lang, "enableModal.aria")}
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "100%",
          maxWidth: 460,
          border: "1px solid rgba(255,255,255,0.12)",
          borderRadius: 14,
          background: "linear-gradient(180deg, rgba(12,18,38,0.98), rgba(8,12,28,0.99))",
          boxShadow: "0 20px 60px rgba(0,0,0,0.6)",
          padding: "1.4rem",
        }}
      >
        <h2 style={{ fontSize: "1.2rem", fontWeight: 700, color: "#f1f5ff", margin: "0 0 0.5rem" }}>
          {t(lang, "enableModal.title")}
        </h2>
        <p style={{ fontSize: "0.9rem", color: "#c7d2e8", lineHeight: 1.55, margin: "0 0 0.75rem" }}>
          {t(lang, "enableModal.body", { account: accountLabel, strategy: strategyName })}
        </p>
        <p style={{ fontSize: "0.78rem", color: "#8fa0b7", lineHeight: 1.5, margin: "0 0 1rem" }}>
          {t(lang, "enableModal.demoNote")}
        </p>

        {error && (
          <div
            role="alert"
            style={{
              marginBottom: "1rem",
              padding: "0.6rem 0.75rem",
              borderRadius: 8,
              border: "1px solid rgba(239,68,68,0.4)",
              background: "rgba(239,68,68,0.1)",
              color: "#fca5a5",
              fontSize: "0.82rem",
              lineHeight: 1.5,
            }}
          >
            {error}
          </div>
        )}

        <div style={{ display: "flex", gap: "0.6rem", justifyContent: "flex-end", flexWrap: "wrap" }}>
          <Button variant="secondary" onClick={onCancel} disabled={busy}>
            {t(lang, "common.cancel")}
          </Button>
          <Button variant="primary" onClick={onConfirm} disabled={busy}>
            {busy ? t(lang, "common.enabling") : error ? t(lang, "common.tryAgain") : t(lang, "enableModal.confirm")}
          </Button>
        </div>
      </div>
    </div>
  );
}

"use client";

import React from "react";
import { formatWhen } from "@/lib/broker-status";
import type { ValidationTimeline } from "@/types/broker";

/** WS-D/Phase-3 — the staff/operator VALIDATION TIMELINE rail. Renders a validation pipeline for one
 * correlation id: each stage with a status icon, the customer-safe label, and (staff) the operator label +
 * reason. Plus the customer-safe summary and the operator summary. Secret-safe: it only renders the
 * allow-listed fields the API returns (masked login, non-secret server, reason code) — never a password,
 * ciphertext, host path, or stack trace. Presentation only; no fetching. */
const ICON: Record<string, { ch: string; color: string; label: string }> = {
  ok: { ch: "✓", color: "#22c55e", label: "done" },
  failed: { ch: "✕", color: "#f59e0b", label: "failed" },
  not_reached: { ch: "○", color: "#64748b", label: "not reached" },
};

function fmtDuration(ms: number | null): string {
  if (ms === null || ms === undefined || Number.isNaN(ms)) return "";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

export const ValidationTimelinePanel: React.FC<{ timeline: ValidationTimeline }> = ({ timeline: t }) => {
  // Phase-4 WS-A (S21): the correlation id is the field support pastes into logs/searches — give it a copy
  // affordance (it stays selectable regardless). Guarded for non-clipboard environments (tests / http).
  const [copied, setCopied] = React.useState(false);
  const copyCorrelation = () => {
    try {
      navigator.clipboard?.writeText(t.correlation_id);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* clipboard unavailable — the value is still selectable */ }
  };
  if (!t.found) {
    return (
      <div style={{ color: "#8fa0b7", fontSize: "0.9rem", padding: "0.75rem 0" }}>
        No validation found for that search. Check the correlation ID / account ID / attempt ID.
      </div>
    );
  }
  return (
    <div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 16, marginBottom: 14, fontSize: "0.82rem", color: "#9fb0c8" }}>
        <span>
          Correlation: <code style={{ color: "#cbd5f5" }}>{t.correlation_id}</code>
          <button type="button" onClick={copyCorrelation} aria-label="Copy correlation ID"
                  style={{ marginLeft: 6, background: "transparent", border: "1px solid rgba(255,255,255,0.18)",
                           borderRadius: 6, color: "#9fb0c8", fontSize: "0.72rem", padding: "1px 6px", cursor: "pointer" }}>
            {copied ? "Copied" : "Copy"}
          </button>
        </span>
        {t.attempt_id != null && <span>Attempt #{t.attempt_id}</span>}
        {t.account_id != null && <span>Account #{t.account_id}</span>}
        {t.server && <span>Server: {t.server}</span>}
        {t.login_masked && <span>Login: {t.login_masked}</span>}
        {t.trigger && <span>Trigger: {t.trigger}</span>}
        {t.duration_ms != null && <span>Total: {fmtDuration(t.duration_ms)}</span>}
      </div>

      {/* Phase-4 WS-A (S19): legend — an amber ✕ is easily read as a "warning"; state plainly that it means
          the stage did NOT complete, so the rail is unambiguous at a glance. */}
      <div style={{ display: "flex", flexWrap: "wrap", gap: 14, marginBottom: 8, fontSize: "0.75rem", color: "#9fb0c8" }}>
        <span><span style={{ color: ICON.ok.color }}>✓</span> done</span>
        <span><span style={{ color: ICON.failed.color }}>✕</span> did not complete</span>
        <span><span style={{ color: ICON.not_reached.color }}>○</span> not reached</span>
      </div>

      <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {t.stages.map((s) => {
          const icon = ICON[s.state] || ICON.not_reached;
          return (
            // Phase-4 WS-A (S5): NO row-level opacity — dimming the whole <li> pushed the (already muted,
            // ~12px) operator label below WCAG AA contrast on the dark panel. The ○ glyph + muted icon colour
            // already signal "not reached"; text stays at full-contrast colours.
            <li key={s.key} style={{ display: "flex", gap: 12, alignItems: "flex-start", padding: "0.35rem 0" }}>
              <span aria-label={icon.label} style={{ color: icon.color, fontSize: "1.05rem", lineHeight: 1.3, width: 16, flexShrink: 0 }}>
                {icon.ch}
              </span>
              <div>
                <div style={{ color: "#e2e8f0", fontSize: "0.9rem" }}>{s.customer_label}</div>
                {/* operator detail — staff-only surface (this whole page is admin-gated) */}
                <div style={{ color: "#9fb0c8", fontSize: "0.8rem" }}>
                  {s.operator_label}{s.state === "failed" && s.reason ? ` — ${s.reason}` : ""}
                </div>
              </div>
            </li>
          );
        })}
      </ol>

      <div style={{ marginTop: 14, borderTop: "1px solid rgba(255,255,255,0.08)", paddingTop: 10 }}>
        <div style={{ color: "#cbd5f5", fontSize: "0.85rem" }}>
          <strong style={{ color: "#8fa0b7" }}>Customer summary:</strong> {t.customer_summary}
        </div>
        <div style={{ color: "#9fb0c8", fontSize: "0.8rem", marginTop: 4 }}>
          <strong style={{ color: "#8fa0b7" }}>Operator:</strong> {t.operator_summary}
        </div>
        {(t.started_at || t.finished_at) && (
          <div style={{ color: "#9fb0c8", fontSize: "0.8rem", marginTop: 4 }}>
            {t.started_at ? `Started ${formatWhen(t.started_at)}` : ""}
            {t.started_at && t.finished_at ? " · " : ""}
            {t.finished_at ? `Finished ${formatWhen(t.finished_at)}` : ""}
          </div>
        )}
      </div>
    </div>
  );
};

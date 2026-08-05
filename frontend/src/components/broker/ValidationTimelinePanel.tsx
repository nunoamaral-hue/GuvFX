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
        <span>Correlation: <code style={{ color: "#cbd5f5" }}>{t.correlation_id}</code></span>
        {t.attempt_id != null && <span>Attempt #{t.attempt_id}</span>}
        {t.account_id != null && <span>Account #{t.account_id}</span>}
        {t.server && <span>Server: {t.server}</span>}
        {t.login_masked && <span>Login: {t.login_masked}</span>}
        {t.trigger && <span>Trigger: {t.trigger}</span>}
        {t.duration_ms != null && <span>Total: {fmtDuration(t.duration_ms)}</span>}
      </div>

      <ol style={{ listStyle: "none", margin: 0, padding: 0 }}>
        {t.stages.map((s) => {
          const icon = ICON[s.state] || ICON.not_reached;
          return (
            <li key={s.key} style={{ display: "flex", gap: 12, alignItems: "flex-start", padding: "0.35rem 0",
                                     opacity: s.state === "not_reached" ? 0.6 : 1 }}>
              <span aria-label={icon.label} style={{ color: icon.color, fontSize: "1.05rem", lineHeight: 1.3, width: 16, flexShrink: 0 }}>
                {icon.ch}
              </span>
              <div>
                <div style={{ color: "#e2e8f0", fontSize: "0.9rem" }}>{s.customer_label}</div>
                {/* operator detail — staff-only surface (this whole page is admin-gated) */}
                <div style={{ color: "#8fa0b7", fontSize: "0.76rem" }}>
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
          <div style={{ color: "#64748b", fontSize: "0.75rem", marginTop: 4 }}>
            {t.started_at ? `Started ${formatWhen(t.started_at)}` : ""}
            {t.started_at && t.finished_at ? " · " : ""}
            {t.finished_at ? `Finished ${formatWhen(t.finished_at)}` : ""}
          </div>
        )}
      </div>
    </div>
  );
};

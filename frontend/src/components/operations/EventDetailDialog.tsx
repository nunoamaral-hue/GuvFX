"use client";

import React from "react";
import { Dialog } from "@/components/broker/Dialog";
import { CategoryBadge, ResolutionBadge, SeverityBadge } from "@/components/operations/OpsBadges";
import { formatWhen } from "@/lib/broker-status";
import type { OperationalEvent } from "@/types/operations";

/** WP5.3 — read-only event detail. Displays the non-secret projection only: summary, reason, source,
 * timestamps, resolution, and a strict FRONTEND allow-list of human-facing metadata keys. It NEVER renders
 * host paths, credentials, ciphertext, stack traces, internal exception text, raw backend state enums
 * (from_state/to_state/status/resulting_status/phase), internal version counters (state_version,
 * requested/current_state_version) or internal identifiers (job_id/plan_id/pause_record_id/runtime_uuid).
 * The metadata allow-list is fail-closed: only keys in SAFE_META_LABELS render, so a NEW backend metadata
 * key can never leak through this surface (belt-and-braces with the WP5.1/5.2 backend allow-lists). */
const label: React.CSSProperties = { color: "#8fa0b7", fontSize: "0.72rem", marginBottom: 3 };
const value: React.CSSProperties = { color: "#e9f4ff", fontSize: "0.9rem", wordBreak: "break-word" };
const rowStyle: React.CSSProperties = { marginBottom: 12 };

const Field: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div style={rowStyle}><div style={label}>{title}</div><div style={value}>{children}</div></div>
);

/** The ONLY metadata keys surfaced by the detail view — human-facing scalars, never raw enums/ids/versions.
 * `reason_code` is intentionally omitted here because it is already rendered as the top-level "Reason". */
const SAFE_META_LABELS: Record<string, string> = {
  is_demo: "Demo account",
  retryable: "Retryable",
  trigger: "Trigger",
  pause_required: "Pause required",
  resume_eligible: "Resume eligible",
  validation_invalidated: "Validation invalidated",
  credential_destroyed: "Credential destroyed",
  disconnected_at: "Disconnected at",
};

function formatMetaValue(key: string, v: unknown): string {
  if (typeof v === "boolean") return v ? "Yes" : "No";
  if (key === "disconnected_at" && typeof v === "string") return formatWhen(v) || v;
  return String(v);
}

export const EventDetailDialog: React.FC<{
  event: OperationalEvent | null;
  onClose: () => void;
}> = ({ event, onClose }) => {
  const metaEntries = event
    ? Object.entries(event.metadata || {}).filter(
        ([k, v]) => k in SAFE_META_LABELS && v !== null && v !== undefined && v !== "",
      )
    : [];
  return (
    <Dialog open={event !== null} onClose={onClose} title="Event detail" labelId="ops-event-detail-title">
      {event && (
        <div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 14 }}>
            <SeverityBadge severity={event.severity} />
            <CategoryBadge category={event.category} />
            <ResolutionBadge resolved={event.resolved} />
          </div>
          <Field title="Summary">{event.summary || "—"}</Field>
          <Field title="Reason">{event.reason_code || "—"}</Field>
          <Field title="Source">{event.source || "—"}</Field>
          <Field title="Event type">{event.event_type || "—"}</Field>
          <Field title="Occurred">{formatWhen(event.timestamp) || "—"}</Field>
          <Field title="Resolved">
            {event.resolved ? (formatWhen(event.resolved_at) || "Yes") : "No"}
          </Field>
          {event.correlation_id ? <Field title="Correlation id">{event.correlation_id}</Field> : null}
          {metaEntries.length > 0 && (
            <div style={rowStyle}>
              <div style={label}>Details</div>
              <div style={{ display: "grid", gap: 4 }}>
                {metaEntries.map(([k, v]) => (
                  <div key={k} style={{ display: "flex", gap: 8, fontSize: "0.82rem" }}>
                    <span style={{ color: "#8fa0b7", minWidth: 130 }}>{SAFE_META_LABELS[k]}</span>
                    <span style={{ color: "#cbd5f5", wordBreak: "break-word" }}>
                      {formatMetaValue(k, v)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </Dialog>
  );
};

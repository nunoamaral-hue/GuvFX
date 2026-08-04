"use client";

import React from "react";
import { Dialog } from "@/components/broker/Dialog";
import { CategoryBadge, ResolutionBadge, SeverityBadge } from "@/components/operations/OpsBadges";
import { formatWhen } from "@/lib/broker-status";
import type { OperationalEvent } from "@/types/operations";

/** WP5.3 — read-only event detail. Displays the non-secret projection only: summary, reason, source,
 * timestamps, resolution, and the allow-listed metadata dict. It NEVER renders host paths, credentials,
 * ciphertext, or stack traces — the WP5.1 API only ever emits non-secret metadata, and this view makes no
 * attempt to interpret it as anything but display key/values. */
const label: React.CSSProperties = { color: "#8fa0b7", fontSize: "0.72rem", marginBottom: 3 };
const value: React.CSSProperties = { color: "#e9f4ff", fontSize: "0.9rem", wordBreak: "break-word" };
const rowStyle: React.CSSProperties = { marginBottom: 12 };

const Field: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div style={rowStyle}><div style={label}>{title}</div><div style={value}>{children}</div></div>
);

export const EventDetailDialog: React.FC<{
  event: OperationalEvent | null;
  onClose: () => void;
}> = ({ event, onClose }) => {
  const metaEntries = event ? Object.entries(event.metadata || {}) : [];
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
                    <span style={{ color: "#8fa0b7", minWidth: 130 }}>{k}</span>
                    <span style={{ color: "#cbd5f5", wordBreak: "break-word" }}>
                      {typeof v === "object" ? JSON.stringify(v) : String(v)}
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

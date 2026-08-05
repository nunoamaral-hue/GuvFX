"use client";

import React from "react";
import { EmptyState } from "@/components/broker/States";
import type { ValidationAttempt } from "@/types/broker";
import { formatWhen, reasonMessage, reasonShort } from "@/lib/broker-status";

/** WP4.2 + Phase-3 WS-D — a support-friendly validation-attempt history (secret-safe fields only). Columns:
 * status icon, time, outcome (concise), summary (customer-safe sentence). The correlation-id column is
 * OPERATOR-only and rendered solely when `staff` is set — customers never see the correlation id or any raw
 * reason code / operator diagnostic. */
const th: React.CSSProperties = {
  textAlign: "left", padding: "0.5rem 0.6rem", fontSize: "0.72rem", color: "#8fa0b7",
  borderBottom: "1px solid rgba(255,255,255,0.1)", whiteSpace: "nowrap", textTransform: "uppercase",
  letterSpacing: "0.02em",
};
const td: React.CSSProperties = {
  padding: "0.55rem 0.6rem", fontSize: "0.85rem", color: "#cbd5f5",
  borderBottom: "1px solid rgba(255,255,255,0.05)", verticalAlign: "top",
};

function icon(status: string): { ch: string; color: string; label: string } {
  if (status === "HEALTHY") return { ch: "✓", color: "#22c55e", label: "verified" };
  if (status === "NEEDS_ATTENTION") return { ch: "!", color: "#f59e0b", label: "needs attention" };
  return { ch: "○", color: "#94a3b8", label: "couldn't complete" };   // UNAVAILABLE / unknown
}

export const ValidationHistoryTable: React.FC<{ attempts: ValidationAttempt[]; staff?: boolean }> = ({
  attempts, staff = false,
}) => {
  if (!attempts || attempts.length === 0) {
    return <EmptyState title="No validation attempts yet" body="Run a connection test to validate this account." />;
  }
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", minWidth: staff ? 640 : 460 }}>
        <caption style={{ textAlign: "left", color: "#8fa0b7", fontSize: "0.8rem", padding: "0 0 0.5rem" }}>
          Validation history
        </caption>
        <thead>
          <tr>
            <th scope="col" style={{ ...th, width: 34 }} aria-label="Status" />
            <th scope="col" style={th}>Time</th>
            <th scope="col" style={th}>Outcome</th>
            <th scope="col" style={th}>Summary</th>
            {staff && <th scope="col" style={th}>Correlation ID</th>}
          </tr>
        </thead>
        <tbody>
          {attempts.map((a) => {
            const ic = icon(a.status);
            const outcome = a.status === "HEALTHY" ? "Verified" : reasonShort(a.reason_code);
            return (
              <tr key={a.id}>
                <td style={{ ...td, textAlign: "center" }}>
                  <span aria-label={ic.label} title={ic.label} style={{ color: ic.color, fontSize: "1rem" }}>{ic.ch}</span>
                </td>
                <td style={{ ...td, whiteSpace: "nowrap" }}>{formatWhen(a.created_at) || "—"}</td>
                <td style={td}>{outcome}</td>
                <td style={{ ...td, color: "#9fb0c8" }}>{reasonMessage(a.reason_code) || (a.status === "HEALTHY" ? "Broker connection verified." : "—")}</td>
                {staff && (
                  <td style={{ ...td, color: "#8fa0b7", fontFamily: "var(--font-geist-mono), monospace", fontSize: "0.78rem" }}>
                    {a.correlation_id || "—"}
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};

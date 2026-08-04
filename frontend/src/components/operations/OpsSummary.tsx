"use client";

import React from "react";
import { StatusBadge } from "@/components/broker/StatusBadge";
import { SeverityBadge } from "@/components/operations/OpsBadges";
import { validationStatusView, formatWhen } from "@/lib/broker-status";
import {
  credentialView, disconnectView, healthStateView, pauseView,
} from "@/lib/operations-status";
import type { OperationalSummary } from "@/types/operations";

/** WP5.3 — the read-only Account Overview dashboard. Current STATE (validation / health / pause /
 * eligibility / credential / disconnect) + latest validation/warning/error + event counts + last update.
 * No diagnostics; every value is a mapped view or a customer-safe summary string. */
const card: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.08)", borderRadius: 14, padding: "1rem 1.2rem",
  background: "rgba(10,15,35,0.5)",
};
const label: React.CSSProperties = { color: "#8fa0b7", fontSize: "0.75rem", marginBottom: 4 };
const row: React.CSSProperties = { display: "flex", flexWrap: "wrap", gap: 16 };
const cell: React.CSSProperties = { minWidth: 140 };

const Cell: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <div style={cell}><div style={label}>{title}</div>{children}</div>
);

export const OpsSummary: React.FC<{ summary: OperationalSummary }> = ({ summary }) => {
  const h = summary.health_state;
  const p = summary.runtime_pause;
  const counts = summary.event_counts;
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={card}>
        <div style={row}>
          <Cell title="Validation">
            <StatusBadge view={validationStatusView(summary.validation_state.status)} />
          </Cell>
          <Cell title="Broker health">
            <StatusBadge view={healthStateView(h.state, h.available)} />
          </Cell>
          <Cell title="Runtime">
            <StatusBadge view={pauseView(!!p.paused)} />
          </Cell>
          <Cell title="Eligible to execute">
            <StatusBadge view={h.available && h.eligible ? { label: "Eligible", color: "green" }
              : { label: "Not eligible", color: "gray" }} />
          </Cell>
          <Cell title="Credentials">
            <StatusBadge view={credentialView(!!summary.credential_status.present)} />
          </Cell>
          <Cell title="Connection">
            <StatusBadge view={disconnectView(!!summary.disconnect_state.disconnected)} />
          </Cell>
        </div>
      </div>

      <div style={card}>
        <div style={row}>
          <Cell title="Total events"><span style={{ color: "#e9f4ff" }}>{counts.total}</span></Cell>
          <Cell title="Open (needs attention)"><span style={{ color: "#e9f4ff" }}>{counts.open}</span></Cell>
          <Cell title="Latest validation">
            {summary.latest_validation
              ? <span style={{ color: "#cbd5f5", fontSize: "0.85rem" }}>{summary.latest_validation.summary || "—"}</span>
              : <span style={{ color: "#6b7a94" }}>None</span>}
          </Cell>
          <Cell title="Latest warning">
            {summary.latest_warning
              ? <SeverityBadge severity={summary.latest_warning.severity} />
              : <span style={{ color: "#6b7a94" }}>None</span>}
          </Cell>
          <Cell title="Latest error">
            {summary.latest_error
              ? <SeverityBadge severity={summary.latest_error.severity} />
              : <span style={{ color: "#6b7a94" }}>None</span>}
          </Cell>
          <Cell title="Last update">
            <span style={{ color: "#9fb0c8", fontSize: "0.85rem" }}>
              {formatWhen(summary.last_update) || "—"}
            </span>
          </Cell>
        </div>
      </div>
    </div>
  );
};

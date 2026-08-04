"use client";

import React from "react";

/** WP4.2 — reusable loading / empty / error states for the Broker Accounts journey. */

const box: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.08)", borderRadius: 14, padding: "2rem 1.5rem",
  textAlign: "center", color: "#8fa0b7", background: "rgba(10,15,35,0.5)",
};

export const LoadingState: React.FC<{ label?: string }> = ({ label = "Loading…" }) => (
  <div style={box} role="status" aria-live="polite" aria-busy="true">
    <div style={{ fontSize: "0.95rem" }}>{label}</div>
  </div>
);

export const EmptyState: React.FC<{ title?: string; body?: string; action?: React.ReactNode }> = ({
  title = "No broker accounts yet", body = "Add a broker account to connect and validate it.", action,
}) => (
  <div style={box}>
    <div style={{ color: "#e9f4ff", fontSize: "1.05rem", marginBottom: 6 }}>{title}</div>
    <div style={{ fontSize: "0.9rem", marginBottom: action ? 16 : 0 }}>{body}</div>
    {action}
  </div>
);

export const ErrorState: React.FC<{ message?: string; onRetry?: () => void }> = ({
  message = "Something went wrong. Please try again.", onRetry,
}) => (
  <div style={{ ...box, border: "1px solid rgba(239,68,68,0.35)", color: "#fca5a5" }} role="alert">
    <div style={{ marginBottom: onRetry ? 14 : 0 }}>{message}</div>
    {onRetry && (
      <button type="button" onClick={onRetry}
              style={{ background: "rgba(255,255,255,0.06)", color: "#e5f4ff", border: "1px solid rgba(255,255,255,0.15)",
                       borderRadius: 10, padding: "0.5rem 1rem", cursor: "pointer" }}>Try again</button>
    )}
  </div>
);

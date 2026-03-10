"use client";

import React, { useState } from "react";

// ─────────────────────────────────────────────────────────────────────
// Reusable "action request" modal for plan changes, hosting changes, etc.
// Frontend-only — no backend calls.
// ─────────────────────────────────────────────────────────────────────

type ActionRequestModalProps = {
  open: boolean;
  onClose: () => void;
  /** Modal title, e.g. "Plan change request" */
  title: string;
  /** Confirmation body shown after "submit" */
  confirmationBody: string;
  /** Optional context line shown above the submit button */
  contextLine?: string;
};

const inputStyle: React.CSSProperties = {
  width: "100%",
  padding: "0.6rem 0.8rem",
  borderRadius: 10,
  border: "1px solid rgba(255, 255, 255, 0.1)",
  background: "rgba(8, 12, 32, 0.9)",
  color: "#e5f4ff",
  fontSize: "0.9rem",
  outline: "none",
};

export function ActionRequestModal({
  open,
  onClose,
  title,
  confirmationBody,
  contextLine,
}: ActionRequestModalProps) {
  const [submitted, setSubmitted] = useState(false);
  const [message, setMessage] = useState("");

  if (!open) return null;

  const handleClose = () => {
    setSubmitted(false);
    setMessage("");
    onClose();
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        background: "rgba(0, 0, 0, 0.7)",
        backdropFilter: "blur(4px)",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) handleClose();
      }}
    >
      <div
        style={{
          width: "100%",
          maxWidth: 440,
          margin: "0 1rem",
          background: "rgba(5, 8, 22, 0.97)",
          border: "1px solid rgba(74, 179, 255, 0.15)",
          borderRadius: 18,
          padding: "2rem",
          boxShadow:
            "0 24px 80px rgba(0, 0, 0, 0.6), 0 0 60px rgba(30, 111, 255, 0.08)",
          color: "#e5f4ff",
          fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
          position: "relative",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {submitted ? (
          /* ── Confirmation state ── */
          <div style={{ textAlign: "center" }}>
            <div
              style={{
                width: 48,
                height: 48,
                borderRadius: 999,
                background: "rgba(34, 197, 94, 0.12)",
                border: "1px solid rgba(34, 197, 94, 0.3)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                margin: "0 auto 1.25rem",
              }}
            >
              <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#4ade80" strokeWidth="2.5">
                <path d="M20 6L9 17l-5-5" />
              </svg>
            </div>

            <h2 style={{ fontSize: "1.3rem", fontWeight: 700, margin: "0 0 0.75rem", color: "#e9f4ff" }}>
              {title}
            </h2>
            <p style={{ fontSize: "0.9rem", color: "#8fa0b7", lineHeight: 1.6, margin: "0 0 2rem" }}>
              {confirmationBody}
            </p>

            <button
              onClick={handleClose}
              style={{
                width: "100%",
                padding: "0.75rem 1rem",
                borderRadius: 999,
                border: "none",
                background: "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)",
                color: "#fff",
                fontSize: "0.95rem",
                fontWeight: 600,
                cursor: "pointer",
                boxShadow: "0 8px 24px rgba(30, 111, 255, 0.3)",
              }}
            >
              Close
            </button>
          </div>
        ) : (
          /* ── Form state ── */
          <>
            <button
              onClick={handleClose}
              style={{
                position: "absolute",
                top: "1rem",
                right: "1rem",
                background: "none",
                border: "none",
                color: "#64748b",
                fontSize: "1.5rem",
                cursor: "pointer",
                lineHeight: 1,
                padding: "0.25rem",
              }}
              aria-label="Close"
            >
              ×
            </button>

            <h2 style={{ fontSize: "1.3rem", fontWeight: 700, margin: "0 0 0.35rem", color: "#e9f4ff" }}>
              {title}
            </h2>

            {contextLine && (
              <p style={{ fontSize: "0.85rem", color: "#67e8f9", lineHeight: 1.5, margin: "0.5rem 0 1rem" }}>
                {contextLine}
              </p>
            )}

            <label
              htmlFor="arm-message"
              style={{ display: "block", fontSize: "0.8rem", color: "#94a3b8", marginBottom: "0.35rem", fontWeight: 600 }}
            >
              Message <span style={{ fontWeight: 400, color: "#64748b" }}>(optional)</span>
            </label>
            <textarea
              id="arm-message"
              placeholder="Tell us about your requirements..."
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={3}
              style={{
                ...inputStyle,
                resize: "vertical" as const,
                fontFamily: "inherit",
                marginBottom: "1.5rem",
              }}
            />

            <div style={{ display: "flex", gap: "0.75rem" }}>
              <button
                onClick={handleClose}
                style={{
                  flex: 1,
                  padding: "0.7rem 1rem",
                  borderRadius: 999,
                  border: "1px solid rgba(255, 255, 255, 0.15)",
                  background: "transparent",
                  color: "#c2d5ff",
                  fontSize: "0.9rem",
                  fontWeight: 500,
                  cursor: "pointer",
                }}
              >
                Cancel
              </button>
              <button
                onClick={() => setSubmitted(true)}
                style={{
                  flex: 1,
                  padding: "0.7rem 1rem",
                  borderRadius: 999,
                  border: "none",
                  background: "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)",
                  color: "#fff",
                  fontSize: "0.9rem",
                  fontWeight: 600,
                  cursor: "pointer",
                  boxShadow: "0 8px 24px rgba(30, 111, 255, 0.3)",
                }}
              >
                Submit request
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

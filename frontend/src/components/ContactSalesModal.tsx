"use client";

import { useState } from "react";

// ─────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────
type ContactMethod = "email" | "call" | "chat";

type ContactSalesModalProps = {
  open: boolean;
  onClose: () => void;
  /** Pre-filled email from auth context (skips email field if provided) */
  userEmail?: string;
  /** Optional source label for analytics/logging later */
  source?: string;
};

const METHODS: { key: ContactMethod; label: string; icon: string }[] = [
  { key: "email", label: "Email", icon: "M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2zM22 6l-10 7L2 6" },
  { key: "call", label: "Schedule a Call", icon: "M22 16.92v3a2 2 0 01-2.18 2 19.79 19.79 0 01-8.63-3.07 19.5 19.5 0 01-6-6 19.79 19.79 0 01-3.07-8.67A2 2 0 014.11 2h3a2 2 0 012 1.72c.127.96.361 1.903.7 2.81a2 2 0 01-.45 2.11L8.09 9.91a16 16 0 006 6l1.27-1.27a2 2 0 012.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0122 16.92z" },
  { key: "chat", label: "Chat with Sales", icon: "M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" },
];

// ─────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────
export function ContactSalesModal({ open, onClose, userEmail, source }: ContactSalesModalProps) {
  const [method, setMethod] = useState<ContactMethod | null>(null);
  const [message, setMessage] = useState("");
  const [email, setEmail] = useState("");
  const [submitted, setSubmitted] = useState(false);

  // Suppress unused lint — source is for future analytics
  void source;

  if (!open) return null;

  const resolvedEmail = userEmail || email;
  const canSubmit = method !== null && (userEmail || email.trim().length > 0);

  const handleSubmit = () => {
    if (!canSubmit) return;
    // Local state transition only — no backend call
    setSubmitted(true);
  };

  const handleClose = () => {
    // Reset state on close so modal is fresh next time
    setMethod(null);
    setMessage("");
    setEmail("");
    setSubmitted(false);
    onClose();
  };

  return (
    // Backdrop
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
      {/* Modal card */}
      <div
        style={{
          width: "100%",
          maxWidth: 460,
          margin: "0 1rem",
          background: "rgba(5, 8, 22, 0.97)",
          border: "1px solid rgba(74, 179, 255, 0.15)",
          borderRadius: 18,
          padding: "2rem",
          boxShadow: "0 24px 80px rgba(0, 0, 0, 0.6), 0 0 60px rgba(30, 111, 255, 0.08)",
          color: "#e5f4ff",
          fontFamily: "system-ui, -apple-system, BlinkMacSystemFont, sans-serif",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {submitted ? (
          /* ── Confirmation state ── */
          <div style={{ textAlign: "center" }}>
            {/* Check icon */}
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
              Request captured
            </h2>
            <p style={{ fontSize: "0.9rem", color: "#8fa0b7", lineHeight: 1.6, margin: "0 0 2rem" }}>
              Your interest has been recorded.{" "}
              A member of the GuvFX team will contact you once sales requests are enabled.
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
              Return to Plans
            </button>
          </div>
        ) : (
          /* ── Form state ── */
          <>
            {/* Close button */}
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
              Contact Sales
            </h2>
            <p style={{ fontSize: "0.85rem", color: "#8fa0b7", lineHeight: 1.5, margin: "0 0 1.5rem" }}>
              Tell us how you&apos;d like to connect and we&apos;ll follow up.
            </p>

            {/* Contact method selection */}
            <label style={{ display: "block", fontSize: "0.8rem", color: "#94a3b8", marginBottom: "0.5rem", fontWeight: 600 }}>
              Preferred contact method
            </label>
            <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem", marginBottom: "1.25rem" }}>
              {METHODS.map((m) => {
                const selected = method === m.key;
                return (
                  <button
                    key={m.key}
                    type="button"
                    onClick={() => setMethod(m.key)}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "0.75rem",
                      padding: "0.7rem 1rem",
                      borderRadius: 10,
                      border: selected
                        ? "1px solid rgba(59, 130, 246, 0.5)"
                        : "1px solid rgba(255, 255, 255, 0.1)",
                      background: selected
                        ? "rgba(59, 130, 246, 0.1)"
                        : "rgba(255, 255, 255, 0.03)",
                      color: selected ? "#93c5fd" : "#c2d5ff",
                      fontSize: "0.9rem",
                      fontWeight: 500,
                      cursor: "pointer",
                      textAlign: "left",
                      transition: "border-color 0.15s, background 0.15s",
                    }}
                  >
                    <svg
                      width="18"
                      height="18"
                      viewBox="0 0 24 24"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <path d={m.icon} />
                    </svg>
                    {m.label}
                  </button>
                );
              })}
            </div>

            {/* Email field — only if no authenticated email */}
            {!userEmail && (
              <>
                <label
                  htmlFor="cs-email"
                  style={{ display: "block", fontSize: "0.8rem", color: "#94a3b8", marginBottom: "0.35rem", fontWeight: 600 }}
                >
                  Email
                </label>
                <input
                  id="cs-email"
                  type="email"
                  placeholder="you@company.com"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 10,
                    border: "1px solid rgba(255, 255, 255, 0.1)",
                    background: "rgba(8, 12, 32, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    marginBottom: "1.25rem",
                    outline: "none",
                  }}
                />
              </>
            )}

            {/* Optional message */}
            <label
              htmlFor="cs-message"
              style={{ display: "block", fontSize: "0.8rem", color: "#94a3b8", marginBottom: "0.35rem", fontWeight: 600 }}
            >
              Message <span style={{ fontWeight: 400, color: "#64748b" }}>(optional)</span>
            </label>
            <textarea
              id="cs-message"
              placeholder="Tell us about your use case or requirements..."
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={3}
              style={{
                width: "100%",
                padding: "0.6rem 0.8rem",
                borderRadius: 10,
                border: "1px solid rgba(255, 255, 255, 0.1)",
                background: "rgba(8, 12, 32, 0.9)",
                color: "#e5f4ff",
                fontSize: "0.9rem",
                marginBottom: "1.5rem",
                outline: "none",
                resize: "vertical",
                fontFamily: "inherit",
              }}
            />

            {/* Actions */}
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
                onClick={handleSubmit}
                disabled={!canSubmit}
                style={{
                  flex: 1,
                  padding: "0.7rem 1rem",
                  borderRadius: 999,
                  border: "none",
                  background: canSubmit
                    ? "linear-gradient(135deg, #1e6fff 0%, #00d4ff 50%, #1e6fff 100%)"
                    : "rgba(59, 130, 246, 0.2)",
                  color: canSubmit ? "#fff" : "#64748b",
                  fontSize: "0.9rem",
                  fontWeight: 600,
                  cursor: canSubmit ? "pointer" : "not-allowed",
                  boxShadow: canSubmit ? "0 8px 24px rgba(30, 111, 255, 0.3)" : "none",
                  transition: "background 0.15s, color 0.15s",
                }}
              >
                Submit
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

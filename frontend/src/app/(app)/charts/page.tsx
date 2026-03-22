"use client";

import { Card } from "@/components/ui/Card";

export default function ChartsPage() {
  return (
    <div style={{ maxWidth: 1100, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Charts</h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
        Market charts and visualisation.
      </p>

      <Card title="MT5 Charts">
        <p style={{ color: "#b7c5dd", marginBottom: "1rem", lineHeight: 1.6 }}>
          MT5 charts are available through the terminal session interface.
          Launch or resume a terminal session to access live charts, indicators,
          and market data directly in your browser.
        </p>
        <a
          href="/trading/terminal-access"
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.5rem",
            padding: "0.5rem 1.25rem",
            borderRadius: 8,
            background: "linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%)",
            color: "#fff",
            fontWeight: 600,
            fontSize: "0.875rem",
            textDecoration: "none",
            border: "1px solid rgba(74, 179, 255, 0.2)",
            transition: "opacity 0.15s",
          }}
        >
          Open Terminal Access
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M5 12h14" />
            <path d="m12 5 7 7-7 7" />
          </svg>
        </a>
      </Card>
    </div>
  );
}

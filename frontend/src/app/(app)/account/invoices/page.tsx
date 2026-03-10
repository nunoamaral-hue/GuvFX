"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { Invoice, InvoicesResponse } from "@/types/billing";

// ─────────────────────────────────────────────────────────────────────
// Display helpers — humanization is display-only
// ─────────────────────────────────────────────────────────────────────

const humanize = (s: string) =>
  s.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });

const fmtDateTime = (iso: string) =>
  new Date(iso).toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });

// Status badge color + always includes readable text (amendment #1)
const statusColor: Record<string, string> = {
  paid: "#4ade80",
  issued: "#60a5fa",
  draft: "#94a3b8",
  past_due: "#fbbf24",
  void: "#f87171",
  cancelled: "#f87171",
};

// ─────────────────────────────────────────────────────────────────────
// Shared styles
// ─────────────────────────────────────────────────────────────────────

const glassCard: React.CSSProperties = {
  borderRadius: 16,
  border: "1px solid rgba(74, 179, 255, 0.12)",
  background:
    "linear-gradient(135deg, rgba(10, 15, 40, 0.95) 0%, rgba(5, 8, 22, 0.98) 100%)",
  boxShadow:
    "0 8px 32px rgba(0, 0, 0, 0.4), 0 0 60px rgba(30, 111, 255, 0.04)",
  padding: "1.5rem",
};

// ─────────────────────────────────────────────────────────────────────
// Invoice card
// ─────────────────────────────────────────────────────────────────────

function InvoiceCard({ inv }: { inv: Invoice }) {
  const color = statusColor[inv.status] ?? "#94a3b8";

  return (
    <div
      style={{
        ...glassCard,
        padding: "1rem 1.25rem",
        display: "flex",
        flexWrap: "wrap",
        alignItems: "center",
        gap: "1rem",
      }}
    >
      {/* Invoice number + plan */}
      <div style={{ flex: "1 1 200px", minWidth: 0 }}>
        <div
          style={{
            fontSize: "0.95rem",
            fontWeight: 600,
            color: "#e9f4ff",
          }}
        >
          {inv.invoice_number}
        </div>
        {inv.plan_at_issue && (
          <div
            style={{
              fontSize: "0.8rem",
              color: "#8fa0b7",
              marginTop: 2,
            }}
          >
            {humanize(inv.plan_at_issue)}
            {inv.billing_cycle_at_issue
              ? ` · ${humanize(inv.billing_cycle_at_issue)}`
              : ""}
          </div>
        )}
      </div>

      {/* Period */}
      <div style={{ flex: "0 0 auto" }}>
        <div
          style={{
            fontSize: "0.75rem",
            color: "#94a3b8",
            marginBottom: 2,
          }}
        >
          Period
        </div>
        <div style={{ fontSize: "0.85rem", color: "#c2d5ff" }}>
          {fmtDate(inv.period_start)} – {fmtDate(inv.period_end)}
        </div>
      </div>

      {/* Issue date */}
      <div style={{ flex: "0 0 auto" }}>
        <div
          style={{
            fontSize: "0.75rem",
            color: "#94a3b8",
            marginBottom: 2,
          }}
        >
          Issued
        </div>
        <div style={{ fontSize: "0.85rem", color: "#c2d5ff" }}>
          {fmtDate(inv.issue_date)}
        </div>
      </div>

      {/* Status badge — color + readable text always */}
      <div style={{ flex: "0 0 auto" }}>
        <span
          style={{
            display: "inline-block",
            fontSize: "0.75rem",
            fontWeight: 600,
            padding: "0.2rem 0.6rem",
            borderRadius: 999,
            background: `${color}20`,
            color,
            border: `1px solid ${color}40`,
          }}
        >
          {humanize(inv.status)}
        </span>
      </div>

      {/* Amount */}
      <div
        style={{
          flex: "0 0 auto",
          textAlign: "right",
          minWidth: 80,
        }}
      >
        <div
          style={{
            fontSize: "1rem",
            fontWeight: 700,
            color: "#e9f4ff",
          }}
        >
          {inv.currency} {inv.total_amount}
        </div>
        {inv.paid_at && (
          <div
            style={{
              fontSize: "0.75rem",
              color: "#4ade80",
              marginTop: 2,
            }}
          >
            Paid {fmtDateTime(inv.paid_at)}
          </div>
        )}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// Page
// ─────────────────────────────────────────────────────────────────────

export default function InvoicesPage() {
  const [invoices, setInvoices] = useState<Invoice[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchInvoices = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await apiFetch<InvoicesResponse>(
          "/api/billing/invoices/",
          {}
        );
        // Render in backend order — do not re-sort (amendment #4)
        setInvoices(data.invoices);
      } catch (err: unknown) {
        const message =
          err instanceof Error ? err.message : "Failed to load invoices.";
        setError(message);
      } finally {
        setLoading(false);
      }
    };
    fetchInvoices();
  }, []);

  return (
    <div style={{ maxWidth: 900, margin: "0 auto" }}>
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Invoices</h1>
      <p
        style={{
          fontSize: "0.9rem",
          color: "#b7c5dd",
          marginBottom: "1.5rem",
        }}
      >
        View your billing invoices.
      </p>

      {/* Loading */}
      {loading && (
        <div
          style={{
            ...glassCard,
            textAlign: "center",
            color: "#8fa0b7",
            fontSize: "0.9rem",
          }}
        >
          <p style={{ margin: 0 }}>Loading invoices…</p>
        </div>
      )}

      {/* Error */}
      {!loading && error && (
        <div
          style={{
            ...glassCard,
            textAlign: "center",
            color: "#f87171",
            fontSize: "0.9rem",
          }}
        >
          <p style={{ margin: 0 }}>{error}</p>
        </div>
      )}

      {/* Empty */}
      {!loading && !error && invoices.length === 0 && (
        <div
          style={{
            ...glassCard,
            textAlign: "center",
            color: "#8fa0b7",
            fontSize: "0.9rem",
          }}
        >
          <p style={{ margin: 0 }}>No invoices available yet.</p>
        </div>
      )}

      {/* Invoice list — rendered in backend order */}
      {!loading && !error && invoices.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
          {invoices.map((inv) => (
            <InvoiceCard key={inv.invoice_number} inv={inv} />
          ))}
        </div>
      )}
    </div>
  );
}

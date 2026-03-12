"use client";

import React, { useState } from "react";

// =============================================================================
// STYLE CONSTANTS — matching GuvFX dark premium theme
// =============================================================================

const COLORS = {
  bg: "rgba(7, 12, 30, 0.96)",
  bgDeep: "#050713",
  bgRow: "rgba(7, 12, 30, 0.9)",
  border: "rgba(148, 163, 184, 0.35)",
  borderSubtle: "#111827",
  text: "#e5f4ff",
  textMuted: "#9ca3af",
  textSecondary: "#8fa0b7",
  textLabel: "#cbd5f5",
  accent: "#3fe0ff",
  accentBlue: "#2979ff",
  warn: "#fcd34d",
  warnBg: "rgba(245, 158, 11, 0.12)",
  warnBorder: "rgba(245, 158, 11, 0.35)",
  danger: "#fca5a5",
  dangerBg: "rgba(239, 68, 68, 0.12)",
};

// =============================================================================
// AdminSectionHeader
// =============================================================================

type AdminSectionHeaderProps = {
  title: string;
  subtitle?: string;
};

export const AdminSectionHeader: React.FC<AdminSectionHeaderProps> = ({
  title,
  subtitle,
}) => (
  <header style={{ marginBottom: "1.25rem" }}>
    <h1 style={{ fontSize: "1.65rem", margin: 0, color: COLORS.text }}>
      {title}
    </h1>
    {subtitle && (
      <p
        style={{
          margin: "0.25rem 0 0",
          fontSize: "0.9rem",
          color: COLORS.textSecondary,
        }}
      >
        {subtitle}
      </p>
    )}
  </header>
);

// =============================================================================
// FilterBar
// =============================================================================

type FilterOption = { label: string; value: string };

type FilterBarProps = {
  filters: {
    key: string;
    label: string;
    options: FilterOption[];
    value: string;
    onChange: (val: string) => void;
  }[];
};

export const FilterBar: React.FC<FilterBarProps> = ({ filters }) => (
  <div
    style={{
      display: "flex",
      flexWrap: "wrap",
      gap: "0.75rem",
      marginBottom: "1rem",
      alignItems: "center",
    }}
  >
    {filters.map((f) => (
      <label
        key={f.key}
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.35rem",
          fontSize: "0.82rem",
          color: COLORS.textLabel,
        }}
      >
        {f.label}:
        <select
          value={f.value}
          onChange={(e) => f.onChange(e.target.value)}
          style={{
            background: COLORS.bgRow,
            color: COLORS.text,
            border: `1px solid ${COLORS.borderSubtle}`,
            borderRadius: 6,
            padding: "0.3rem 0.5rem",
            fontSize: "0.82rem",
            cursor: "pointer",
          }}
        >
          {f.options.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </label>
    ))}
  </div>
);

// =============================================================================
// DataTable
// =============================================================================

type Column<T> = {
  key: string;
  header: string;
  render: (row: T) => React.ReactNode;
  width?: string;
};

type DataTableProps<T> = {
  columns: Column<T>[];
  data: T[];
  rowKey: (row: T) => string | number;
  onRowClick?: (row: T) => void;
  emptyMessage?: string;
};

export function DataTable<T>({
  columns,
  data,
  rowKey,
  onRowClick,
  emptyMessage = "No records found.",
}: DataTableProps<T>) {
  return (
    <div style={{ overflowX: "auto" }}>
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: "0.84rem",
        }}
      >
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                style={{
                  textAlign: "left",
                  padding: "0.55rem 0.6rem",
                  color: COLORS.textMuted,
                  fontWeight: 500,
                  fontSize: "0.78rem",
                  textTransform: "uppercase",
                  letterSpacing: "0.04em",
                  borderBottom: `1px solid ${COLORS.borderSubtle}`,
                  width: col.width,
                }}
              >
                {col.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.length === 0 ? (
            <tr>
              <td
                colSpan={columns.length}
                style={{
                  padding: "1.5rem 0.6rem",
                  color: COLORS.textMuted,
                  textAlign: "center",
                }}
              >
                {emptyMessage}
              </td>
            </tr>
          ) : (
            data.map((row) => (
              <tr
                key={rowKey(row)}
                onClick={() => onRowClick?.(row)}
                style={{
                  cursor: onRowClick ? "pointer" : "default",
                  borderBottom: `1px solid ${COLORS.borderSubtle}`,
                  transition: "background 0.12s",
                }}
                onMouseEnter={(e) => {
                  if (onRowClick)
                    (e.currentTarget as HTMLTableRowElement).style.background =
                      "rgba(59, 130, 246, 0.06)";
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLTableRowElement).style.background =
                    "transparent";
                }}
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    style={{
                      padding: "0.55rem 0.6rem",
                      color: COLORS.text,
                    }}
                  >
                    {col.render(row)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}

// =============================================================================
// DetailDrawer
// =============================================================================

type DetailDrawerProps = {
  open: boolean;
  onClose: () => void;
  title: string;
  children: React.ReactNode;
};

export const DetailDrawer: React.FC<DetailDrawerProps> = ({
  open,
  onClose,
  title,
  children,
}) => {
  if (!open) return null;
  return (
    <>
      {/* backdrop */}
      <div
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0,0,0,0.55)",
          zIndex: 900,
        }}
      />
      {/* panel */}
      <aside
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: "min(520px, 90vw)",
          background: COLORS.bgDeep,
          borderLeft: `1px solid ${COLORS.border}`,
          boxShadow: "-8px 0 40px rgba(0,0,0,0.5)",
          zIndex: 901,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <header
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "1rem 1.25rem",
            borderBottom: `1px solid ${COLORS.borderSubtle}`,
          }}
        >
          <h2 style={{ margin: 0, fontSize: "1.1rem", color: COLORS.text }}>
            {title}
          </h2>
          <button
            onClick={onClose}
            style={{
              background: "transparent",
              border: "none",
              color: COLORS.textMuted,
              fontSize: "1.3rem",
              cursor: "pointer",
              padding: "0.25rem",
            }}
          >
            ✕
          </button>
        </header>
        <div
          style={{
            flex: 1,
            overflowY: "auto",
            padding: "1rem 1.25rem",
          }}
        >
          {children}
        </div>
      </aside>
    </>
  );
};

// =============================================================================
// ConfirmationModal
// =============================================================================

type ConfirmationModalProps = {
  open: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  loading?: boolean;
};

export const ConfirmationModal: React.FC<ConfirmationModalProps> = ({
  open,
  onClose,
  onConfirm,
  title,
  message,
  confirmLabel = "Confirm",
  danger = false,
  loading = false,
}) => {
  if (!open) return null;
  return (
    <>
      <div
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0,0,0,0.6)",
          zIndex: 1000,
        }}
      />
      <div
        style={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          background: COLORS.bgDeep,
          border: `1px solid ${COLORS.border}`,
          borderRadius: 12,
          padding: "1.5rem",
          minWidth: 360,
          maxWidth: "90vw",
          zIndex: 1001,
          boxShadow: "0 20px 60px rgba(0,0,0,0.7)",
        }}
      >
        <h3 style={{ margin: "0 0 0.75rem", fontSize: "1.05rem", color: COLORS.text }}>
          {title}
        </h3>
        <p style={{ margin: "0 0 1.25rem", fontSize: "0.88rem", color: COLORS.textLabel }}>
          {message}
        </p>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.6rem" }}>
          <button
            onClick={onClose}
            disabled={loading}
            style={{
              background: "transparent",
              color: COLORS.textLabel,
              border: `1px solid ${COLORS.borderSubtle}`,
              borderRadius: 999,
              padding: "0.4rem 1rem",
              fontSize: "0.85rem",
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            style={{
              background: danger
                ? "linear-gradient(135deg, #ef4444 0%, #f97316 100%)"
                : "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)",
              color: "#ffffff",
              border: "none",
              borderRadius: 999,
              padding: "0.4rem 1rem",
              fontSize: "0.85rem",
              cursor: loading ? "wait" : "pointer",
              opacity: loading ? 0.7 : 1,
            }}
          >
            {loading ? "Processing…" : confirmLabel}
          </button>
        </div>
      </div>
    </>
  );
};

// =============================================================================
// AuditNotice
// =============================================================================

export const AuditNotice: React.FC = () => (
  <div
    style={{
      display: "flex",
      alignItems: "center",
      gap: "0.4rem",
      padding: "0.4rem 0.75rem",
      borderRadius: 6,
      background: "rgba(59, 130, 246, 0.08)",
      border: "1px solid rgba(59, 130, 246, 0.2)",
      fontSize: "0.78rem",
      color: "#93c5fd",
      marginBottom: "0.75rem",
    }}
  >
    <span style={{ fontSize: "0.9rem" }}>⊘</span>
    This action is audited. Actor identity and timestamp are recorded.
  </div>
);

// =============================================================================
// ReadOnlyNotice
// =============================================================================

export const ReadOnlyNotice: React.FC<{ message?: string }> = ({
  message = "This record is read-only.",
}) => (
  <div
    style={{
      display: "inline-flex",
      alignItems: "center",
      gap: "0.35rem",
      padding: "0.3rem 0.65rem",
      borderRadius: 6,
      background: "rgba(148, 163, 184, 0.08)",
      border: "1px solid rgba(148, 163, 184, 0.2)",
      fontSize: "0.78rem",
      color: COLORS.textMuted,
      marginBottom: "0.75rem",
    }}
  >
    <span style={{ fontSize: "0.85rem" }}>🔒</span>
    {message}
  </div>
);

// =============================================================================
// WarningBanner
// =============================================================================

type WarningBannerProps = {
  children: React.ReactNode;
};

export const WarningBanner: React.FC<WarningBannerProps> = ({ children }) => (
  <div
    style={{
      padding: "0.6rem 1rem",
      borderRadius: 8,
      background: COLORS.warnBg,
      border: `1px solid ${COLORS.warnBorder}`,
      color: COLORS.warn,
      fontSize: "0.85rem",
      fontWeight: 500,
      marginBottom: "1rem",
    }}
  >
    {children}
  </div>
);

// =============================================================================
// OneTimeSecretPanel
// =============================================================================

type OneTimeSecretPanelProps = {
  secret: string;
  onDismiss: () => void;
};

export const OneTimeSecretPanel: React.FC<OneTimeSecretPanelProps> = ({
  secret,
  onDismiss,
}) => {
  const [copied, setCopied] = useState(false);

  return (
    <div
      style={{
        padding: "1.25rem",
        borderRadius: 10,
        background: COLORS.warnBg,
        border: `1px solid ${COLORS.warnBorder}`,
        marginBottom: "1rem",
      }}
    >
      <div
        style={{
          fontSize: "0.85rem",
          fontWeight: 600,
          color: COLORS.warn,
          marginBottom: "0.6rem",
        }}
      >
        Worker secret will only be shown once. Store it securely.
      </div>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "0.6rem",
          marginBottom: "0.75rem",
        }}
      >
        <code
          style={{
            flex: 1,
            background: "rgba(0,0,0,0.4)",
            padding: "0.5rem 0.75rem",
            borderRadius: 6,
            fontSize: "0.82rem",
            color: COLORS.text,
            fontFamily: "var(--font-geist-mono), monospace",
            wordBreak: "break-all",
          }}
        >
          {secret}
        </code>
        <button
          onClick={() => {
            navigator.clipboard.writeText(secret);
            setCopied(true);
            setTimeout(() => setCopied(false), 2000);
          }}
          style={{
            background: "rgba(255,255,255,0.08)",
            border: `1px solid ${COLORS.borderSubtle}`,
            borderRadius: 6,
            padding: "0.4rem 0.75rem",
            fontSize: "0.8rem",
            color: COLORS.textLabel,
            cursor: "pointer",
            whiteSpace: "nowrap",
          }}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <button
        onClick={onDismiss}
        style={{
          background: "transparent",
          border: `1px solid ${COLORS.warnBorder}`,
          borderRadius: 999,
          padding: "0.35rem 0.9rem",
          fontSize: "0.82rem",
          color: COLORS.warn,
          cursor: "pointer",
        }}
      >
        I have stored the secret — dismiss
      </button>
    </div>
  );
};

// =============================================================================
// EmptyState / LoadingState / ErrorState
// =============================================================================

export const LoadingState: React.FC<{ message?: string }> = ({
  message = "Loading…",
}) => (
  <p style={{ fontSize: "0.9rem", color: COLORS.textLabel, padding: "1rem 0" }}>
    {message}
  </p>
);

export const EmptyState: React.FC<{ message?: string }> = ({
  message = "No records found.",
}) => (
  <p style={{ fontSize: "0.9rem", color: COLORS.textMuted, padding: "1rem 0" }}>
    {message}
  </p>
);

// =============================================================================
// DetailField — key/value pair for detail views
// =============================================================================

type DetailFieldProps = {
  label: string;
  children: React.ReactNode;
  mono?: boolean;
};

export const DetailField: React.FC<DetailFieldProps> = ({
  label,
  children,
  mono,
}) => (
  <div style={{ marginBottom: "0.65rem" }}>
    <div
      style={{
        fontSize: "0.75rem",
        color: COLORS.textMuted,
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        marginBottom: "0.15rem",
      }}
    >
      {label}
    </div>
    <div
      style={{
        fontSize: "0.88rem",
        color: COLORS.text,
        fontFamily: mono ? "var(--font-geist-mono), monospace" : "inherit",
        wordBreak: mono ? "break-all" : "normal",
      }}
    >
      {children}
    </div>
  </div>
);

// =============================================================================
// SafeJsonViewer — renders JSON safely (no unescaped HTML)
// =============================================================================

const SENSITIVE_KEYS = new Set([
  "password",
  "secret",
  "token",
  "api_key",
  "apikey",
  "authorization",
  "credential",
  "private_key",
  "access_token",
  "refresh_token",
]);

function maskSensitive(obj: unknown, depth = 0): unknown {
  if (depth > 10) return "[nested]";
  if (obj === null || obj === undefined) return obj;
  if (typeof obj === "string") return obj;
  if (typeof obj === "number" || typeof obj === "boolean") return obj;
  if (Array.isArray(obj)) return obj.map((v) => maskSensitive(v, depth + 1));
  if (typeof obj === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      out[k] = SENSITIVE_KEYS.has(k.toLowerCase()) ? "••••••••" : maskSensitive(v, depth + 1);
    }
    return out;
  }
  return String(obj);
}

export const SafeJsonViewer: React.FC<{ data: unknown }> = ({ data }) => {
  const masked = maskSensitive(data);
  return (
    <pre
      style={{
        background: "rgba(0,0,0,0.35)",
        border: `1px solid ${COLORS.borderSubtle}`,
        borderRadius: 8,
        padding: "0.75rem 1rem",
        fontSize: "0.78rem",
        color: COLORS.textLabel,
        fontFamily: "var(--font-geist-mono), monospace",
        overflow: "auto",
        maxHeight: 320,
        whiteSpace: "pre-wrap",
        wordBreak: "break-word",
      }}
    >
      {JSON.stringify(masked, null, 2)}
    </pre>
  );
};

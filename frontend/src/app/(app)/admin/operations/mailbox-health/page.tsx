"use client";

/**
 * Operator console — Withdrawal Intelligence mailbox OAuth ingestion health (pkt-149 / ADR-0113).
 *
 * A read-only, value-blind operator projection: fleet-wide mailbox health coverage (healthy / degraded /
 * reconsent-required counts), the synchronizer-stall signal, and a per-mailbox table showing the MASKED identity, the
 * seven-status OAuth classification, the alert severity, the age since last success, and the exact operator action.
 * It surfaces a dead grant (RECONSENT_REQUIRED — the 7-day Testing-mode expiry) or a dead worker (SYNCHRONIZER_STALLED)
 * BEFORE prolonged blindness. Staff-only (backend `IsAdminUser` is the real gate) and DARK (the backend endpoint 404s
 * until `BROKER_MAILBOX_OPERATOR_HEALTH_ENABLED` is armed); it NEVER shows a token, credential, raw cursor, email
 * body, or a full address — only the backend's already-masked payload.
 */
import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useAdminRole } from "@/components/admin/useAdminRole";
import {
  AdminSectionHeader,
  DataTable,
  WarningBanner,
  LoadingState,
  ReadOnlyNotice,
} from "@/components/admin/AdminShared";
import { Alert } from "@/components/ui/Alert";

const ENDPOINT = "/api/broker-intelligence/operator/mailbox-health/";

type MailboxHealth = {
  mailbox_id: number;
  provider: string;
  masked_email: string;
  connection_status: string;
  oauth_status: string;
  failure_class: string;
  alert_severity: string;
  recovery_required: boolean;
  reconsent_required: boolean;
  recommended_action: string;
  last_error_code: string;
  last_error_http_status: number | null;
  consecutive_failures: number;
  age_since_last_success_seconds: number | null;
  last_successful_sync: string | null;
};

type Aggregate = {
  generated_at: string;
  monitored_mailbox_count: number;
  healthy_count: number;
  owner_disconnected_count: number;
  degraded_count: number;
  reconsent_required_count: number;
  synchronizer_stalled: boolean;
  any_reconsent_required: boolean;
  worst_alert_severity: string;
  counts_by_status: Record<string, number>;
  mailboxes: MailboxHealth[];
};

const STATUS_COLOR: Record<string, string> = {
  HEALTHY: "#5ad19a",
  OWNER_DISCONNECTED: "#9fb0c9", // benign: the member disconnected it — muted, deliberately not an alert colour
  TRANSIENT_FAILURE: "#e7c77a",
  DEGRADED: "#e7c77a",
  UNKNOWN_FAIL_CLOSED: "#e7c77a",
  RECONSENT_REQUIRED: "#e8a0a0",
  SECURITY_CONFIGURATION_ERROR: "#e8a0a0",
  SYNCHRONIZER_STALLED: "#e8a0a0",
};

function fmtAge(s: number | null): string {
  if (s === null || s === undefined) return "—";
  if (s < 90) return `${s}s`;
  if (s < 90 * 60) return `${Math.round(s / 60)}m`;
  if (s < 48 * 3600) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
}

const wrap = { maxWidth: 1100, margin: "0 auto", padding: "0 1rem" } as const;

export default function MailboxHealthConsole() {
  const admin = useAdminRole();
  const [agg, setAgg] = useState<Aggregate | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "dark" | "error">("loading");

  useEffect(() => {
    if (admin.loading || !admin.authorized) return;
    let alive = true;
    apiFetch<Aggregate>(ENDPOINT, {})
      .then((r) => {
        if (!alive) return;
        setAgg(r);
        setState("ready");
      })
      .catch((e: unknown) => {
        if (!alive) return;
        const status = (e as { status?: number })?.status;
        setState(status === 404 ? "dark" : "error");
      });
    return () => {
      alive = false;
    };
  }, [admin.loading, admin.authorized]);

  if (admin.loading) return <div style={wrap}><LoadingState message="Verifying access…" /></div>;
  if (!admin.authorized)
    return <div style={wrap}><Alert type="error">You do not have permission to access mailbox health.</Alert></div>;

  const columns = [
    { key: "mailbox", header: "Mailbox", render: (m: MailboxHealth) => (
      <span style={{ color: "#dbe6f5" }}>{m.masked_email} <span style={{ color: "#6b7a90" }}>#{m.mailbox_id}</span></span>
    ) },
    { key: "status", header: "OAuth status", render: (m: MailboxHealth) => (
      <span style={{ color: STATUS_COLOR[m.oauth_status] ?? "#dbe6f5", fontWeight: 500 }}>{m.oauth_status}</span>
    ) },
    { key: "severity", header: "Severity", render: (m: MailboxHealth) => m.alert_severity },
    { key: "failure", header: "Failure class", render: (m: MailboxHealth) => (
      <span style={{ color: "#8fa0b7" }}>{m.failure_class}</span>
    ) },
    { key: "age", header: "Last success", render: (m: MailboxHealth) => fmtAge(m.age_since_last_success_seconds) },
    { key: "code", header: "Error code", render: (m: MailboxHealth) => (
      <span style={{ color: "#8fa0b7" }}>{m.last_error_code || "—"}</span>
    ) },
    { key: "action", header: "Operator action", render: (m: MailboxHealth) => (
      <span style={{ color: "#b8c7db" }}>{m.recommended_action || "—"}</span>
    ) },
  ];

  const summaryCell = (label: string, value: React.ReactNode, color = "#e5f4ff") => (
    <div style={{ border: "1px solid rgba(148,163,184,0.25)", borderRadius: 10, padding: "0.7rem 0.9rem", minWidth: 120 }}>
      <div style={{ fontSize: "0.72rem", color: "#8fa0b7", textTransform: "uppercase", letterSpacing: "0.05em" }}>{label}</div>
      <div style={{ fontSize: "1.25rem", color, marginTop: "0.2rem" }}>{value}</div>
    </div>
  );

  return (
    <div style={wrap}>
      <AdminSectionHeader
        title="Mailbox Health"
        subtitle="Withdrawal Intelligence email ingestion — value-blind OAuth health coverage (read-only)."
      />
      <ReadOnlyNotice message="Read-only. This view reads the ingestion health model and never shows a token, credential, raw cursor, email body, or full address." />

      {state === "loading" && <LoadingState message="Loading mailbox health…" />}
      {state === "dark" && (
        <Alert type="info">
          The operator mailbox-health projection is not enabled (BROKER_MAILBOX_OPERATOR_HEALTH_ENABLED is off). It stays
          dark until armed.
        </Alert>
      )}
      {state === "error" && <Alert type="error">Could not load mailbox health. Try again.</Alert>}

      {state === "ready" && agg && (
        <>
          {(agg.any_reconsent_required || agg.synchronizer_stalled) && (
            <div style={{ margin: "0.5rem 0 0.9rem" }}>
              <WarningBanner>
                {agg.synchronizer_stalled
                  ? "The ingestion worker appears stalled (no sync attempts across connected mailboxes). Restart the ingestion timer/service."
                  : `${agg.reconsent_required_count} connected mailbox(es) need owner re-consent — ingestion is blind for them until reconnected.`}
              </WarningBanner>
            </div>
          )}

          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.6rem", margin: "0.6rem 0 1rem" }}>
            {summaryCell("Monitored", agg.monitored_mailbox_count)}
            {summaryCell("Healthy", agg.healthy_count, "#5ad19a")}
            {summaryCell("Owner-disconnected", agg.owner_disconnected_count, agg.owner_disconnected_count ? "#9fb0c9" : "#e5f4ff")}
            {summaryCell("Degraded", agg.degraded_count, agg.degraded_count ? "#e7c77a" : "#e5f4ff")}
            {summaryCell("Reconnect needed", agg.reconsent_required_count, agg.reconsent_required_count ? "#e8a0a0" : "#e5f4ff")}
            {summaryCell("Worst severity", agg.worst_alert_severity, agg.worst_alert_severity === "OK" ? "#5ad19a" : "#e7c77a")}
            {summaryCell("Worker", agg.synchronizer_stalled ? "STALLED" : "running", agg.synchronizer_stalled ? "#e8a0a0" : "#5ad19a")}
          </div>

          <DataTable<MailboxHealth>
            columns={columns}
            data={agg.mailboxes}
            rowKey={(m) => m.mailbox_id}
            emptyMessage="No connected mailboxes to report."
          />
          <div style={{ fontSize: "0.72rem", color: "#6b7a90", marginTop: "0.6rem" }}>
            Generated {agg.generated_at}
          </div>
        </>
      )}
    </div>
  );
}

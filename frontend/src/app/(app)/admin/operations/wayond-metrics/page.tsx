"use client";

/**
 * Operator console — Wayond truthful-metrics foundation (pkt-151 / ADR-0116).
 *
 * A read-only, value-blind operator projection of Wayond signal-lifecycle PERFORMANCE (latency
 * stages with p50/p95/p99 where genuinely measurable) and QUALITY/reliability (outcome, approval,
 * plan, dead-letter counts). Its defining property is TRUTHFULNESS: a latency stage or quality
 * figure whose evidence is genuinely absent renders `N/A` / `UNMEASURABLE` with a reason — never a
 * fabricated 0, success, or invented duration. Broker-fill latency is shown as a separate, absent
 * stage and is NEVER combined with internal submission latency.
 *
 * Staff-only (backend `IsAdminUser` is the real gate) and DARK (the backend endpoint 404s until
 * `WAYOND_METRICS_OPERATOR_ENABLED` is armed). It shows NO message bodies, Telegram handles, chat
 * ids, account numbers, emails, credentials or tokens — only the backend's aggregate payload.
 */
import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { useAdminRole } from "@/components/admin/useAdminRole";
import {
  AdminSectionHeader,
  DataTable,
  LoadingState,
  ReadOnlyNotice,
} from "@/components/admin/AdminShared";
import { Alert } from "@/components/ui/Alert";

const ENDPOINT = "/api/reliability/wayond-metrics/";

type LatencyStage = {
  stage: string;
  status: string;
  clock_domain: string;
  valid_samples: number;
  excluded_missing?: number;
  excluded_skew?: number;
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  na_reason?: string;
  note?: string;
};

type Block = { status: string } & Record<string, unknown>;

type Payload = {
  schema: string;
  generated_at: string;
  window_days: number;
  cohort: string;
  value_blind: boolean;
  provenance_note: string;
  freshness: Record<string, unknown>;
  provider_coverage: { providers: { slug: string; status: string }[]; armed_count: number; note: string };
  latency_stages: LatencyStage[];
  quality: Record<string, Block>;
  boundaries: Record<string, unknown>;
};

const STATUS_COLOR: Record<string, string> = {
  MEASURED: "#5ad19a",
  OK: "#5ad19a",
  PARTIAL: "#e7c77a",
  EMPTY: "#8fa0b7",
  "N/A": "#8fa0b7",
  UNMEASURABLE: "#8fa0b7",
  UNKNOWN_FAIL_CLOSED: "#e8a0a0",
};

const wrap = { maxWidth: 1100, margin: "0 auto", padding: "0 1rem" } as const;

function ms(v: number | null): string {
  if (v === null || v === undefined) return "—";
  if (v < 1000) return `${Math.round(v)}ms`;
  return `${(v / 1000).toFixed(2)}s`;
}

function StatusPill({ status }: { status: string }) {
  return <span style={{ color: STATUS_COLOR[status] ?? "#dbe6f5", fontWeight: 600 }}>{status}</span>;
}

export default function WayondMetricsConsole() {
  const admin = useAdminRole();
  const [data, setData] = useState<Payload | null>(null);
  const [state, setState] = useState<"loading" | "ready" | "dark" | "error">("loading");

  useEffect(() => {
    if (admin.loading || !admin.authorized) return;
    let alive = true;
    apiFetch<Payload>(ENDPOINT, {})
      .then((r) => {
        if (!alive) return;
        setData(r);
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
    return <div style={wrap}><Alert type="error">You do not have permission to access Wayond metrics.</Alert></div>;

  const latencyColumns = [
    { key: "stage", header: "Stage", render: (s: LatencyStage) => (
      <span style={{ color: "#dbe6f5" }}>{s.stage}</span>
    ) },
    { key: "status", header: "Status", render: (s: LatencyStage) => <StatusPill status={s.status} /> },
    { key: "clock", header: "Clock domain", render: (s: LatencyStage) => (
      <span style={{ color: "#8fa0b7", fontSize: "0.8rem" }}>{s.clock_domain}</span>
    ) },
    { key: "n", header: "Samples", render: (s: LatencyStage) => s.valid_samples },
    { key: "p50", header: "p50", render: (s: LatencyStage) => ms(s.p50_ms) },
    { key: "p95", header: "p95", render: (s: LatencyStage) => ms(s.p95_ms) },
    { key: "p99", header: "p99", render: (s: LatencyStage) => ms(s.p99_ms) },
    { key: "reason", header: "Provenance / reason", render: (s: LatencyStage) => (
      <span style={{ color: "#8fa0b7", fontSize: "0.78rem" }}>{s.na_reason || s.note || "—"}</span>
    ) },
  ];

  const summaryCell = (label: string, value: React.ReactNode, color = "#e5f4ff") => (
    <div style={{ border: "1px solid rgba(148,163,184,0.25)", borderRadius: 10, padding: "0.7rem 0.9rem", minWidth: 120 }}>
      <div style={{ fontSize: "0.72rem", color: "#8fa0b7", textTransform: "uppercase", letterSpacing: "0.05em" }}>{label}</div>
      <div style={{ fontSize: "1.2rem", color, marginTop: "0.2rem" }}>{value}</div>
    </div>
  );

  const q = data?.quality ?? {};
  const naMetrics = ["duplicate_rate", "submission_success", "broker_fill", "slippage", "requotes", "listener_liveness", "flood_wait"];

  return (
    <div style={wrap}>
      <AdminSectionHeader
        title="Wayond Metrics"
        subtitle="Wayond signal performance & quality — truthful, value-blind, N/A where unmeasured (read-only)."
      />
      <ReadOnlyNotice message="Read-only, aggregate-only. Absent evidence shows N/A — never a fabricated speed or success. Broker-fill latency is a separate domain, never combined with internal submission latency. No message body, handle, account or credential is ever shown." />

      {state === "loading" && <LoadingState message="Loading Wayond metrics…" />}
      {state === "dark" && (
        <Alert type="info">
          The Wayond metrics operator projection is not enabled (WAYOND_METRICS_OPERATOR_ENABLED is off). It stays dark
          until armed.
        </Alert>
      )}
      {state === "error" && <Alert type="error">Could not load Wayond metrics. Try again.</Alert>}

      {state === "ready" && data && (
        <>
          <div style={{ display: "flex", flexWrap: "wrap", gap: "0.6rem", margin: "0.6rem 0 1rem" }}>
            {summaryCell("Window", `${data.window_days}d`)}
            {summaryCell("Cohort", data.cohort)}
            {summaryCell("Providers armed", data.provider_coverage?.armed_count ?? 0,
              (data.provider_coverage?.armed_count ?? 0) === 0 ? "#5ad19a" : "#e7c77a")}
            {summaryCell("Messages (genuine)", String((q.messages_observed as Block)?.genuine_observed ?? "—"))}
            {summaryCell("Value-blind", data.value_blind ? "yes" : "no", data.value_blind ? "#5ad19a" : "#e8a0a0")}
          </div>

          <h3 style={{ color: "#c9d6e8", fontSize: "0.95rem", margin: "0.8rem 0 0.4rem" }}>Latency stages</h3>
          <DataTable<LatencyStage>
            columns={latencyColumns}
            data={data.latency_stages}
            rowKey={(s) => s.stage}
            emptyMessage="No latency stages."
          />

          <h3 style={{ color: "#c9d6e8", fontSize: "0.95rem", margin: "1.2rem 0 0.4rem" }}>Unmeasured metrics (truthful N/A)</h3>
          <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem" }}>
            {naMetrics.map((k) => {
              const b = q[k] as Block | undefined;
              if (!b) return null;
              return (
                <div key={k} style={{ display: "flex", gap: "0.6rem", fontSize: "0.82rem" }}>
                  <span style={{ color: "#dbe6f5", minWidth: 180 }}>{k}</span>
                  <StatusPill status={b.status} />
                  <span style={{ color: "#8fa0b7" }}>{String((b as Record<string, unknown>).reason ?? "")}</span>
                </div>
              );
            })}
          </div>

          <div style={{ fontSize: "0.72rem", color: "#6b7a90", marginTop: "1rem", lineHeight: 1.5 }}>
            {data.provenance_note}
            <br />
            Generated {data.generated_at}
          </div>
        </>
      )}
    </div>
  );
}

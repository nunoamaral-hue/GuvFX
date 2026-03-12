"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Alert } from "@/components/ui/Alert";
import { useAdminRole } from "@/components/admin/useAdminRole";
import {
  AdminSectionHeader,
  FilterBar,
  DataTable,
  DetailDrawer,
  ConfirmationModal,
  AuditNotice,
  ReadOnlyNotice,
  DetailField,
  SafeJsonViewer,
  LoadingState,
} from "@/components/admin/AdminShared";
import type { ExecutionJob } from "@/types/admin";

const STATUS_BADGE: Record<string, "green" | "red" | "gray" | "blue" | "yellow"> = {
  completed: "green",
  failed: "red",
  cancelled: "gray",
  running: "blue",
  pending: "yellow",
};

export default function ExecutionJobsPage() {
  const admin = useAdminRole();
  const [jobs, setJobs] = useState<ExecutionJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [filterType, setFilterType] = useState("");
  const [filterStatus, setFilterStatus] = useState("");
  const [filterAccount, setFilterAccount] = useState("");

  // Detail
  const [selected, setSelected] = useState<ExecutionJob | null>(null);

  // Action confirm
  const [confirmAction, setConfirmAction] = useState<{
    type: "retry" | "cancel";
    job: ExecutionJob;
  } | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const fetchJobs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (filterType) params.set("job_type", filterType);
      if (filterStatus) params.set("status", filterStatus);
      if (filterAccount) params.set("account", filterAccount);
      const qs = params.toString();
      const url = `/api/execution/jobs/${qs ? `?${qs}` : ""}`;
      const data = await apiFetch<ExecutionJob[] | { results: ExecutionJob[] }>(url);
      const list = Array.isArray(data) ? data : (data.results ?? []);
      setJobs(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load execution jobs.");
    } finally {
      setLoading(false);
    }
  }, [filterType, filterStatus, filterAccount]);

  useEffect(() => { fetchJobs(); }, [fetchJobs]);

  const handleAction = async () => {
    if (!confirmAction) return;
    setActionLoading(true);
    setError(null);
    try {
      const endpoint = confirmAction.type === "retry"
        ? `/api/execution/jobs/${confirmAction.job.id}/retry/`
        : `/api/execution/jobs/${confirmAction.job.id}/cancel/`;
      await apiFetch(endpoint, { method: "POST", body: JSON.stringify({}) });
      setConfirmAction(null);
      setSelected(null);
      await fetchJobs();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${confirmAction.type} job.`);
    } finally {
      setActionLoading(false);
    }
  };

  if (admin.loading) return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><LoadingState message="Verifying access…" /></div>;
  if (!admin.authorized || admin.permissions?.execution_jobs === "none") {
    return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><Alert type="error">You do not have permission to access execution jobs.</Alert></div>;
  }

  const canWrite = admin.permissions?.execution_jobs === "full";

  // Derive filter options
  const typeOptions = [{ label: "All types", value: "" }].concat(
    [...new Set(jobs.map((j) => j.job_type))].filter(Boolean).map((t) => ({ label: t, value: t }))
  );
  const accountOptions = [{ label: "All accounts", value: "" }].concat(
    [...new Set(jobs.map((j) => j.account_display))].filter(Boolean).map((a) => ({ label: a, value: a }))
  );

  const columns = [
    { key: "id", header: "ID", render: (r: ExecutionJob) => <span style={{ fontFamily: "var(--font-geist-mono), monospace" }}>{r.id}</span>, width: "60px" },
    { key: "type", header: "Type", render: (r: ExecutionJob) => r.job_type },
    { key: "status", header: "Status", render: (r: ExecutionJob) => <Badge color={STATUS_BADGE[r.status] ?? "gray"}>{r.status}</Badge> },
    { key: "account", header: "Account", render: (r: ExecutionJob) => r.account_display || "—" },
    { key: "strategy", header: "Strategy", render: (r: ExecutionJob) => r.strategy_name || "—" },
    { key: "created", header: "Created", render: (r: ExecutionJob) => new Date(r.created_at).toLocaleString() },
  ];

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "0 1rem" }}>
      <AdminSectionHeader
        title="Execution Job Diagnostics"
        subtitle="Inspect execution pipeline state and manage job lifecycle."
      />

      {error && <Alert type="error">{error}</Alert>}

      <Card>
        <FilterBar
          filters={[
            {
              key: "type",
              label: "Job Type",
              options: typeOptions,
              value: filterType,
              onChange: setFilterType,
            },
            {
              key: "status",
              label: "Status",
              options: [
                { label: "All", value: "" },
                { label: "Pending", value: "pending" },
                { label: "Running", value: "running" },
                { label: "Completed", value: "completed" },
                { label: "Failed", value: "failed" },
                { label: "Cancelled", value: "cancelled" },
              ],
              value: filterStatus,
              onChange: setFilterStatus,
            },
            {
              key: "account",
              label: "Account",
              options: accountOptions,
              value: filterAccount,
              onChange: setFilterAccount,
            },
          ]}
        />

        {loading ? (
          <LoadingState />
        ) : (
          <DataTable
            columns={columns}
            data={jobs}
            rowKey={(r) => r.id}
            onRowClick={(r) => setSelected(r)}
            emptyMessage="No execution jobs found."
          />
        )}
      </Card>

      {/* Detail Drawer */}
      <DetailDrawer
        open={!!selected}
        onClose={() => setSelected(null)}
        title={`Execution Job #${selected?.id ?? ""}`}
      >
        {selected && (
          <>
            <DetailField label="Job Type">{selected.job_type}</DetailField>
            <DetailField label="Status">
              <Badge color={STATUS_BADGE[selected.status] ?? "gray"}>{selected.status}</Badge>
            </DetailField>
            <DetailField label="Account">{selected.account_display || "—"}</DetailField>
            <DetailField label="Strategy">{selected.strategy_name || "—"}</DetailField>

            {/* Timeline / lifecycle */}
            <div
              style={{
                marginTop: "0.5rem",
                marginBottom: "0.75rem",
                padding: "0.75rem",
                borderRadius: 8,
                background: "rgba(7,12,30,0.9)",
                border: "1px solid #111827",
              }}
            >
              <div style={{ fontSize: "0.78rem", color: "#9ca3af", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.5rem" }}>
                Lifecycle Timeline
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem", fontSize: "0.84rem" }}>
                <TimelineEntry label="Created" ts={selected.created_at} />
                <TimelineEntry label="Started" ts={selected.started_at} />
                <TimelineEntry label="Completed" ts={selected.completed_at} />
                <TimelineEntry label="Cancelled" ts={selected.cancelled_at} />
              </div>
            </div>

            {selected.error_message && (
              <DetailField label="Error">
                <span style={{ color: "#fca5a5" }}>{selected.error_message}</span>
              </DetailField>
            )}

            {/* Payload — immutable, read-only */}
            <div style={{ marginTop: "0.5rem" }}>
              <ReadOnlyNotice message="Execution payload is immutable and cannot be edited." />
              <div style={{ fontSize: "0.75rem", color: "#9ca3af", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.35rem" }}>
                Payload
              </div>
              <SafeJsonViewer data={selected.payload} />
            </div>

            {selected.result && (
              <div style={{ marginTop: "0.75rem" }}>
                <div style={{ fontSize: "0.75rem", color: "#9ca3af", textTransform: "uppercase", letterSpacing: "0.04em", marginBottom: "0.35rem" }}>
                  Result
                </div>
                <SafeJsonViewer data={selected.result} />
              </div>
            )}

            {/* Actions — only in detail view, permission-aware */}
            {canWrite && (
              <div style={{ marginTop: "1rem" }}>
                <AuditNotice />
                <div style={{ display: "flex", gap: "0.5rem" }}>
                  {selected.status === "failed" && (
                    <button
                      onClick={() => setConfirmAction({ type: "retry", job: selected })}
                      style={{
                        background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)",
                        color: "#fff",
                        border: "none",
                        borderRadius: 999,
                        padding: "0.45rem 1.1rem",
                        fontSize: "0.85rem",
                        cursor: "pointer",
                      }}
                    >
                      Retry Job
                    </button>
                  )}
                  {selected.status === "pending" && (
                    <button
                      onClick={() => setConfirmAction({ type: "cancel", job: selected })}
                      style={{
                        background: "linear-gradient(135deg, #ef4444 0%, #f97316 100%)",
                        color: "#fff",
                        border: "none",
                        borderRadius: 999,
                        padding: "0.45rem 1.1rem",
                        fontSize: "0.85rem",
                        cursor: "pointer",
                      }}
                    >
                      Cancel Job
                    </button>
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </DetailDrawer>

      {/* Confirmation */}
      <ConfirmationModal
        open={!!confirmAction}
        onClose={() => setConfirmAction(null)}
        onConfirm={handleAction}
        title={confirmAction?.type === "retry" ? "Retry Failed Job" : "Cancel Pending Job"}
        message={
          confirmAction?.type === "retry"
            ? `Retry execution job #${confirmAction.job.id} (${confirmAction.job.job_type})? The original payload will be reused. This action is audited.`
            : `Cancel pending execution job #${confirmAction?.job.id} (${confirmAction?.job.job_type})? This action is audited.`
        }
        confirmLabel={confirmAction?.type === "retry" ? "Retry" : "Cancel Job"}
        danger={confirmAction?.type === "cancel"}
        loading={actionLoading}
      />
    </div>
  );
}

// =============================================================================
// Timeline entry helper
// =============================================================================

function TimelineEntry({ label, ts }: { label: string; ts: string | null }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between" }}>
      <span style={{ color: "#8fa0b7" }}>{label}</span>
      <span style={{ color: ts ? "#e5f4ff" : "#6b7280", fontFamily: "var(--font-geist-mono), monospace", fontSize: "0.82rem" }}>
        {ts ? new Date(ts).toLocaleString() : "—"}
      </span>
    </div>
  );
}

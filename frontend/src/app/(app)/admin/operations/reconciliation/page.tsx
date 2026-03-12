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
  DetailField,
  LoadingState,
} from "@/components/admin/AdminShared";
import type { ReconciliationEvent } from "@/types/admin";

const SEVERITY_BADGE: Record<string, "red" | "yellow" | "blue"> = {
  critical: "red",
  warning: "yellow",
  info: "blue",
};

const STATUS_BADGE: Record<string, "red" | "green" | "gray"> = {
  open: "red",
  acknowledged: "gray",
  resolved: "green",
};

export default function ReconciliationPage() {
  const admin = useAdminRole();
  const [events, setEvents] = useState<ReconciliationEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [filterAccount, setFilterAccount] = useState("");
  const [filterSeverity, setFilterSeverity] = useState("");
  const [filterStatus, setFilterStatus] = useState("");

  // Detail drawer
  const [selected, setSelected] = useState<ReconciliationEvent | null>(null);

  // Confirmation modal
  const [confirmAction, setConfirmAction] = useState<{
    type: "acknowledge" | "resolve";
    event: ReconciliationEvent;
  } | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const fetchEvents = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (filterAccount) params.set("account", filterAccount);
      if (filterSeverity) params.set("severity", filterSeverity);
      if (filterStatus) params.set("resolution_status", filterStatus);
      const qs = params.toString();
      const url = `/api/reconciliation/events/${qs ? `?${qs}` : ""}`;
      const data = await apiFetch<ReconciliationEvent[] | { results: ReconciliationEvent[] }>(url);
      const list = Array.isArray(data) ? data : (data.results ?? []);
      setEvents(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load reconciliation events.");
    } finally {
      setLoading(false);
    }
  }, [filterAccount, filterSeverity, filterStatus]);

  useEffect(() => { fetchEvents(); }, [fetchEvents]);

  const handleAction = async () => {
    if (!confirmAction) return;
    setActionLoading(true);
    try {
      const endpoint = confirmAction.type === "acknowledge"
        ? `/api/reconciliation/events/${confirmAction.event.id}/acknowledge/`
        : `/api/reconciliation/events/${confirmAction.event.id}/resolve/`;
      await apiFetch(endpoint, { method: "POST", body: JSON.stringify({}) });
      setConfirmAction(null);
      setSelected(null);
      await fetchEvents();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${confirmAction.type} event.`);
    } finally {
      setActionLoading(false);
    }
  };

  if (admin.loading) return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><LoadingState message="Verifying access…" /></div>;
  if (!admin.authorized || admin.permissions?.reconciliation === "none") {
    return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><Alert type="error">You do not have permission to access reconciliation.</Alert></div>;
  }

  const canWrite = admin.permissions?.reconciliation === "full" || admin.permissions?.reconciliation === "acknowledge";

  // Derive unique accounts for filter
  const accountOptions = [{ label: "All accounts", value: "" }].concat(
    [...new Set(events.map((e) => e.account_display))].filter(Boolean).map((a) => ({ label: a, value: a }))
  );

  const columns = [
    { key: "ticket", header: "Ticket", render: (r: ReconciliationEvent) => <span style={{ fontFamily: "var(--font-geist-mono), monospace" }}>{r.ticket}</span>, width: "80px" },
    { key: "account", header: "Account", render: (r: ReconciliationEvent) => r.account_display },
    { key: "field", header: "Field", render: (r: ReconciliationEvent) => r.field_name },
    { key: "severity", header: "Severity", render: (r: ReconciliationEvent) => <Badge color={SEVERITY_BADGE[r.severity] ?? "gray"}>{r.severity}</Badge> },
    { key: "status", header: "Status", render: (r: ReconciliationEvent) => <Badge color={STATUS_BADGE[r.resolution_status] ?? "gray"}>{r.resolution_status}</Badge> },
    { key: "created", header: "Detected", render: (r: ReconciliationEvent) => new Date(r.created_at).toLocaleString() },
  ];

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "0 1rem" }}>
      <AdminSectionHeader
        title="Reconciliation Dashboard"
        subtitle="Operational visibility into trade reconciliation discrepancies."
      />

      {error && <Alert type="error">{error}</Alert>}

      <Card>
        <FilterBar
          filters={[
            {
              key: "account",
              label: "Account",
              options: accountOptions,
              value: filterAccount,
              onChange: setFilterAccount,
            },
            {
              key: "severity",
              label: "Severity",
              options: [
                { label: "All", value: "" },
                { label: "Critical", value: "critical" },
                { label: "Warning", value: "warning" },
                { label: "Info", value: "info" },
              ],
              value: filterSeverity,
              onChange: setFilterSeverity,
            },
            {
              key: "status",
              label: "Status",
              options: [
                { label: "All", value: "" },
                { label: "Open", value: "open" },
                { label: "Acknowledged", value: "acknowledged" },
                { label: "Resolved", value: "resolved" },
              ],
              value: filterStatus,
              onChange: setFilterStatus,
            },
          ]}
        />

        {loading ? (
          <LoadingState />
        ) : (
          <DataTable
            columns={columns}
            data={events}
            rowKey={(r) => r.id}
            onRowClick={(r) => setSelected(r)}
            emptyMessage="No discrepancies found."
          />
        )}
      </Card>

      {/* Detail Drawer */}
      <DetailDrawer
        open={!!selected}
        onClose={() => setSelected(null)}
        title={`Discrepancy #${selected?.id ?? ""}`}
      >
        {selected && (
          <>
            <DetailField label="Ticket">{selected.ticket}</DetailField>
            <DetailField label="Account">{selected.account_display}</DetailField>
            <DetailField label="Field">{selected.field_name}</DetailField>
            <DetailField label="MT5 Value" mono>{selected.mt5_value}</DetailField>
            <DetailField label="Platform Value" mono>{selected.platform_value}</DetailField>
            <DetailField label="Severity">
              <Badge color={SEVERITY_BADGE[selected.severity] ?? "gray"}>{selected.severity}</Badge>
            </DetailField>
            <DetailField label="Resolution Status">
              <Badge color={STATUS_BADGE[selected.resolution_status] ?? "gray"}>{selected.resolution_status}</Badge>
            </DetailField>
            <DetailField label="Run ID" mono>{selected.reconciliation_run_id}</DetailField>
            <DetailField label="Signature" mono>{selected.signature}</DetailField>
            <DetailField label="Detected">{new Date(selected.created_at).toLocaleString()}</DetailField>

            {/* Actions */}
            {canWrite && selected.resolution_status === "open" && (
              <div style={{ marginTop: "1rem" }}>
                <AuditNotice />
                <button
                  onClick={() => setConfirmAction({ type: "acknowledge", event: selected })}
                  style={{
                    background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)",
                    color: "#fff",
                    border: "none",
                    borderRadius: 999,
                    padding: "0.45rem 1.1rem",
                    fontSize: "0.85rem",
                    cursor: "pointer",
                    marginRight: "0.5rem",
                  }}
                >
                  Acknowledge
                </button>
              </div>
            )}
            {canWrite && selected.resolution_status === "acknowledged" && (
              <div style={{ marginTop: "1rem" }}>
                <AuditNotice />
                <button
                  onClick={() => setConfirmAction({ type: "resolve", event: selected })}
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
                  Mark Resolved
                </button>
              </div>
            )}
          </>
        )}
      </DetailDrawer>

      {/* Confirmation Modal */}
      <ConfirmationModal
        open={!!confirmAction}
        onClose={() => setConfirmAction(null)}
        onConfirm={handleAction}
        title={confirmAction?.type === "acknowledge" ? "Acknowledge Discrepancy" : "Resolve Discrepancy"}
        message={
          confirmAction?.type === "acknowledge"
            ? `Acknowledge discrepancy #${confirmAction.event.id} (ticket ${confirmAction.event.ticket}, field: ${confirmAction.event.field_name})? This action is audited.`
            : `Mark discrepancy #${confirmAction?.event.id} as resolved? This action is audited.`
        }
        confirmLabel={confirmAction?.type === "acknowledge" ? "Acknowledge" : "Mark Resolved"}
        loading={actionLoading}
      />
    </div>
  );
}

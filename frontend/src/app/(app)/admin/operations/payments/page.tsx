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
  ReadOnlyNotice,
  DetailField,
  SafeJsonViewer,
  LoadingState,
} from "@/components/admin/AdminShared";
import type { PaymentEvent } from "@/types/admin";

const STATUS_BADGE: Record<string, "green" | "gray" | "blue" | "red" | "yellow"> = {
  processed: "green",
  verified: "blue",
  duplicate: "gray",
  rejected: "red",
  failed: "red",
  received: "yellow",
};

export default function PaymentsPage() {
  const admin = useAdminRole();
  const [events, setEvents] = useState<PaymentEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [filterProvider, setFilterProvider] = useState("");
  const [filterType, setFilterType] = useState("");
  const [filterStatus, setFilterStatus] = useState("");

  // Detail
  const [selected, setSelected] = useState<PaymentEvent | null>(null);

  const fetchEvents = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (filterProvider) params.set("provider_name", filterProvider);
      if (filterType) params.set("provider_event_type", filterType);
      if (filterStatus) params.set("processing_status", filterStatus);
      const qs = params.toString();
      const url = `/api/billing/payment-events/${qs ? `?${qs}` : ""}`;
      const data = await apiFetch<PaymentEvent[] | { results: PaymentEvent[] }>(url);
      const list = Array.isArray(data) ? data : (data.results ?? []);
      setEvents(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load payment events.");
    } finally {
      setLoading(false);
    }
  }, [filterProvider, filterType, filterStatus]);

  useEffect(() => { fetchEvents(); }, [fetchEvents]);

  if (admin.loading) return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><LoadingState message="Verifying access…" /></div>;
  if (!admin.authorized || admin.permissions?.payments === "none") {
    return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><Alert type="error">You do not have permission to access payment events.</Alert></div>;
  }

  // Derive unique values for filters
  const providerOptions = [{ label: "All providers", value: "" }].concat(
    [...new Set(events.map((e) => e.provider_name))].filter(Boolean).map((p) => ({ label: p, value: p }))
  );
  const typeOptions = [{ label: "All types", value: "" }].concat(
    [...new Set(events.map((e) => e.provider_event_type))].filter(Boolean).map((t) => ({ label: t, value: t }))
  );

  const columns = [
    { key: "id", header: "ID", render: (r: PaymentEvent) => <span style={{ fontFamily: "var(--font-geist-mono), monospace" }}>{r.id}</span>, width: "60px" },
    { key: "provider", header: "Provider", render: (r: PaymentEvent) => r.provider_name },
    { key: "type", header: "Event Type", render: (r: PaymentEvent) => r.provider_event_type },
    { key: "status", header: "Status", render: (r: PaymentEvent) => <Badge color={STATUS_BADGE[r.processing_status] ?? "gray"}>{r.processing_status}</Badge> },
    { key: "user", header: "User", render: (r: PaymentEvent) => r.user_email ?? "—" },
    { key: "created", header: "Received", render: (r: PaymentEvent) => new Date(r.created_at).toLocaleString() },
  ];

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "0 1rem" }}>
      <AdminSectionHeader
        title="Payment Event Viewer"
        subtitle="Read-only inspection of payment lifecycle events."
      />

      {error && <Alert type="error">{error}</Alert>}

      <Card>
        <FilterBar
          filters={[
            {
              key: "provider",
              label: "Provider",
              options: providerOptions,
              value: filterProvider,
              onChange: setFilterProvider,
            },
            {
              key: "type",
              label: "Event Type",
              options: typeOptions,
              value: filterType,
              onChange: setFilterType,
            },
            {
              key: "status",
              label: "Status",
              options: [
                { label: "All", value: "" },
                { label: "Processed", value: "processed" },
                { label: "Verified", value: "verified" },
                { label: "Duplicate", value: "duplicate" },
                { label: "Rejected", value: "rejected" },
                { label: "Failed", value: "failed" },
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
            emptyMessage="No payment events found."
          />
        )}
      </Card>

      {/* Detail Drawer — read-only */}
      <DetailDrawer
        open={!!selected}
        onClose={() => setSelected(null)}
        title={`Payment Event #${selected?.id ?? ""}`}
      >
        {selected && (
          <>
            <ReadOnlyNotice message="This record is read-only. No modifications are permitted." />

            <DetailField label="Provider">{selected.provider_name}</DetailField>
            <DetailField label="Event Type">{selected.provider_event_type}</DetailField>
            <DetailField label="Provider Event ID" mono>{selected.provider_event_id}</DetailField>
            <DetailField label="Processing Status">
              <Badge color={STATUS_BADGE[selected.processing_status] ?? "gray"}>{selected.processing_status}</Badge>
            </DetailField>
            <DetailField label="Subscription Reference" mono>{selected.subscription_reference || "—"}</DetailField>
            <DetailField label="User">{selected.user_email ?? (selected.user ? `User #${selected.user}` : "—")}</DetailField>
            <DetailField label="Idempotency Key" mono>{selected.idempotency_key}</DetailField>
            <DetailField label="Provider Timestamp">{selected.provider_timestamp ? new Date(selected.provider_timestamp).toLocaleString() : "—"}</DetailField>
            <DetailField label="Processed At">{selected.processed_at ? new Date(selected.processed_at).toLocaleString() : "—"}</DetailField>
            <DetailField label="Received At">{new Date(selected.created_at).toLocaleString()}</DetailField>

            <div style={{ marginTop: "0.75rem" }}>
              <div
                style={{
                  fontSize: "0.75rem",
                  color: "#9ca3af",
                  textTransform: "uppercase",
                  letterSpacing: "0.04em",
                  marginBottom: "0.35rem",
                }}
              >
                Raw Payload (sanitized)
              </div>
              <div
                style={{
                  fontSize: "0.75rem",
                  color: "#fcd34d",
                  marginBottom: "0.4rem",
                  fontStyle: "italic",
                }}
              >
                Sensitive fields may be partially masked for security.
              </div>
              <SafeJsonViewer data={selected.raw_payload} />
            </div>
          </>
        )}
      </DetailDrawer>
    </div>
  );
}

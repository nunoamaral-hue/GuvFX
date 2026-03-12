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
  WarningBanner,
  DetailField,
  LoadingState,
} from "@/components/admin/AdminShared";
import type { EntitlementOverride } from "@/types/admin";

export default function EntitlementsPage() {
  const admin = useAdminRole();
  const [overrides, setOverrides] = useState<EntitlementOverride[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Filters
  const [filterActive, setFilterActive] = useState("active");

  // Detail
  const [selected, setSelected] = useState<EntitlementOverride | null>(null);

  // Create override form
  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState({
    user_email: "",
    capability: "",
    reason: "",
    expires_hours: "24",
  });
  const [createLoading, setCreateLoading] = useState(false);

  // Cancel/Renew confirm
  const [confirmAction, setConfirmAction] = useState<{
    type: "cancel" | "renew";
    override: EntitlementOverride;
  } | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const fetchOverrides = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      if (filterActive === "active") params.set("is_active", "true");
      else if (filterActive === "expired") params.set("is_active", "false");
      const qs = params.toString();
      const url = `/api/billing/entitlement-overrides/${qs ? `?${qs}` : ""}`;
      const data = await apiFetch<EntitlementOverride[] | { results: EntitlementOverride[] }>(url);
      const list = Array.isArray(data) ? data : (data.results ?? []);
      setOverrides(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load overrides.");
    } finally {
      setLoading(false);
    }
  }, [filterActive]);

  useEffect(() => { fetchOverrides(); }, [fetchOverrides]);

  const handleCreate = async () => {
    setCreateLoading(true);
    setError(null);
    try {
      const expiresAt = new Date(
        Date.now() + parseInt(createForm.expires_hours, 10) * 3600_000
      ).toISOString();
      await apiFetch("/api/billing/entitlement-overrides/", {
        method: "POST",
        body: JSON.stringify({
          user_email: createForm.user_email,
          capability: createForm.capability,
          reason: createForm.reason,
          expires_at: expiresAt,
        }),
      });
      setShowCreate(false);
      setCreateForm({ user_email: "", capability: "", reason: "", expires_hours: "24" });
      await fetchOverrides();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create override.");
    } finally {
      setCreateLoading(false);
    }
  };

  const handleAction = async () => {
    if (!confirmAction) return;
    setActionLoading(true);
    setError(null);
    try {
      const endpoint = confirmAction.type === "cancel"
        ? `/api/billing/entitlement-overrides/${confirmAction.override.id}/cancel/`
        : `/api/billing/entitlement-overrides/${confirmAction.override.id}/renew/`;
      await apiFetch(endpoint, { method: "POST", body: JSON.stringify({}) });
      setConfirmAction(null);
      setSelected(null);
      await fetchOverrides();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${confirmAction.type} override.`);
    } finally {
      setActionLoading(false);
    }
  };

  if (admin.loading) return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><LoadingState message="Verifying access…" /></div>;
  if (!admin.authorized || admin.permissions?.entitlements === "none") {
    return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><Alert type="error">You do not have permission to manage entitlement overrides. This surface is restricted to super_admin.</Alert></div>;
  }

  const hasActiveOverrides = overrides.some((o) => o.is_active);

  const columns = [
    { key: "user", header: "User", render: (r: EntitlementOverride) => r.user_email },
    { key: "capability", header: "Capability", render: (r: EntitlementOverride) => <span style={{ fontFamily: "var(--font-geist-mono), monospace" }}>{r.capability}</span> },
    { key: "reason", header: "Reason", render: (r: EntitlementOverride) => <span style={{ maxWidth: 200, display: "inline-block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.reason}</span> },
    {
      key: "status",
      header: "Status",
      render: (r: EntitlementOverride) => (
        <Badge color={r.is_active ? "yellow" : "gray"}>
          {r.is_active ? "Active" : "Expired"}
        </Badge>
      ),
    },
    { key: "expires", header: "Expires", render: (r: EntitlementOverride) => new Date(r.expires_at).toLocaleString() },
    { key: "created_by", header: "Created By", render: (r: EntitlementOverride) => r.created_by },
  ];

  const inputStyle: React.CSSProperties = {
    background: "rgba(0,0,0,0.3)",
    border: "1px solid #111827",
    borderRadius: 6,
    padding: "0.4rem 0.6rem",
    fontSize: "0.84rem",
    color: "#e5f4ff",
    width: "100%",
  };

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "0 1rem" }}>
      <AdminSectionHeader
        title="Entitlement Override Tools"
        subtitle="Temporary, capability-scoped operational overrides. Super admin only."
      />

      {hasActiveOverrides && (
        <WarningBanner>
          ⚠ TEMPORARY ENTITLEMENT OVERRIDE ACTIVE — {overrides.filter((o) => o.is_active).length} active override(s). Overrides do not mutate underlying subscription state.
        </WarningBanner>
      )}

      {error && <Alert type="error">{error}</Alert>}

      <Card>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.75rem" }}>
          <FilterBar
            filters={[
              {
                key: "active",
                label: "Status",
                options: [
                  { label: "Active Only", value: "active" },
                  { label: "Expired Only", value: "expired" },
                  { label: "All", value: "" },
                ],
                value: filterActive,
                onChange: setFilterActive,
              },
            ]}
          />
          {!showCreate && (
            <button
              onClick={() => setShowCreate(true)}
              style={{
                background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)",
                color: "#fff",
                border: "none",
                borderRadius: 999,
                padding: "0.45rem 1.1rem",
                fontSize: "0.85rem",
                cursor: "pointer",
                flexShrink: 0,
              }}
            >
              Apply Override
            </button>
          )}
        </div>

        {showCreate && (
          <div
            style={{
              padding: "1rem",
              borderRadius: 8,
              background: "rgba(7, 12, 30, 0.9)",
              border: "1px solid #111827",
              marginBottom: "0.75rem",
            }}
          >
            <AuditNotice />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.6rem", marginBottom: "0.6rem" }}>
              <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem", fontSize: "0.82rem", color: "#cbd5f5" }}>
                User Email *
                <input value={createForm.user_email} onChange={(e) => setCreateForm({ ...createForm, user_email: e.target.value })} placeholder="user@example.com" style={inputStyle} />
              </label>
              <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem", fontSize: "0.82rem", color: "#cbd5f5" }}>
                Capability *
                <input value={createForm.capability} onChange={(e) => setCreateForm({ ...createForm, capability: e.target.value })} placeholder="e.g. can_deploy_automation" style={inputStyle} />
              </label>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "2fr 1fr", gap: "0.6rem", marginBottom: "0.75rem" }}>
              <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem", fontSize: "0.82rem", color: "#cbd5f5" }}>
                Reason *
                <input value={createForm.reason} onChange={(e) => setCreateForm({ ...createForm, reason: e.target.value })} placeholder="Mandatory reason for override" style={inputStyle} />
              </label>
              <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem", fontSize: "0.82rem", color: "#cbd5f5" }}>
                Duration (hours)
                <input type="number" min="1" max="720" value={createForm.expires_hours} onChange={(e) => setCreateForm({ ...createForm, expires_hours: e.target.value })} style={inputStyle} />
              </label>
            </div>
            <div style={{ display: "flex", gap: "0.5rem" }}>
              <button
                onClick={handleCreate}
                disabled={!createForm.user_email || !createForm.capability || !createForm.reason || createLoading}
                style={{
                  background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)",
                  color: "#fff",
                  border: "none",
                  borderRadius: 999,
                  padding: "0.45rem 1rem",
                  fontSize: "0.85rem",
                  cursor: (!createForm.user_email || !createForm.capability || !createForm.reason) ? "not-allowed" : "pointer",
                  opacity: (!createForm.user_email || !createForm.capability || !createForm.reason) ? 0.6 : 1,
                }}
              >
                {createLoading ? "Applying…" : "Apply Override"}
              </button>
              <button
                onClick={() => { setShowCreate(false); setCreateForm({ user_email: "", capability: "", reason: "", expires_hours: "24" }); }}
                style={{ background: "transparent", border: "1px solid #111827", borderRadius: 999, padding: "0.45rem 1rem", fontSize: "0.85rem", color: "#cbd5f5", cursor: "pointer" }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}

        {loading ? (
          <LoadingState />
        ) : (
          <DataTable
            columns={columns}
            data={overrides}
            rowKey={(r) => r.id}
            onRowClick={(r) => setSelected(r)}
            emptyMessage="No entitlement overrides found."
          />
        )}
      </Card>

      {/* Detail Drawer */}
      <DetailDrawer
        open={!!selected}
        onClose={() => setSelected(null)}
        title={`Override #${selected?.id ?? ""}`}
      >
        {selected && (
          <>
            {selected.is_active && (
              <WarningBanner>
                ⚠ TEMPORARY ENTITLEMENT OVERRIDE ACTIVE
              </WarningBanner>
            )}
            <DetailField label="User">{selected.user_email}</DetailField>
            <DetailField label="Capability" mono>{selected.capability}</DetailField>
            <DetailField label="Reason">{selected.reason}</DetailField>
            <DetailField label="Created By">{selected.created_by}</DetailField>
            <DetailField label="Status">
              <Badge color={selected.is_active ? "yellow" : "gray"}>
                {selected.is_active ? "Active" : "Expired"}
              </Badge>
            </DetailField>
            <DetailField label="Expires At">{new Date(selected.expires_at).toLocaleString()}</DetailField>
            <DetailField label="Created At">{new Date(selected.created_at).toLocaleString()}</DetailField>

            {/* Actions */}
            {selected.is_active && (
              <div style={{ marginTop: "1rem" }}>
                <AuditNotice />
                <div style={{ display: "flex", gap: "0.5rem" }}>
                  <button
                    onClick={() => setConfirmAction({ type: "renew", override: selected })}
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
                    Renew Override
                  </button>
                  <button
                    onClick={() => setConfirmAction({ type: "cancel", override: selected })}
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
                    Cancel Override
                  </button>
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
        title={confirmAction?.type === "renew" ? "Renew Override" : "Cancel Override"}
        message={
          confirmAction?.type === "renew"
            ? `Renew entitlement override for "${confirmAction.override.capability}" on ${confirmAction.override.user_email}? Renewals are audited separately.`
            : `Cancel entitlement override for "${confirmAction?.override.capability}" on ${confirmAction?.override.user_email}? This action is audited.`
        }
        confirmLabel={confirmAction?.type === "renew" ? "Renew" : "Cancel Override"}
        danger={confirmAction?.type === "cancel"}
        loading={actionLoading}
      />
    </div>
  );
}

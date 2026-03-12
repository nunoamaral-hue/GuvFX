"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";
import { Alert } from "@/components/ui/Alert";
import { useAdminRole } from "@/components/admin/useAdminRole";
import {
  AdminSectionHeader,
  DataTable,
  DetailDrawer,
  ConfirmationModal,
  AuditNotice,
  WarningBanner,
  OneTimeSecretPanel,
  DetailField,
  LoadingState,
} from "@/components/admin/AdminShared";
import type { WorkerIdentity, WorkerCreateResponse, WorkerRotateResponse } from "@/types/admin";

const STATUS_BADGE: Record<string, "green" | "red" | "gray"> = {
  active: "green",
  revoked: "red",
  suspended: "gray",
};

export default function WorkersPage() {
  const admin = useAdminRole();
  const [workers, setWorkers] = useState<WorkerIdentity[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Detail
  const [selected, setSelected] = useState<WorkerIdentity | null>(null);

  // Create worker
  const [showCreate, setShowCreate] = useState(false);
  const [createWorkerId, setCreateWorkerId] = useState("");
  const [createPerms, setCreatePerms] = useState("");
  const [createLoading, setCreateLoading] = useState(false);

  // One-time secret display
  const [oneTimeSecret, setOneTimeSecret] = useState<{ workerId: string; secret: string } | null>(null);

  // Confirmation for rotate/revoke
  const [confirmAction, setConfirmAction] = useState<{
    type: "rotate" | "revoke";
    worker: WorkerIdentity;
  } | null>(null);
  const [actionLoading, setActionLoading] = useState(false);

  const fetchWorkers = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await apiFetch<WorkerIdentity[] | { results: WorkerIdentity[] }>("/api/execution/workers/");
      const list = Array.isArray(data) ? data : (data.results ?? []);
      setWorkers(list);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load workers.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchWorkers(); }, [fetchWorkers]);

  const handleCreate = async () => {
    setCreateLoading(true);
    setError(null);
    try {
      const body: Record<string, unknown> = { worker_id: createWorkerId };
      if (createPerms.trim()) body.permission_set = createPerms.split(",").map((s) => s.trim());
      const res = await apiFetch<WorkerCreateResponse>("/api/execution/workers/", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setOneTimeSecret({ workerId: res.worker_id, secret: res.secret });
      setShowCreate(false);
      setCreateWorkerId("");
      setCreatePerms("");
      await fetchWorkers();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create worker.");
    } finally {
      setCreateLoading(false);
    }
  };

  const handleAction = async () => {
    if (!confirmAction) return;
    setActionLoading(true);
    setError(null);
    try {
      if (confirmAction.type === "rotate") {
        const res = await apiFetch<WorkerRotateResponse>(
          `/api/execution/workers/${confirmAction.worker.worker_id}/rotate/`,
          { method: "POST", body: JSON.stringify({}) }
        );
        setOneTimeSecret({ workerId: res.worker_id, secret: res.secret });
      } else {
        await apiFetch(
          `/api/execution/workers/${confirmAction.worker.worker_id}/revoke/`,
          { method: "POST", body: JSON.stringify({}) }
        );
      }
      setConfirmAction(null);
      setSelected(null);
      await fetchWorkers();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Failed to ${confirmAction.type} worker.`);
    } finally {
      setActionLoading(false);
    }
  };

  if (admin.loading) return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><LoadingState message="Verifying access…" /></div>;
  if (!admin.authorized || admin.permissions?.workers === "none") {
    return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><Alert type="error">You do not have permission to manage workers.</Alert></div>;
  }

  const columns = [
    { key: "worker_id", header: "Worker ID", render: (r: WorkerIdentity) => <span style={{ fontFamily: "var(--font-geist-mono), monospace" }}>{r.worker_id}</span> },
    { key: "status", header: "Status", render: (r: WorkerIdentity) => <Badge color={STATUS_BADGE[r.status] ?? "gray"}>{r.status}</Badge> },
    { key: "permissions", header: "Permissions", render: (r: WorkerIdentity) => r.permission_set.length > 0 ? r.permission_set.join(", ") : <span style={{ color: "#9ca3af" }}>—</span> },
    { key: "created", header: "Created", render: (r: WorkerIdentity) => new Date(r.created_at).toLocaleString() },
    { key: "rotated", header: "Last Rotated", render: (r: WorkerIdentity) => r.last_rotated_at ? new Date(r.last_rotated_at).toLocaleString() : "—" },
  ];

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "0 1rem" }}>
      <AdminSectionHeader
        title="Worker Identity Management"
        subtitle="Create, rotate, and revoke execution worker identities."
      />

      <WarningBanner>
        High-sensitivity surface. Worker secrets are shown only once at creation or rotation and are never retrievable afterward.
      </WarningBanner>

      {error && <Alert type="error">{error}</Alert>}

      {/* One-time secret display */}
      {oneTimeSecret && (
        <OneTimeSecretPanel
          secret={oneTimeSecret.secret}
          onDismiss={() => setOneTimeSecret(null)}
        />
      )}

      <Card>
        {/* Create worker action */}
        {!showCreate ? (
          <div style={{ marginBottom: "0.75rem" }}>
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
              }}
            >
              Create Worker
            </button>
          </div>
        ) : (
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
            <div style={{ display: "flex", flexWrap: "wrap", gap: "0.6rem", alignItems: "flex-end" }}>
              <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem", fontSize: "0.82rem", color: "#cbd5f5" }}>
                Worker ID
                <input
                  value={createWorkerId}
                  onChange={(e) => setCreateWorkerId(e.target.value)}
                  placeholder="e.g. mt5-worker-prod"
                  style={{
                    background: "rgba(0,0,0,0.3)",
                    border: "1px solid #111827",
                    borderRadius: 6,
                    padding: "0.4rem 0.6rem",
                    fontSize: "0.84rem",
                    color: "#e5f4ff",
                    width: 200,
                  }}
                />
              </label>
              <label style={{ display: "flex", flexDirection: "column", gap: "0.2rem", fontSize: "0.82rem", color: "#cbd5f5" }}>
                Permissions (comma-separated)
                <input
                  value={createPerms}
                  onChange={(e) => setCreatePerms(e.target.value)}
                  placeholder="e.g. execute_trades,read_positions"
                  style={{
                    background: "rgba(0,0,0,0.3)",
                    border: "1px solid #111827",
                    borderRadius: 6,
                    padding: "0.4rem 0.6rem",
                    fontSize: "0.84rem",
                    color: "#e5f4ff",
                    width: 260,
                  }}
                />
              </label>
              <button
                onClick={handleCreate}
                disabled={!createWorkerId.trim() || createLoading}
                style={{
                  background: "linear-gradient(135deg, #2979ff 0%, #3fe0ff 100%)",
                  color: "#fff",
                  border: "none",
                  borderRadius: 999,
                  padding: "0.45rem 1rem",
                  fontSize: "0.85rem",
                  cursor: createLoading || !createWorkerId.trim() ? "not-allowed" : "pointer",
                  opacity: !createWorkerId.trim() ? 0.6 : 1,
                }}
              >
                {createLoading ? "Creating…" : "Create"}
              </button>
              <button
                onClick={() => { setShowCreate(false); setCreateWorkerId(""); setCreatePerms(""); }}
                style={{
                  background: "transparent",
                  border: "1px solid #111827",
                  borderRadius: 999,
                  padding: "0.45rem 1rem",
                  fontSize: "0.85rem",
                  color: "#cbd5f5",
                  cursor: "pointer",
                }}
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
            data={workers}
            rowKey={(r) => r.id}
            onRowClick={(r) => setSelected(r)}
            emptyMessage="No workers configured."
          />
        )}
      </Card>

      {/* Detail Drawer */}
      <DetailDrawer
        open={!!selected}
        onClose={() => setSelected(null)}
        title={`Worker: ${selected?.worker_id ?? ""}`}
      >
        {selected && (
          <>
            <DetailField label="Worker ID" mono>{selected.worker_id}</DetailField>
            <DetailField label="Status">
              <Badge color={STATUS_BADGE[selected.status] ?? "gray"}>{selected.status}</Badge>
            </DetailField>
            <DetailField label="Permission Set">
              {selected.permission_set.length > 0 ? selected.permission_set.join(", ") : "None"}
            </DetailField>
            <DetailField label="Created">{new Date(selected.created_at).toLocaleString()}</DetailField>
            <DetailField label="Last Rotated">{selected.last_rotated_at ? new Date(selected.last_rotated_at).toLocaleString() : "Never"}</DetailField>

            {/* Actions */}
            {selected.status === "active" && (
              <div style={{ marginTop: "1rem" }}>
                <AuditNotice />
                <div style={{ display: "flex", gap: "0.5rem" }}>
                  <button
                    onClick={() => setConfirmAction({ type: "rotate", worker: selected })}
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
                    Rotate Secret
                  </button>
                  <button
                    onClick={() => setConfirmAction({ type: "revoke", worker: selected })}
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
                    Revoke Worker
                  </button>
                </div>
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
        title={confirmAction?.type === "rotate" ? "Rotate Worker Secret" : "Revoke Worker"}
        message={
          confirmAction?.type === "rotate"
            ? `Rotate the secret for worker "${confirmAction.worker.worker_id}"? The new secret will be shown once. This action is audited.`
            : `Revoke worker "${confirmAction?.worker.worker_id}"? This will permanently disable this worker identity. This action is audited.`
        }
        confirmLabel={confirmAction?.type === "rotate" ? "Rotate Secret" : "Revoke Worker"}
        danger={confirmAction?.type === "revoke"}
        loading={actionLoading}
      />
    </div>
  );
}

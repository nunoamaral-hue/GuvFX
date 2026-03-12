"use client";

import Link from "next/link";
import { useAdminRole } from "@/components/admin/useAdminRole";
import { AdminSectionHeader, LoadingState } from "@/components/admin/AdminShared";
import { Alert } from "@/components/ui/Alert";

const SECTIONS = [
  {
    key: "reconciliation",
    title: "Reconciliation",
    description: "View and manage trade reconciliation discrepancies.",
    href: "/admin/operations/reconciliation",
    permKey: "reconciliation" as const,
  },
  {
    key: "payments",
    title: "Payments",
    description: "Inspect payment lifecycle events (read-only).",
    href: "/admin/operations/payments",
    permKey: "payments" as const,
  },
  {
    key: "workers",
    title: "Workers",
    description: "Manage execution worker identities and secrets.",
    href: "/admin/operations/workers",
    permKey: "workers" as const,
  },
  {
    key: "entitlements",
    title: "Entitlements",
    description: "Temporary capability-scoped entitlement overrides.",
    href: "/admin/operations/entitlements",
    permKey: "entitlements" as const,
  },
  {
    key: "execution-jobs",
    title: "Execution Jobs",
    description: "Inspect execution pipeline state and job lifecycle.",
    href: "/admin/operations/execution-jobs",
    permKey: "execution_jobs" as const,
  },
];

export default function AdminOperationsOverview() {
  const admin = useAdminRole();

  if (admin.loading) return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><LoadingState message="Verifying access…" /></div>;
  if (!admin.authorized) return <div style={{ maxWidth: 1100, margin: "0 auto", padding: "2rem 1rem" }}><Alert type="error">You do not have permission to access the operations console.</Alert></div>;

  const visible = SECTIONS.filter((s) => {
    const perm = admin.permissions?.[s.permKey];
    return perm && perm !== "none";
  });

  return (
    <div style={{ maxWidth: 1100, margin: "0 auto", padding: "0 1rem" }}>
      <AdminSectionHeader
        title="Operations Console"
        subtitle="Internal administration workspace for platform operations."
      />
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
          gap: "0.85rem",
        }}
      >
        {visible.map((s) => (
          <Link
            key={s.key}
            href={s.href}
            style={{
              textDecoration: "none",
              display: "block",
              borderRadius: 10,
              padding: "1.1rem 1.25rem",
              background: "rgba(7, 12, 30, 0.96)",
              border: "1px solid rgba(148, 163, 184, 0.35)",
              transition: "border-color 0.15s, box-shadow 0.15s",
            }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLAnchorElement).style.borderColor = "rgba(63, 224, 255, 0.45)";
              (e.currentTarget as HTMLAnchorElement).style.boxShadow = "0 4px 20px rgba(41, 121, 255, 0.15)";
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLAnchorElement).style.borderColor = "rgba(148, 163, 184, 0.35)";
              (e.currentTarget as HTMLAnchorElement).style.boxShadow = "none";
            }}
          >
            <div style={{ fontSize: "1.05rem", fontWeight: 500, color: "#e5f4ff", marginBottom: "0.3rem" }}>
              {s.title}
            </div>
            <div style={{ fontSize: "0.84rem", color: "#8fa0b7" }}>
              {s.description}
            </div>
          </Link>
        ))}
      </div>
    </div>
  );
}

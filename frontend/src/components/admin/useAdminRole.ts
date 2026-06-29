"use client";

import { useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import type { AdminRole, AdminPermissions } from "@/types/admin";

type MeResponse = {
  id: number;
  email: string;
  username: string;
  is_staff: boolean;
  is_superuser: boolean;
  admin_role?: AdminRole;
};

const PERMISSION_MAP: Record<AdminRole, AdminPermissions> = {
  super_admin: {
    reconciliation: "full",
    payments: "read",
    workers: "full",
    entitlements: "full",
    execution_jobs: "full",
  },
  finance_admin: {
    reconciliation: "full",
    payments: "read",
    workers: "none",
    entitlements: "none",
    execution_jobs: "read",
  },
  ops_admin: {
    reconciliation: "acknowledge",
    payments: "none",
    workers: "full",
    entitlements: "none",
    execution_jobs: "full",
  },
};

export type AdminContext = {
  loading: boolean;
  authorized: boolean;
  role: AdminRole | null;
  permissions: AdminPermissions | null;
  userId: number | null;
  email: string;
};

/**
 * Hook that resolves the current user's admin role and permissions.
 *
 * Backend is the final enforcement authority — this hook drives
 * frontend rendering only. If the backend does not yet expose
 * admin_role, we fall back to is_superuser → super_admin for
 * staff users.
 */
export function useAdminRole(): AdminContext {
  const [ctx, setCtx] = useState<AdminContext>({
    loading: true,
    authorized: false,
    role: null,
    permissions: null,
    userId: null,
    email: "",
  });

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const me = await apiFetch<MeResponse>("/api/auth/me/");
        if (cancelled) return;

        if (!me.is_staff && !me.is_superuser) {
          setCtx({ loading: false, authorized: false, role: null, permissions: null, userId: me.id, email: me.email });
          return;
        }

        // Use backend-provided role if available, else derive from flags
        const role: AdminRole =
          me.admin_role ??
          (me.is_superuser ? "super_admin" : "ops_admin");

        setCtx({
          loading: false,
          authorized: true,
          role,
          permissions: PERMISSION_MAP[role],
          userId: me.id,
          email: me.email,
        });
      } catch {
        if (!cancelled) {
          setCtx({ loading: false, authorized: false, role: null, permissions: null, userId: null, email: "" });
        }
      }
    })();
    return () => { cancelled = true; };
  }, []);

  return ctx;
}

"use client";

import { AppShell } from "@/components/AppShell";

/**
 * App Layout — Wraps all authenticated routes in AppShell.
 *
 * Routes under (app)/ are the main application surface:
 *   /dashboard, /accounts, /strategies/*, /backtests/*, /charts,
 *   /profile, /trading/*, /analytics/*, /admin/*
 *
 * AppShell provides: sidebar navigation, header bar, auth context,
 * language context, and the legal footer.
 *
 * Individual page files should NOT import or wrap with AppShell.
 */
export default function AppLayout({ children }: { children: React.ReactNode }) {
  return <AppShell>{children}</AppShell>;
}

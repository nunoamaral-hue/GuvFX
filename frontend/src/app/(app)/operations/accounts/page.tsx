"use client";

import React, { useEffect, useState } from "react";
import { notFound } from "next/navigation";
import { operationsEnabled } from "@/lib/flags";
import { useAdminRole } from "@/components/admin/useAdminRole";
import { listAccounts } from "@/lib/broker-api";
import { OpsAccountCard } from "@/components/operations/OpsAccountCard";
import { EmptyState, ErrorState, LoadingState } from "@/components/broker/States";
import { toCustomerError } from "@/lib/broker-status";
import type { BrokerAccount } from "@/types/broker";

/** WP5.3 (ADR-0032) — Operations & Support: accounts list. Flag-gated 404 when OFF (BEFORE any hook/API);
 * operator-only (useAdminRole; the backend independently enforces owner-scoping). Read-only. The fetch
 * runs in an async IIFE inside the effect so no setState is called synchronously in the effect body
 * (react-hooks/set-state-in-effect); loading-resets happen in the retry handler, not in the effect. */
const wrap: React.CSSProperties = { maxWidth: 960, margin: "0 auto", padding: "1.5rem 1rem" };

export default function OperationsAccountsPage() {
  if (!operationsEnabled()) notFound();

  const { loading: authLoading, authorized } = useAdminRole();
  const [accounts, setAccounts] = useState<BrokerAccount[] | null>(null);
  const [error, setError] = useState("");
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    if (!authorized) return;
    let cancelled = false;
    void (async () => {
      try {
        const list = await listAccounts();
        if (!cancelled) { setAccounts(list); setError(""); }
      } catch (err) {
        if (!cancelled) setError(toCustomerError(err, "We couldn't load accounts."));
      }
    })();
    return () => { cancelled = true; };
  }, [authorized, reloadKey]);

  // Reload from a user gesture (not an effect) — synchronous setState here is fine.
  const retry = () => { setAccounts(null); setError(""); setReloadKey((k) => k + 1); };

  if (authLoading) return <div style={wrap}><LoadingState label="Loading…" /></div>;
  if (!authorized) {
    return (
      <div style={wrap}>
        <EmptyState title="Restricted"
                    body="The Operations & Support console is available to internal operators only." />
      </div>
    );
  }

  return (
    <div style={wrap}>
      <h1 style={{ margin: "0 0 4px", fontSize: "1.5rem", color: "#e9f4ff" }}>Operations &amp; Support</h1>
      <p style={{ margin: "0 0 18px", color: "#8fa0b7", fontSize: "0.9rem" }}>
        Read-only operational timeline and account status. Select an account to view its overview.
      </p>
      {error
        ? <ErrorState message={error} onRetry={retry} />
        : accounts === null
          ? <LoadingState label="Loading accounts…" />
          : accounts.length === 0
            ? <EmptyState title="No accounts" body="There are no broker accounts to show." />
            : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))", gap: 14 }}>
                {accounts.map((a) => <OpsAccountCard key={a.id} account={a} />)}
              </div>
            )}
    </div>
  );
}

"use client";

import React, { useCallback, useEffect, useState } from "react";
import { listAccounts, getBrokerStatus } from "@/lib/broker-api";
import { AccountCard } from "@/components/broker/AccountCard";
import { BrokerAccountWizard } from "@/components/broker/BrokerAccountWizard";
import { EmptyState, ErrorState, LoadingState } from "@/components/broker/States";
import { toCustomerError } from "@/lib/broker-status";
import { Button } from "@/components/ui/Button";
import type { BrokerAccount, BrokerStatus } from "@/types/broker";

/** WP4.2 broker-accounts LIST body, extracted so the canonical customer route (/accounts, packet WS-A)
 * and the legacy /broker-accounts redirect share one implementation. The caller owns the flag gate;
 * this component assumes the broker-connectivity journey is built. Per-account broker/status is fetched
 * alongside the list; a status that is unavailable degrades gracefully (the card still renders). */
export function BrokerAccountsContent() {
  const [accounts, setAccounts] = useState<BrokerAccount[] | null>(null);
  const [statuses, setStatuses] = useState<Record<number, BrokerStatus | null>>({});
  const [statusLoading, setStatusLoading] = useState(false);
  const [error, setError] = useState("");
  const [wizardOpen, setWizardOpen] = useState(false);

  const load = useCallback(async () => {
    setError("");
    setAccounts(null);
    try {
      const list = await listAccounts();
      setAccounts(list);
      setStatusLoading(true);
      const entries = await Promise.all(list.map(async (a) => {
        try { return [a.id, await getBrokerStatus(a.id)] as const; }
        catch { return [a.id, null] as const; } // status unavailable → degrade, don't fail the page
      }));
      setStatuses(Object.fromEntries(entries));
    } catch (err) {
      setError(toCustomerError(err, "We couldn't load your broker accounts."));
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: "1.5rem 1rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12, marginBottom: 18 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: "1.5rem", color: "#e9f4ff" }}>Broker accounts</h1>
          <p style={{ margin: "4px 0 0", color: "#8fa0b7", fontSize: "0.9rem" }}>
            Connect and validate the broker accounts your strategies trade on.
          </p>
        </div>
        {accounts && accounts.length > 0 && <Button onClick={() => setWizardOpen(true)}>Add account</Button>}
      </div>

      {error
        ? <ErrorState message={error} onRetry={() => void load()} />
        : accounts === null
          ? <LoadingState label="Loading broker accounts…" />
          : accounts.length === 0
            ? <EmptyState action={<Button onClick={() => setWizardOpen(true)}>Add account</Button>} />
            : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 14 }}>
                {accounts.map((a) => (
                  <AccountCard key={a.id} account={a} status={statuses[a.id]} statusLoading={statusLoading && !(a.id in statuses)} />
                ))}
              </div>
            )}

      <BrokerAccountWizard open={wizardOpen} onClose={() => setWizardOpen(false)} onAdded={() => void load()} />
    </div>
  );
}

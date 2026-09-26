"use client";

import React, { useCallback, useEffect, useState } from "react";
import {
  getBrokerStatus, getEntitlementSummary, listAccounts, openMt5Desktop, setAccountActive,
} from "@/lib/broker-api";
import { AccountCard } from "@/components/broker/AccountCard";
import { BrokerAccountWizard } from "@/components/broker/BrokerAccountWizard";
import { SwitchActiveDialog } from "@/components/broker/SwitchActiveDialog";
import { EmptyState, ErrorState, LoadingState } from "@/components/broker/States";
import { toCustomerError } from "@/lib/broker-status";
import { fetchJourney, type HostedJourney } from "@/lib/hosted-journey";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import type { BrokerAccount, BrokerStatus, EntitlementSummary } from "@/types/broker";

/** Phase 9 — member-friendly copy for the hosted-workspace journey banner (no infrastructure terms). Makes
 * the WAITING_FOR_LOGIN → Open MT5 → connected → ready journey understandable to a non-technical member. */
function hostedBanner(journey: HostedJourney): { type: "info"; text: string } | null {
  switch (journey.phase) {
    case "WORKSPACE_REQUESTED":
    case "WORKSPACE_PREPARING":
      return { type: "info", text: "We're setting up your private MetaTrader terminal. This usually takes a few minutes — you can stay on this page." };
    case "AWAITING_BROKER_LOGIN":
      return { type: "info", text: "Your terminal is ready. Open MetaTrader on your broker account and log in to finish connecting — your password is entered securely inside MetaTrader." };
    case "BROKER_CONNECTED":
    case "ACCOUNT_CONFIRMATION_REQUIRED":
      return { type: "info", text: "Connected. Confirm this is your trading account to finish setting it up." };
    case "ACCOUNT_BOUND":
    case "WORKSPACE_READY":
      return { type: "info", text: "Your trading workspace is ready. Choose a strategy and set your risk to start." };
    default:
      return null; // NO_WORKSPACE / WORKSPACE_UNAVAILABLE → no banner (accounts list speaks for itself)
  }
}

/** WP4.2 broker-accounts LIST body, extended in Phase C4 (DARK) for the multi-account customer view.
 *
 * The caller owns the flag gate; this component assumes the broker-connectivity journey is built. Per-account
 * broker/status is fetched alongside the list; a status that is unavailable degrades gracefully (the card
 * still renders). C4 additions (all customer-visible, no infra concepts):
 *  - an entitlement header — "Active N / limit" for CONCURRENT, "Only one account can trade at a time" for
 *    STANDARD (from GET entitlement-summary; degrades to the plain heading if unavailable);
 *  - a per-account "View MT5" action that is account-EXPLICIT (passes the TradingAccount.id, never a guess);
 *  - a per-account Activate/Deactivate control. In STANDARD mode, activating a different account opens a
 *    confirm modal that explains the current account stops trading. Toggling active NEVER arms execution and
 *    the one-active-per-user switch is enforced on the backend only when concurrent enforcement is armed. */
export function BrokerAccountsContent() {
  const [accounts, setAccounts] = useState<BrokerAccount[] | null>(null);
  const [statuses, setStatuses] = useState<Record<number, BrokerStatus | null>>({});
  const [statusLoading, setStatusLoading] = useState(false);
  const [entitlement, setEntitlement] = useState<EntitlementSummary | null>(null);
  const [journey, setJourney] = useState<HostedJourney | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<{ type: "error" | "info"; message: string } | null>(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  // STANDARD-switch confirmation state.
  const [switchTarget, setSwitchTarget] = useState<BrokerAccount | null>(null);
  const [switchBusy, setSwitchBusy] = useState(false);
  const [switchError, setSwitchError] = useState("");

  const load = useCallback(async () => {
    setError("");
    setAccounts(null);
    try {
      const list = await listAccounts();
      setAccounts(list);
      // Entitlement summary is best-effort: if it fails, the header degrades to the plain heading.
      getEntitlementSummary().then(setEntitlement).catch(() => setEntitlement(null));
      // Hosted-workspace journey is best-effort: a hosted customer gets a member-friendly status banner; a
      // traditional customer (404 → unavailable) simply shows no banner. Never fails the page.
      fetchJourney().then((r) => setJourney(r.ok ? r.journey : null)).catch(() => setJourney(null));
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

  const isConcurrent = entitlement?.account_mode === "concurrent";

  const currentActive = useCallback((): BrokerAccount | null => {
    if (!accounts) return null;
    return accounts.find((a) => (statuses[a.id]?.is_active ?? a.is_active)) || null;
  }, [accounts, statuses]);

  // Account-explicit "View MT5": open the desktop link for exactly the clicked account.
  const handleViewMt5 = useCallback(async (accountId: number) => {
    setBusyId(accountId);
    setNotice(null);
    try {
      const res = await openMt5Desktop(accountId);
      if (res.url) {
        window.open(res.url, "_blank", "noopener,noreferrer");
        setNotice({ type: "info", message: "Opening your MT5 terminal…" });
      } else {
        setNotice({ type: "info", message: res.detail || "The terminal viewer isn't available for this account." });
      }
    } catch (err) {
      setNotice({ type: "error", message: toCustomerError(err, "We couldn't open the MT5 terminal.") });
    } finally {
      setBusyId(null);
    }
  }, []);

  const doSetActive = useCallback(async (account: BrokerAccount, nextActive: boolean) => {
    setBusyId(account.id);
    setNotice(null);
    try {
      await setAccountActive(account.id, nextActive);
      await load();
    } catch (err) {
      setNotice({ type: "error", message: toCustomerError(err, "We couldn't update the account.") });
    } finally {
      setBusyId(null);
    }
  }, [load]);

  // In STANDARD mode, activating a DIFFERENT account is a switch (the current one stops) → confirm first.
  const handleSetActive = useCallback((account: BrokerAccount, nextActive: boolean) => {
    const active = currentActive();
    if (nextActive && !isConcurrent && active && active.id !== account.id) {
      setSwitchError("");
      setSwitchTarget(account);
      return;
    }
    void doSetActive(account, nextActive);
  }, [currentActive, isConcurrent, doSetActive]);

  const confirmSwitch = useCallback(async () => {
    if (!switchTarget) return;
    setSwitchBusy(true);
    setSwitchError("");
    try {
      await setAccountActive(switchTarget.id, true);
      setSwitchTarget(null);
      await load();
    } catch (err) {
      setSwitchError(toCustomerError(err, "We couldn't switch accounts. Please try again."));
    } finally {
      setSwitchBusy(false);
    }
  }, [switchTarget, load]);

  const activeCount = accounts
    ? accounts.filter((a) => (statuses[a.id]?.is_active ?? a.is_active)).length
    : 0;
  const entitlementLine = entitlement
    ? (isConcurrent
        ? `Active ${activeCount} / ${entitlement.concurrent_limit}`
        : "Only one account can trade at a time")
    : "Connect and validate the broker accounts your strategies trade on.";

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: "1.5rem 1rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12, marginBottom: 18 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: "1.5rem", color: "#e9f4ff" }}>Broker accounts</h1>
          <p style={{ margin: "4px 0 0", color: "#8fa0b7", fontSize: "0.9rem" }} data-testid="entitlement-line">
            {entitlementLine}
          </p>
        </div>
        {accounts && accounts.length > 0 && <Button onClick={() => setWizardOpen(true)}>Add account</Button>}
      </div>

      {notice && (
        <div style={{ marginBottom: 14 }}>
          <Alert type={notice.type}>{notice.message}</Alert>
        </div>
      )}

      {/* Phase 9 — hosted-workspace member journey banner (preparing → open MT5 & log in → connected → ready). */}
      {journey && hostedBanner(journey) && (
        <div style={{ marginBottom: 14 }} data-testid="hosted-journey-banner">
          <Alert type="info">{hostedBanner(journey)!.text}</Alert>
        </div>
      )}

      {error
        ? <ErrorState message={error} onRetry={() => void load()} />
        : accounts === null
          ? <LoadingState label="Loading broker accounts…" />
          : accounts.length === 0
            ? <EmptyState action={<Button onClick={() => setWizardOpen(true)}>Add account</Button>} />
            : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 14 }}>
                {accounts.map((a) => (
                  <AccountCard key={a.id} account={a} status={statuses[a.id]}
                    statusLoading={statusLoading && !(a.id in statuses)}
                    onViewMt5={handleViewMt5} onSetActive={handleSetActive} busy={busyId === a.id} />
                ))}
              </div>
            )}

      <BrokerAccountWizard open={wizardOpen} onClose={() => setWizardOpen(false)} onAdded={() => void load()} />
      <SwitchActiveDialog open={switchTarget !== null} target={switchTarget} currentActive={currentActive()}
        busy={switchBusy} error={switchError} onConfirm={() => void confirmSwitch()}
        onClose={() => { if (!switchBusy) setSwitchTarget(null); }} />
    </div>
  );
}

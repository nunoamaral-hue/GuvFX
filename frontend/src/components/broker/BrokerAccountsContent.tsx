"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  getBrokerStatus, getDeliveryState, getEntitlementSummary, listAccounts, openMt5Desktop, setAccountActive,
} from "@/lib/broker-api";
import { AccountCard } from "@/components/broker/AccountCard";
import { BrokerAccountWizard } from "@/components/broker/BrokerAccountWizard";
import { SwitchActiveDialog } from "@/components/broker/SwitchActiveDialog";
import { EmptyState, ErrorState, LoadingState } from "@/components/broker/States";
import { brokerLabel, toCustomerError } from "@/lib/broker-status";
import { fetchJourney, type HostedJourney } from "@/lib/hosted-journey";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import type { BrokerAccount, BrokerStatus, DeliveryStateResult, EntitlementSummary } from "@/types/broker";

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
  const [deliveries, setDeliveries] = useState<Record<number, DeliveryStateResult | null>>({});
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
    setReadyToast(null);       // clear any prior ready toast on an explicit (re)load
    refetchGenRef.current++;   // supersede any in-flight poll refetch so it can't commit over this fresh load
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
      // Per-account hosted delivery signal (authoritative "can Open MT5" + broker-connection lifecycle).
      // Best-effort + account-explicit (owner-scoped/IDOR-safe on the backend); null for traditional accounts.
      const deliv = await Promise.all(list.map(async (a) => {
        try { return [a.id, await getDeliveryState(a.id)] as const; }
        catch { return [a.id, null] as const; }
      }));
      setDeliveries(Object.fromEntries(deliv));
    } catch (err) {
      setError(toCustomerError(err, "We couldn't load your broker accounts."));
    } finally {
      setStatusLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const isHostedAcct = useCallback((a: BrokerAccount) =>
    (a.mt5_instance === null || a.mt5_instance === undefined) && a.readiness_provider === "persistent_workspace",
  []);

  // Lightweight live refetch (accounts + per-account delivery) used by the bounded poll, so both the
  // provisioning lifecycle (Setting up -> Ready to log in -> Broker connected) and the managed-Start
  // trading state (Preparing -> Trading) auto-advance without a manual page refresh.
  const refetchGenRef = useRef(0);
  const refetchLive = useCallback(async () => {
    const gen = ++refetchGenRef.current;   // single-flight token: a superseded refetch never commits stale state
    let list: BrokerAccount[];
    try { list = await listAccounts(); } catch { return; }   // transient — keep last state, next tick retries
    if (gen !== refetchGenRef.current) return;               // a newer refetch/load started — drop this one
    setAccounts(list);
    const deliv = await Promise.all(list.map(async (a) => {
      try {
        const d = await getDeliveryState(a.id);
        // Preserve a known-good (deliverable) signal if this fetch blanked it (transient) — don't regress a
        // ready terminal's card to "setting up", and don't churn the toast baseline off a blip.
        return [a.id, d ?? (deliveries[a.id]?.deliverable ? deliveries[a.id] : d)] as const;
      } catch {
        return [a.id, deliveries[a.id] ?? null] as const;
      }
    }));
    if (gen !== refetchGenRef.current) return;               // superseded before the delivery commit
    setDeliveries((prev) => ({ ...prev, ...Object.fromEntries(deliv) }));
  }, [deliveries]);

  // Phase 5/7 — poll while any account is still PROVISIONING (hosted, not yet deliverable) OR "Preparing
  // automated trading" (managed Start completing async), so the card auto-transitions without a manual refresh.
  // Bounded to ~3 min wall-clock (a deadline ref, so it never loops forever); stops when everything is stable.
  const pollDeadlineRef = useRef<number | null>(null);
  const [longRunning, setLongRunning] = useState(false);
  useEffect(() => {
    const list = (accounts || []).filter((a) => !a.is_removed);
    const anyProvisioning = list.some((a) => isHostedAcct(a) && !deliveries[a.id]?.deliverable);
    const anyPreparing = list.some((a) => a.trading_state?.state === "PREPARING");
    if (!anyProvisioning && !anyPreparing) { pollDeadlineRef.current = null; setLongRunning(false); return; }
    if (pollDeadlineRef.current === null) pollDeadlineRef.current = Date.now() + 180_000;
    if (Date.now() > pollDeadlineRef.current) { setLongRunning(true); return; }  // bound reached — friendly state
    const id = setTimeout(() => { void refetchLive(); }, 6_000);
    return () => clearTimeout(id);
  }, [accounts, deliveries, isHostedAcct, refetchLive]);

  // Phase 6 — one-shot "your terminal is ready" toast on a GENUINE deliverable false->true transition. The
  // baseline is a forward-carried accumulator (never wiped by a transient accounts=null render) that records
  // ONLY accounts whose delivery signal is KNOWN, and fires only on a strict recorded-false -> true edge — so
  // it never fires on first load or on a load()-triggered refetch (where deliverable was already true), and
  // never for a removed account. Uses a DEDICATED toast slot, separate from the error `notice`, so the two
  // never clobber each other.
  const deliverableSeenRef = useRef<Record<number, boolean>>({});
  const [readyToast, setReadyToast] = useState<string | null>(null);
  useEffect(() => {
    const next = { ...deliverableSeenRef.current };
    const ready: BrokerAccount[] = [];
    for (const a of (accounts || [])) {
      if (a.is_removed) continue;                 // never toast for a removed account
      const d = deliveries[a.id];
      if (d == null) continue;                    // delivery not yet known — don't record/compare (unknown != false)
      const now = Boolean(d.deliverable);
      if (now && next[a.id] === false) ready.push(a);   // strict KNOWN-false -> true edge (first-load is undefined)
      next[a.id] = now;                           // carry the known state forward
    }
    deliverableSeenRef.current = next;
    if (ready.length === 1) {
      setReadyToast(`Your ${brokerLabel(ready[0])} trading terminal is ready. Open MT5 to log in.`);
    } else if (ready.length > 1) {
      setReadyToast(`${ready.length} of your trading terminals are ready. Open MT5 to log in.`);
    }
  }, [accounts, deliveries]);

  const isConcurrent = entitlement?.account_mode === "concurrent";
  // Phase 9 — the STANDARD "the other account stops trading" confirm must gate on whether the backend
  // will ACTUALLY perform the switch (per-user enforcement armed), not on account_mode alone. When the
  // backend is not enforcing, Start is a plain flip (the other account keeps trading), so showing the
  // switch promise would be a lie. Frontend and backend agree via this authoritative field.
  const switchEnforced = Boolean(entitlement?.switch_enforced);

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
    if (nextActive && switchEnforced && !isConcurrent && active && active.id !== account.id) {
      setSwitchError("");
      setSwitchTarget(account);
      return;
    }
    void doSetActive(account, nextActive);
  }, [currentActive, switchEnforced, isConcurrent, doSetActive]);

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
  // The PRIMARY capacity indicator is the broker-account SLOT usage (owned_count / owned_limit) — the entitlement
  // the member is actually spending, and the number that changes when they add/remove an account. The trading
  // count is shown SEPARATELY (never as active_count / owned_limit, which mixes two different metrics and misleads).
  const entitlementLine = entitlement
    ? (isConcurrent
        ? `${entitlement.owned_count} / ${entitlement.owned_limit} broker accounts`
          + (activeCount ? ` - ${activeCount} trading` : "")
        : "Only one account can trade at a time")
    : "Connect and validate the broker accounts your strategies trade on.";

  // Removed/decommissioned accounts are hidden from the active Broker Accounts view (history retained server-side).
  const visibleAccounts = accounts ? accounts.filter((a) => !a.is_removed) : null;

  return (
    <div style={{ maxWidth: 960, margin: "0 auto", padding: "1.5rem 1rem" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 12, marginBottom: 18 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: "1.5rem", color: "#e9f4ff" }}>Broker accounts</h1>
          <p style={{ margin: "4px 0 0", color: "#8fa0b7", fontSize: "0.9rem" }} data-testid="entitlement-line">
            {entitlementLine}
          </p>
        </div>
        {visibleAccounts && visibleAccounts.length > 0 && <Button onClick={() => setWizardOpen(true)}>Add account</Button>}
      </div>

      {notice && (
        <div style={{ marginBottom: 14 }}>
          <Alert type={notice.type}>{notice.message}</Alert>
        </div>
      )}

      {/* PR B — dedicated "terminal ready" toast slot (separate from the error `notice` so the two never
          clobber each other); dismissible, one-shot per genuine provisioning completion. */}
      {readyToast && (
        <div style={{ marginBottom: 14 }}>
          <Alert type="info">{readyToast}</Alert>
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
          : !visibleAccounts || visibleAccounts.length === 0
            ? <EmptyState action={<Button onClick={() => setWizardOpen(true)}>Add account</Button>} />
            : (
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))", gap: 14 }}>
                {visibleAccounts.map((a) => (
                  <AccountCard key={a.id} account={a} status={statuses[a.id]}
                    statusLoading={statusLoading && !(a.id in statuses)}
                    delivery={deliveries[a.id]} longRunning={longRunning}
                    onViewMt5={handleViewMt5} onSetActive={handleSetActive} busy={busyId === a.id} />
                ))}
              </div>
            )}

      {/* This is the hosted (persistent-workspace) member experience: the Add-account flow creates a hosted
          account (private MetaTrader runtime) — no password here; the customer logs in inside MT5. */}
      <BrokerAccountWizard open={wizardOpen} onClose={() => setWizardOpen(false)} onAdded={() => void load()} hosted />
      <SwitchActiveDialog open={switchTarget !== null} target={switchTarget} currentActive={currentActive()}
        busy={switchBusy} error={switchError} onConfirm={() => void confirmSwitch()}
        onClose={() => { if (!switchBusy) setSwitchTarget(null); }} />
    </div>
  );
}

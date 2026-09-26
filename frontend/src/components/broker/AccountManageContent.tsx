"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { Dialog } from "@/components/broker/Dialog";
import { ErrorState, LoadingState } from "@/components/broker/States";
import {
  getAccount, getBrokerStatus, getBrokerAccountsUxEnabled, getDeliveryState, getEntitlementSummary,
  openMt5Desktop, setAccountActive,
} from "@/lib/broker-api";
import { listAccountAssignments } from "@/lib/strategy-assignments-api";
import { accountTitle, brokerLabel, maskAccountNumber, toCustomerError } from "@/lib/broker-status";
import type { BrokerAccount, BrokerStatus, DeliveryStateResult, EntitlementSummary } from "@/types/broker";

/** WAYOND account-scoped strategy management — the ACCOUNT-EXPLICIT management page (`/accounts/<id>`).
 *
 * Replaces the old dead-end that redirected back to /accounts whenever the legacy build flag was OFF. It
 * is gated PER USER on the certified `broker_accounts_ux` capability (never the legacy global flag), and
 * every fetch is for exactly this account id (owner-scoped + IDOR-safe on the backend). It shows only
 * member-friendly facts + actions — never SID / Windows username / runtime path / workspace UUID /
 * endpoint / node / bridge. "Start / Stop trading" controls automated-trading eligibility only. */
export function AccountManageContent({ accountId }: { accountId: number }) {
  const router = useRouter();
  const [gate, setGate] = useState<"loading" | "new" | "legacy">("loading");
  const [account, setAccount] = useState<BrokerAccount | null>(null);
  const [status, setStatus] = useState<BrokerStatus | null>(null);
  const [delivery, setDelivery] = useState<DeliveryStateResult | null>(null);
  const [entitlement, setEntitlement] = useState<EntitlementSummary | null>(null);
  const [strategyCount, setStrategyCount] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<{ type: "error" | "info"; message: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmStart, setConfirmStart] = useState(false);

  // PER-USER gate (matches /accounts). Non-granted users have no business on this new surface → /accounts.
  useEffect(() => {
    let alive = true;
    getBrokerAccountsUxEnabled()
      .then((on) => { if (alive) setGate(on ? "new" : "legacy"); })
      .catch(() => { if (alive) setGate("legacy"); });
    return () => { alive = false; };
  }, []);
  useEffect(() => { if (gate === "legacy") router.replace("/accounts"); }, [gate, router]);

  const load = useCallback(async () => {
    setError("");
    try {
      const acc = await getAccount(accountId); // 404 (cross-user/unknown) → throws → ErrorState
      setAccount(acc);
      getBrokerStatus(accountId).then(setStatus).catch(() => setStatus(null));
      getDeliveryState(accountId).then(setDelivery).catch(() => setDelivery(null));
      getEntitlementSummary().then(setEntitlement).catch(() => setEntitlement(null));
      listAccountAssignments(accountId).then((a) => setStrategyCount(a.length)).catch(() => setStrategyCount(null));
    } catch (err) {
      setError(toCustomerError(err, "We couldn't load this account."));
    }
  }, [accountId]);

  useEffect(() => { if (gate === "new") void load(); }, [gate, load]);

  const isActive = status ? status.is_active : account?.is_active ?? false;
  const isHosted = account
    ? (account.mt5_instance === null || account.mt5_instance === undefined) && account.readiness_provider === "persistent_workspace"
    : false;
  const hostedOwned = delivery?.is_owner !== false;
  const hostedDeliverable = Boolean(delivery?.deliverable) && hostedOwned;
  const hostedConnected = Boolean(delivery?.connected);
  const standardEnforced = entitlement?.account_mode !== "concurrent" && Boolean(entitlement?.switch_enforced);

  const doSetActive = useCallback(async (next: boolean) => {
    setBusy(true); setNotice(null); setConfirmStart(false);
    try {
      await setAccountActive(accountId, next);
      await load();
      setNotice({ type: "info", message: next ? "Trading started for this account." : "Trading stopped for this account." });
    } catch (err) {
      setNotice({ type: "error", message: toCustomerError(err, "We couldn't update this account.") });
    } finally { setBusy(false); }
  }, [accountId, load]);

  // Start/Stop = automated-trading eligibility. STOP is always allowed. START on a STANDARD-enforced plan,
  // when another account is already trading, is a switch → confirm first (the other one stops).
  const onToggleTrading = useCallback(() => {
    if (isActive) { void doSetActive(false); return; }
    const otherActive = (entitlement?.active_count ?? 0) >= 1 && !isActive;
    if (standardEnforced && otherActive) { setConfirmStart(true); return; }
    void doSetActive(true);
  }, [isActive, standardEnforced, entitlement, doSetActive]);

  const handleViewMt5 = useCallback(async () => {
    setBusy(true); setNotice(null);
    try {
      const res = await openMt5Desktop(accountId);
      if (res.url) window.open(res.url, "_blank", "noopener,noreferrer");
      else setNotice({ type: "info", message: res.detail || "The terminal viewer isn't available for this account." });
    } catch (err) {
      setNotice({ type: "error", message: toCustomerError(err, "We couldn't open the MT5 terminal.") });
    } finally { setBusy(false); }
  }, [accountId]);

  if (gate !== "new") {
    return <div style={wrap}><LoadingState label="Loading your account…" /></div>;
  }
  if (error) return <div style={wrap}><ErrorState message={error} onRetry={() => void load()} /></div>;
  if (!account) return <div style={wrap}><LoadingState label="Loading your account…" /></div>;

  const broker = brokerLabel(account);
  const title = accountTitle(account);
  const server = account.server_name || "";
  const masked = account.masked_account_number || maskAccountNumber(account.account_number);
  const myfxbookHref = account.myfxbook_url && /^https?:\/\//i.test(account.myfxbook_url) ? account.myfxbook_url : null;
  const showMyfxbook = Boolean(account.myfxbook_enabled && myfxbookHref);

  // Terminal availability (member wording). Traditional accounts have no hosted signal → show connection.
  const terminalLabel = isHosted
    ? (hostedDeliverable ? "Available" : "Preparing…")
    : (isActive ? "Available" : "—");
  const connectionLabel = isHosted
    ? (hostedConnected ? "Connected" : hostedDeliverable ? "Login required" : "Preparing…")
    : (status?.is_active ? "Connected" : status?.disconnected_at ? "Disconnected" : "Login required");

  return (
    <div style={wrap}>
      <div style={{ marginBottom: 6 }}>
        <Link href="/accounts" style={{ color: "#93c5fd", fontSize: "0.85rem", textDecoration: "none" }}>← Broker accounts</Link>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10, marginBottom: 16 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: "1.5rem", color: "#e9f4ff" }}>{title}</h1>
          <div style={{ color: "#8fa0b7", fontSize: "0.9rem", marginTop: 4 }}>
            {broker}{server ? ` · ${server}` : ""} · {masked}
          </div>
        </div>
        <Badge color={account.is_demo ? "blue" : "yellow"}>{account.is_demo ? "Demo" : "Live"}</Badge>
      </div>

      {notice && <div style={{ marginBottom: 14 }}><Alert type={notice.type}>{notice.message}</Alert></div>}

      <div style={grid}>
        <Fact label="Broker connection" value={connectionLabel} />
        <Fact label="Terminal" value={terminalLabel} />
        <Fact label="Automated trading" value={isActive ? "Trading" : "Stopped"}
              badge={isActive ? "green" : "gray"} />
        <Fact label="Strategies" value={strategyCount === null ? "…" : `${strategyCount} assigned`} />
      </div>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 18 }}>
        {isHosted
          ? (hostedDeliverable
              ? <Link href={`/trading/terminal-access?account_id=${account.id}`} style={btnLink}>Open MT5</Link>
              : <Button disabled>Open MT5</Button>)
          : <Button onClick={() => void handleViewMt5()} disabled={busy}>View MT5</Button>}
        <Link href={`/accounts/${account.id}/strategies`} style={btnLink}>Manage strategies</Link>
        <Button variant="secondary" onClick={onToggleTrading} disabled={busy}>
          {isActive ? "Stop trading" : "Start trading"}
        </Button>
        {showMyfxbook && (
          <a href={myfxbookHref as string} target="_blank" rel="noopener noreferrer" style={btnLink}>View on Myfxbook ↗</a>
        )}
      </div>

      <Dialog open={confirmStart} onClose={() => { if (!busy) setConfirmStart(false); }} title="Switch trading account" busy={busy}>
        <Alert type="info">Only one account can trade at a time on your plan.</Alert>
        <p style={{ color: "#cbd5f5", fontSize: "0.9rem", lineHeight: 1.6, margin: "12px 0 0" }}>
          Starting trading on <strong>{title}</strong> will stop your other trading account. It stays
          connected — it just won&apos;t place new trades until you switch back.
        </p>
        <div style={{ marginTop: 16, display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button type="button" variant="secondary" onClick={() => setConfirmStart(false)} disabled={busy}>Cancel</Button>
          <Button type="button" onClick={() => void doSetActive(true)} disabled={busy}>{busy ? "Starting…" : "Start trading"}</Button>
        </div>
      </Dialog>
    </div>
  );
}

const wrap: React.CSSProperties = { maxWidth: 760, margin: "0 auto", padding: "1.5rem 1rem" };
const grid: React.CSSProperties = { display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(180px, 1fr))", gap: 12 };
const btnLink: React.CSSProperties = {
  background: "transparent", border: "1px solid rgba(147,197,253,0.4)", borderRadius: 8,
  color: "#93c5fd", fontSize: "0.9rem", padding: "8px 14px", cursor: "pointer", textDecoration: "none",
};

const Fact: React.FC<{ label: string; value: string; badge?: "green" | "gray" }> = ({ label, value, badge }) => (
  <div style={{ border: "1px solid rgba(255,255,255,0.09)", borderRadius: 12, padding: "0.8rem 0.9rem", background: "rgba(12,18,40,0.6)" }}>
    <div style={{ fontSize: "0.75rem", color: "#8fa0b7", textTransform: "uppercase", letterSpacing: 0.4 }}>{label}</div>
    <div style={{ marginTop: 6 }}>
      {badge ? <Badge color={badge}>{value}</Badge> : <span style={{ color: "#e9f4ff", fontSize: "0.98rem" }}>{value}</span>}
    </div>
  </div>
);

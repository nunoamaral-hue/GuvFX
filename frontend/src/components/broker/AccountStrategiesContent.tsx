"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { EmptyState, ErrorState, LoadingState } from "@/components/broker/States";
import { getAccount, getBrokerAccountsUxEnabled } from "@/lib/broker-api";
import {
  createAssignment, listAccountAssignments, listAssignableStrategies, removeAssignment,
  type AvailableStrategy, type StrategyAssignment,
} from "@/lib/strategy-assignments-api";
import { accountTitle, brokerLabel, maskAccountNumber, toCustomerError } from "@/lib/broker-status";
import type { BrokerAccount } from "@/types/broker";

/** WAYOND account-scoped strategy management — the ACCOUNT-EXPLICIT "Manage strategies" page
 * (`/accounts/<id>/strategies`). Lists this account's StrategyAssignments (GET assignments?account=<id>),
 * lets the member add a strategy they own to THIS account (a NEW assignment; sibling accounts untouched),
 * and remove one. One account may hold MANY strategies — no product count limit. Every write is
 * account-explicit and both-axis-ownership-checked on the backend. No infrastructure concepts are shown. */
export function AccountStrategiesContent({ accountId }: { accountId: number }) {
  const router = useRouter();
  const [gate, setGate] = useState<"loading" | "new" | "legacy">("loading");
  const [account, setAccount] = useState<BrokerAccount | null>(null);
  const [assignments, setAssignments] = useState<StrategyAssignment[] | null>(null);
  const [available, setAvailable] = useState<AvailableStrategy[]>([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<{ type: "error" | "info"; message: string } | null>(null);
  const [selected, setSelected] = useState<string>("");
  const [busy, setBusy] = useState(false);

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
      const [acc, asn, avail] = await Promise.all([
        getAccount(accountId),
        listAccountAssignments(accountId),
        listAssignableStrategies().catch(() => [] as AvailableStrategy[]),
      ]);
      setAccount(acc);
      setAssignments(asn);
      setAvailable(avail);
    } catch (err) {
      setError(toCustomerError(err, "We couldn't load this account's strategies."));
    }
  }, [accountId]);

  useEffect(() => { if (gate === "new") void load(); }, [gate, load]);

  // Strategies the member owns that are NOT already assigned to THIS account (so we never offer a duplicate
  // — the backend rejects a duplicate (strategy, account) pair).
  const assignable = useMemo(() => {
    const taken = new Set((assignments || []).map((a) => a.strategy));
    // Also hide any strategy whose FAMILY (template_slug) is ACTIVELY assigned to this account — so the
    // canonical marketplace strategy is never offered on an account that already runs a legacy copy of it
    // (double-run). Matches the backend family guard EXACTLY (active-only): an INACTIVE same-family row must
    // NOT hide the canonical, so the legacy→canonical transition stays possible through the picker.
    const takenFamilies = new Set(
      (assignments || []).filter((a) => a.is_active)
        .map((a) => a.strategy_family).filter((f): f is string => Boolean(f)));
    return available.filter((s) => !taken.has(s.id) && !(s.family && takenFamilies.has(s.family)));
  }, [assignments, available]);

  const onAdd = useCallback(async () => {
    const sid = parseInt(selected, 10);
    if (!Number.isSafeInteger(sid) || sid <= 0) return;
    setBusy(true); setNotice(null);
    try {
      await createAssignment({ strategy: sid, account: accountId });
      setSelected("");
      await load();
      setNotice({ type: "info", message: "Strategy added to this account." });
    } catch (err) {
      setNotice({ type: "error", message: toCustomerError(err, "We couldn't add that strategy.") });
    } finally { setBusy(false); }
  }, [selected, accountId, load]);

  const onRemove = useCallback(async (asn: StrategyAssignment) => {
    setBusy(true); setNotice(null);
    try {
      await removeAssignment(asn.id);
      await load();
      setNotice({ type: "info", message: "Strategy removed from this account." });
    } catch (err) {
      setNotice({ type: "error", message: toCustomerError(err, "We couldn't remove that strategy.") });
    } finally { setBusy(false); }
  }, [load]);

  if (gate !== "new") return <div style={wrap}><LoadingState label="Loading…" /></div>;
  if (error) return <div style={wrap}><ErrorState message={error} onRetry={() => void load()} /></div>;
  if (!account || assignments === null) return <div style={wrap}><LoadingState label="Loading strategies…" /></div>;

  const broker = brokerLabel(account);
  const title = accountTitle(account);
  const server = account.server_name || "";
  const masked = account.masked_account_number || maskAccountNumber(account.account_number);

  return (
    <div style={wrap}>
      <div style={{ marginBottom: 6 }}>
        <Link href={`/accounts/${account.id}`} style={{ color: "#93c5fd", fontSize: "0.85rem", textDecoration: "none" }}>← {title}</Link>
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 10, marginBottom: 8 }}>
        <h1 style={{ margin: 0, fontSize: "1.4rem", color: "#e9f4ff" }}>Strategies</h1>
        <Badge color={account.is_demo ? "blue" : "yellow"}>{account.is_demo ? "Demo" : "Live"}</Badge>
      </div>
      <div style={{ color: "#8fa0b7", fontSize: "0.9rem", marginBottom: 18 }}>
        {broker}{server ? ` · ${server}` : ""} · {masked}
      </div>

      {notice && <div style={{ marginBottom: 14 }}><Alert type={notice.type}>{notice.message}</Alert></div>}

      <h2 style={{ fontSize: "0.8rem", color: "#8fa0b7", textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 10px" }}>
        Assigned strategies
      </h2>
      {assignments.length === 0
        ? <EmptyState title="No strategies yet" body="Add a strategy below to start trading it on this account." />
        : (
          <div style={{ display: "grid", gap: 10 }} data-testid="assigned-strategies">
            {assignments.map((a) => (
              <div key={a.id} style={rowCard}>
                <div>
                  <div style={{ color: "#e9f4ff", fontSize: "1rem", fontWeight: 600 }}>{a.strategy_name || `Strategy ${a.strategy}`}</div>
                  <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center", marginTop: 6 }}>
                    <Badge color={a.is_active ? "green" : "gray"}>{a.is_active ? "Active" : "Inactive"}</Badge>
                    <span style={meta}>Stage: {a.stage}</span>
                    <span style={meta}>Size: {a.lot_per_leg ? `${a.lot_per_leg} lots/leg` : "default"}</span>
                  </div>
                </div>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <Link href={`/accounts/${account.id}/strategies/${a.id}`} style={btnLink}>Configure</Link>
                  <Button variant="secondary" onClick={() => void onRemove(a)} disabled={busy}>Remove</Button>
                </div>
              </div>
            ))}
          </div>
        )}

      <div style={{ marginTop: 22, borderTop: "1px solid rgba(255,255,255,0.08)", paddingTop: 16 }}>
        <h2 style={{ fontSize: "0.8rem", color: "#8fa0b7", textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 10px" }}>
          Add a strategy
        </h2>
        {assignable.length === 0
          ? <p style={meta}>
              {available.length === 0
                ? <>No strategies are available to add yet. <Link href="/strategies/marketplace" style={{ color: "#93c5fd" }}>Browse the marketplace</Link>.</>
                : "Every available strategy is already assigned to this account."}
            </p>
          : (
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
              <select value={selected} onChange={(e) => setSelected(e.target.value)} disabled={busy}
                      aria-label="Choose a strategy to add" style={select}>
                <option value="">Choose a strategy…</option>
                {assignable.map((s) => <option key={s.id} value={String(s.id)}>{s.name}</option>)}
              </select>
              <Button onClick={() => void onAdd()} disabled={busy || !selected}>+ Add strategy</Button>
            </div>
          )}
      </div>
    </div>
  );
}

const wrap: React.CSSProperties = { maxWidth: 760, margin: "0 auto", padding: "1.5rem 1rem" };
const meta: React.CSSProperties = { fontSize: "0.82rem", color: "#8fa0b7" };
const rowCard: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 12, padding: "0.9rem 1rem",
  background: "rgba(12,18,40,0.6)", display: "flex", justifyContent: "space-between",
  alignItems: "center", gap: 12, flexWrap: "wrap",
};
const btnLink: React.CSSProperties = {
  background: "transparent", border: "1px solid rgba(147,197,253,0.4)", borderRadius: 8,
  color: "#93c5fd", fontSize: "0.85rem", padding: "6px 12px", cursor: "pointer", textDecoration: "none",
};
const select: React.CSSProperties = {
  background: "rgba(12,18,40,0.9)", border: "1px solid rgba(255,255,255,0.15)", borderRadius: 8,
  color: "#e9f4ff", fontSize: "0.9rem", padding: "8px 10px", minWidth: 220,
};

"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { ErrorState, LoadingState } from "@/components/broker/States";
import { getAccount, getBrokerAccountsUxEnabled } from "@/lib/broker-api";
import {
  getAssignment, getLegSizing, setLegSizing,
  type LegSizing, type StrategyAssignment,
} from "@/lib/strategy-assignments-api";
import { accountTitle, brokerLabel, maskAccountNumber, toCustomerError } from "@/lib/broker-status";
import type { BrokerAccount } from "@/types/broker";

/** WAYOND account-scoped strategy management — the ASSIGNMENT-specific configuration page.
 *
 * This configures ONE StrategyAssignment's per-account TRADING settings (status + position sizing) — NOT
 * the global strategy definition. It answers "what risk/sizing is THIS strategy using on THIS broker
 * account?" by reading the authoritative per-assignment control (AssignmentLegSizing.lot_per_leg via the
 * leg-sizing endpoint), so Account 35's real 0.01 is shown, never a blank global "risk per trade" field.
 * Gated per user on broker_accounts_ux. No global-strategy edit is offered here. */
export function AssignmentConfigContent({ accountId, assignmentId }: { accountId: number; assignmentId: number }) {
  const router = useRouter();
  const [gate, setGate] = useState<"loading" | "new" | "legacy">("loading");
  const [account, setAccount] = useState<BrokerAccount | null>(null);
  const [assignment, setAssignment] = useState<StrategyAssignment | null>(null);
  const [sizing, setSizing] = useState<LegSizing | null>(null);
  const [lotInput, setLotInput] = useState<string>("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState<{ type: "error" | "info"; message: string } | null>(null);
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
      const [acc, asn, sz] = await Promise.all([
        getAccount(accountId),
        getAssignment(assignmentId),
        getLegSizing(assignmentId).catch(() => null),
      ]);
      // The assignment must belong to THIS account (defence in depth against a mismatched URL).
      if (asn.account !== accountId) { router.replace(`/accounts/${accountId}/strategies`); return; }
      setAccount(acc);
      setAssignment(asn);
      setSizing(sz);
      if (sz) setLotInput(sz.lot_per_leg);
    } catch (err) {
      setError(toCustomerError(err, "We couldn't load this strategy configuration."));
    }
  }, [accountId, assignmentId, router]);

  useEffect(() => { if (gate === "new") void load(); }, [gate, load]);

  const onSaveSizing = useCallback(async () => {
    setBusy(true); setNotice(null);
    try {
      const updated = await setLegSizing(assignmentId, lotInput.trim());
      setSizing(updated); setLotInput(updated.lot_per_leg);
      setNotice({ type: "info", message: "Position size saved for this strategy on this account." });
    } catch (err) {
      setNotice({ type: "error", message: toCustomerError(err, "We couldn't save that position size.") });
    } finally { setBusy(false); }
  }, [assignmentId, lotInput]);

  if (gate !== "new") return <div style={wrap}><LoadingState label="Loading…" /></div>;
  if (error) return <div style={wrap}><ErrorState message={error} onRetry={() => void load()} /></div>;
  if (!account || !assignment) return <div style={wrap}><LoadingState label="Loading configuration…" /></div>;

  const broker = brokerLabel(account);
  const server = account.server_name || "";
  const masked = account.masked_account_number || maskAccountNumber(account.account_number);
  const strategyName = assignment.strategy_name || `Strategy ${assignment.strategy}`;
  const dirty = sizing != null && lotInput.trim() !== sizing.lot_per_leg;

  return (
    <div style={wrap}>
      <div style={{ marginBottom: 6 }}>
        <Link href={`/accounts/${account.id}/strategies`} style={{ color: "#93c5fd", fontSize: "0.85rem", textDecoration: "none" }}>← Strategies · {accountTitle(account)}</Link>
      </div>
      <h1 style={{ margin: "0 0 4px", fontSize: "1.4rem", color: "#e9f4ff" }}>{strategyName}</h1>
      <div style={{ color: "#8fa0b7", fontSize: "0.9rem", marginBottom: 18 }}>
        {broker}{server ? ` · ${server}` : ""} · {masked}
      </div>

      {notice && <div style={{ marginBottom: 14 }}><Alert type={notice.type}>{notice.message}</Alert></div>}

      <h2 style={sectionH}>Trading configuration</h2>
      <div style={{ display: "grid", gap: 14 }}>
        <div style={fact}>
          <div style={factLabel}>Status</div>
          <div style={{ marginTop: 6 }}>
            <Badge color={assignment.is_active ? "green" : "gray"}>{assignment.is_active ? "Active" : "Stopped"}</Badge>
          </div>
          <div style={hint}>Start or stop trading for this account from the account page.</div>
        </div>

        <div style={fact}>
          <div style={factLabel}>Position size per trade leg</div>
          {sizing ? (
            <>
              <div style={{ display: "flex", gap: 10, alignItems: "center", marginTop: 8, flexWrap: "wrap" }}>
                <input
                  type="text" inputMode="decimal" value={lotInput}
                  onChange={(e) => setLotInput(e.target.value)} disabled={busy}
                  aria-label="Position size per trade leg (lots)" style={input}
                />
                <span style={{ color: "#8fa0b7", fontSize: "0.9rem" }}>lots</span>
                <Button onClick={() => void onSaveSizing()} disabled={busy || !dirty || !lotInput.trim()}>
                  {busy ? "Saving…" : "Save"}
                </Button>
              </div>
              <div style={hint}>{sizing.note}</div>
              <div style={hint}>
                Allowed range {sizing.min}–{sizing.max} lots (steps of {sizing.step}).
                {sizing.is_override ? "" : " Currently using the default size."}
              </div>
            </>
          ) : (
            <div style={hint}>Position sizing isn&apos;t available for this strategy.</div>
          )}
        </div>
      </div>
    </div>
  );
}

const wrap: React.CSSProperties = { maxWidth: 640, margin: "0 auto", padding: "1.5rem 1rem" };
const sectionH: React.CSSProperties = { fontSize: "0.8rem", color: "#8fa0b7", textTransform: "uppercase", letterSpacing: 0.5, margin: "0 0 12px" };
const fact: React.CSSProperties = { border: "1px solid rgba(255,255,255,0.09)", borderRadius: 12, padding: "1rem 1.1rem", background: "rgba(12,18,40,0.6)" };
const factLabel: React.CSSProperties = { color: "#e9f4ff", fontSize: "0.98rem", fontWeight: 600 };
const hint: React.CSSProperties = { color: "#8fa0b7", fontSize: "0.82rem", marginTop: 8, lineHeight: 1.5 };
const input: React.CSSProperties = {
  background: "rgba(12,18,40,0.9)", border: "1px solid rgba(255,255,255,0.15)", borderRadius: 8,
  color: "#e9f4ff", fontSize: "0.95rem", padding: "8px 10px", width: 120,
};

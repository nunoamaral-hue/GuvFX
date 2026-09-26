"use client";

import React from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/Badge";
import { StatusBadge } from "@/components/broker/StatusBadge";
import type { BrokerAccount, BrokerStatus } from "@/types/broker";
import {
  connectionView, lastValidatedLine, latestAttemptLine, maskAccountNumber, validationStatusView,
} from "@/lib/broker-status";

/** WP4.2 — one broker account summary. `status` (from broker/status) is optional: while it loads, or if
 * it is unavailable, the card still renders the account basics.
 *
 * Phase C4 (multi-account, DARK) — additive customer-visible affordances: a strategies-assigned badge,
 * a "View MT5" action (account-EXPLICIT), a "Manage strategies" link, a "View on Myfxbook" link, and an
 * Activate/Deactivate control. These render ONLY when their callbacks/data are supplied, so existing
 * single-account callers are unaffected. No infrastructure concepts (SID, slot, runtime, node) are shown. */
type Props = {
  account: BrokerAccount;
  status?: BrokerStatus | null;
  statusLoading?: boolean;
  /** When set, the "View MT5" action renders and calls this with the account id. */
  onViewMt5?: (accountId: number) => void;
  /** When set, an Activate/Deactivate control renders and calls this with (account, nextActive). */
  onSetActive?: (account: BrokerAccount, nextActive: boolean) => void;
  /** Disables this card's action buttons while a parent-owned action is in flight. */
  busy?: boolean;
};

const card: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 14, padding: "1.1rem 1.2rem",
  background: "rgba(12,18,40,0.6)",
};
const row: React.CSSProperties = { display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" };
const meta: React.CSSProperties = { fontSize: "0.8rem", color: "#8fa0b7" };
const linkStyle: React.CSSProperties = { color: "#93c5fd", fontSize: "0.85rem", textDecoration: "none" };
const actionBtn: React.CSSProperties = {
  background: "transparent", border: "1px solid rgba(147,197,253,0.4)", borderRadius: 8,
  color: "#93c5fd", fontSize: "0.82rem", padding: "4px 10px", cursor: "pointer",
};

export const AccountCard: React.FC<Props> = ({ account, status, statusLoading, onViewMt5, onSetActive, busy }) => {
  const broker = account.broker_display_name || account.broker_name || "Broker";
  const server = account.server_name || "";
  // Prefer the server-computed mask; fall back to the local masker so the raw number is never shown.
  const masked = account.masked_account_number || maskAccountNumber(account.account_number);
  const validation = validationStatusView(status?.validation_status);
  const isActive = status ? status.is_active : account.is_active;
  const connection = connectionView(isActive, status?.disconnected_at);
  const strategyCount = account.active_strategy_count ?? 0;
  // Defence-in-depth: only ever render an http(s) Myfxbook link (the backend URLField already restricts
  // schemes, but never render a non-http scheme into an href on the client either).
  const myfxbookHref = account.myfxbook_url && /^https?:\/\//i.test(account.myfxbook_url)
    ? account.myfxbook_url : null;
  const showMyfxbook = Boolean(account.myfxbook_enabled && myfxbookHref);

  return (
    <div style={card}>
      <div style={{ ...row, justifyContent: "space-between", marginBottom: 8 }}>
        <div>
          <div style={{ color: "#e9f4ff", fontSize: "1.02rem", fontWeight: 600 }}>{account.name || broker}</div>
          <div style={meta}>{broker}{server ? ` · ${server}` : ""} · {masked}</div>
        </div>
        <Badge color={account.is_demo ? "blue" : "yellow"}>{account.is_demo ? "Demo" : "Live"}</Badge>
      </div>

      {/* WS-G — two non-overlapping concepts (Broker connection, Trading account); the redundant
          broker-health badge (same latest-attempt signal as validation) is removed. */}
      <div style={{ ...row, marginBottom: 10 }}>
        {statusLoading
          ? <span style={meta} role="status">Checking status…</span>
          : <>
              <StatusBadge view={validation} title="Broker connection" />
              <StatusBadge view={connection} title="Trading account" />
            </>}
        {/* Phase C4 — assigned-strategies badge (N:M via StrategyAssignment). */}
        <Badge color="gray">{strategyCount === 1 ? "1 strategy" : `${strategyCount} strategies`}</Badge>
        {isActive && <Badge color="green">Trading</Badge>}
      </div>

      {/* WS-C — Current validation state (badge above), Last successful validation, and Latest attempt are
          three DISTINCT concepts; the card keeps them separate (never merged). */}
      <div style={meta}>{lastValidatedLine(status?.validation_status, status?.validated_at)}</div>
      <div style={{ ...row, justifyContent: "space-between", marginTop: 2 }}>
        <span style={meta}>{latestAttemptLine(status?.latest_attempt, status?.validation_status) || " "}</span>
        <Link href={`/accounts/${account.id}`} style={linkStyle}
              aria-label={`Manage ${account.name || broker}`}>Manage →</Link>
      </div>

      {/* Phase C4 — customer-visible per-account actions. Additive; each renders only when supported. */}
      {(onViewMt5 || onSetActive || showMyfxbook) && (
        <div style={{ ...row, marginTop: 12, paddingTop: 10, borderTop: "1px solid rgba(255,255,255,0.06)" }}>
          {onViewMt5 && (
            <button type="button" style={actionBtn} disabled={busy}
                    onClick={() => onViewMt5(account.id)}
                    aria-label={`View MT5 for ${account.name || broker}`}>View MT5</button>
          )}
          <Link href={`/accounts/${account.id}`} style={{ ...actionBtn, textDecoration: "none" }}
                aria-label={`Manage strategies for ${account.name || broker}`}>Manage strategies</Link>
          {showMyfxbook && (
            <a href={myfxbookHref as string} target="_blank" rel="noopener noreferrer"
               style={{ ...actionBtn, textDecoration: "none" }}
               aria-label={`View ${account.name || broker} on Myfxbook`}>View on Myfxbook ↗</a>
          )}
          {onSetActive && (
            <button type="button" style={{ ...actionBtn, marginLeft: "auto" }} disabled={busy}
                    onClick={() => onSetActive(account, !isActive)}
                    aria-label={`${isActive ? "Deactivate" : "Activate"} ${account.name || broker}`}>
              {isActive ? "Deactivate" : "Activate"}
            </button>
          )}
        </div>
      )}
    </div>
  );
};

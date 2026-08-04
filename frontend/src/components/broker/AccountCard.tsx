"use client";

import React from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/Badge";
import { StatusBadge } from "@/components/broker/StatusBadge";
import type { BrokerAccount, BrokerStatus } from "@/types/broker";
import {
  connectionView, formatWhen, healthStatusView, maskAccountNumber, validationStatusView,
} from "@/lib/broker-status";

/** WP4.2 — one broker account summary. `status` (from broker/status) is optional: while it loads, or if
 * it is unavailable, the card still renders the account basics. */
type Props = { account: BrokerAccount; status?: BrokerStatus | null; statusLoading?: boolean };

const card: React.CSSProperties = {
  border: "1px solid rgba(255,255,255,0.09)", borderRadius: 14, padding: "1.1rem 1.2rem",
  background: "rgba(12,18,40,0.6)",
};
const row: React.CSSProperties = { display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" };
const meta: React.CSSProperties = { fontSize: "0.8rem", color: "#8fa0b7" };

export const AccountCard: React.FC<Props> = ({ account, status, statusLoading }) => {
  const broker = account.broker_display_name || account.broker_name || "Broker";
  const server = account.server_name || "";
  const health = healthStatusView(status?.latest_attempt?.status);
  const validation = validationStatusView(status?.validation_status);
  const connection = connectionView(
    status ? status.is_active : account.is_active,
    status?.disconnected_at,
  );
  const validatedAt = formatWhen(status?.validated_at);

  return (
    <div style={card}>
      <div style={{ ...row, justifyContent: "space-between", marginBottom: 8 }}>
        <div>
          <div style={{ color: "#e9f4ff", fontSize: "1.02rem", fontWeight: 600 }}>{account.name || broker}</div>
          <div style={meta}>{broker}{server ? ` · ${server}` : ""} · {maskAccountNumber(account.account_number)}</div>
        </div>
        <Badge color={account.is_demo ? "blue" : "yellow"}>{account.is_demo ? "Demo" : "Live"}</Badge>
      </div>

      <div style={{ ...row, marginBottom: 10 }}>
        {statusLoading
          ? <span style={meta} role="status">Checking status…</span>
          : <>
              <StatusBadge view={validation} title="Validation status" />
              {status?.latest_attempt && <StatusBadge view={health} title="Broker health" />}
              <StatusBadge view={connection} title="Connection" />
            </>}
      </div>

      <div style={{ ...row, justifyContent: "space-between" }}>
        <span style={meta}>{validatedAt ? `Last validated ${validatedAt}` : "Never validated"}</span>
        <Link href={`/broker-accounts/${account.id}`}
              style={{ color: "#93c5fd", fontSize: "0.85rem", textDecoration: "none" }}
              aria-label={`Manage ${account.name || broker}`}>Manage →</Link>
      </div>
    </div>
  );
};

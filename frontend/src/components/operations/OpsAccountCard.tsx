"use client";

import React from "react";
import Link from "next/link";
import { maskAccountNumber } from "@/lib/broker-status";
import type { BrokerAccount } from "@/types/broker";

/** WP5.3 — a lightweight account card for the operations accounts list. Identity + a link to the account
 * overview; NO per-account summary fetch here (the overview loads it on the detail page) to avoid N calls.
 * Account number is masked; no secret ever rendered. */
const card: React.CSSProperties = {
  display: "block", border: "1px solid rgba(255,255,255,0.08)", borderRadius: 14, padding: "1rem 1.2rem",
  background: "rgba(10,15,35,0.5)", textDecoration: "none", color: "inherit",
};
const meta: React.CSSProperties = { color: "#8fa0b7", fontSize: "0.85rem", marginTop: 4 };

export const OpsAccountCard: React.FC<{ account: BrokerAccount }> = ({ account }) => {
  const broker = account.broker_display_name || account.broker_name || "Broker";
  return (
    <Link href={`/operations/accounts/${account.id}`} style={card}
          aria-label={`Operations overview for ${account.name || broker}`}>
      <div style={{ color: "#e9f4ff", fontSize: "1.05rem" }}>{account.name || broker}</div>
      <div style={meta}>
        {broker}{account.server_name ? ` · ${account.server_name}` : ""} · {maskAccountNumber(account.account_number)}
      </div>
      <div style={{ ...meta, marginTop: 8, color: "#93c5fd" }}>View operational timeline →</div>
    </Link>
  );
};

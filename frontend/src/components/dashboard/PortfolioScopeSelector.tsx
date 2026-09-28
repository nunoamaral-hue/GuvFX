"use client";

import React from "react";

/** Portfolio scope selector: "All Accounts (N)" + each owned, non-decommissioned broker account (real broker name
 * + masked number). Exposes NO internal identifiers (TradingAccount id is only the option value, never shown text;
 * no workspace id / windows user / runtime / port / magic). Changing scope drives the WHOLE dashboard via the
 * parent's `onChange`. */
export type ScopeAccount = {
  id: number;
  broker_name?: string | null;
  name?: string | null;
  account_number?: string | null;
  masked_account_number?: string | null;
  is_removed?: boolean;
};

function mask(a: ScopeAccount): string {
  if (a.masked_account_number) return a.masked_account_number;
  const n = (a.account_number || "").trim();
  return n.length >= 4 ? `••••${n.slice(-4)}` : "••••";
}

const sel: React.CSSProperties = {
  background: "rgba(12,18,40,0.85)", color: "#e9f4ff", border: "1px solid rgba(147,197,253,0.35)",
  borderRadius: 8, padding: "6px 10px", fontSize: "0.85rem", maxWidth: 260,
};

export const PortfolioScopeSelector: React.FC<{
  accounts: ScopeAccount[];
  scope: string;                       // "ALL" | String(account.id)
  onChange: (scope: string) => void;
  lang?: "en" | "ja";
}> = ({ accounts, scope, onChange, lang = "en" }) => {
  const visible = accounts.filter((a) => !a.is_removed);
  const allLabel = lang === "ja" ? `すべての口座 (${visible.length})` : `All Accounts (${visible.length})`;
  return (
    <select value={scope} onChange={(e) => onChange(e.target.value)} style={sel}
            aria-label="Portfolio account scope" data-testid="portfolio-scope">
      <option value="ALL">{allLabel}</option>
      {visible.map((a) => (
        <option key={a.id} value={String(a.id)}>
          {(a.broker_name || a.name || "Account")} {mask(a)}
        </option>
      ))}
    </select>
  );
};

export default PortfolioScopeSelector;

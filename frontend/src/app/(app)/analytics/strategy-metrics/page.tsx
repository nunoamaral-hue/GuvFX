"use client";

import { useCallback, useEffect, useState } from "react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { apiFetch } from "@/lib/api";
import type { TradingAccount } from "@/types/strategies";
import { useLang } from "@/components/AppShell";
import { LocalizedBetaSurface } from "@/components/i18n/LocalizedBetaSurface";

type StrategyMetric = {
  strategy_name: string;
  trades: number;
  net_pnl: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  assigned?: boolean;
  has_attributed_trades?: boolean;
};

type MetricsResponse = {
  account_id: number;
  account_number?: string;
  strategies: StrategyMetric[];
};

// Broker-facing account label — never the internal DB PK. e.g. "Hosted Workspace — 1302575".
function accountLabel(acc: TradingAccount): string {
  const num = (acc.account_number || "").trim();
  const name = (acc.name || "").trim();
  if (name && num) return `${name} — ${num}`;
  return num || name || "Trading account";
}

type UiState = "loading" | "no_accounts" | "no_strategies" | "unavailable" | "ready";

export default function StrategyMetricsPage() {
  const lang = useLang();
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [selectedAccountId, setSelectedAccountId] = useState<string>("");
  const [rows, setRows] = useState<StrategyMetric[]>([]);
  const [state, setState] = useState<UiState>("loading");

  // 1) Discover the authenticated customer's OWN accounts (server-scoped). Auto-select the only one.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const data = await apiFetch<TradingAccount[]>("/api/trading/accounts/", {});
        if (cancelled) return;
        const list = Array.isArray(data) ? data : [];
        setAccounts(list);
        if (list.length === 0) {
          setState("no_accounts");
        } else {
          setSelectedAccountId(String(list[0].id));
        }
      } catch {
        if (!cancelled) setState("unavailable");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // 2) Load metrics for the selected owned account.
  const load = useCallback(async (accountId: string) => {
    if (!accountId) return;
    setState("loading");
    try {
      const data = await apiFetch<MetricsResponse>(
        `/api/analytics/strategy-metrics/?account=${encodeURIComponent(accountId)}`,
        {},
      );
      const list = Array.isArray(data?.strategies) ? data.strategies : [];
      setRows(list);
      setState(list.length === 0 ? "no_strategies" : "ready");
    } catch {
      // Never surface a raw HTTP status to the customer.
      setRows([]);
      setState("unavailable");
    }
  }, []);

  useEffect(() => {
    // Fetch-on-selection with a loading state is intentional; `load` guards an empty id and cancels safely.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    load(selectedAccountId);
  }, [selectedAccountId, load]);

  const selected = accounts.find((a) => String(a.id) === selectedAccountId);

  return (
    <LocalizedBetaSurface lang={lang}>
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>Strategy Metrics</h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          Performance by strategy for your connected trading account. Read-only and informational.
        </p>

        {/* Account selector — broker account number, never an internal ID. Auto-selected when you have one. */}
        {accounts.length > 0 && (
          <Card title="Account">
          <div style={{ display: "flex", gap: "0.75rem", alignItems: "center", padding: "0.75rem" }}>
            <label style={{ fontWeight: 600 }}>Trading account</label>
            {accounts.length === 1 ? (
              <span style={{ color: "#e5f4ff" }}>{accountLabel(accounts[0])}</span>
            ) : (
              <select
                value={selectedAccountId}
                onChange={(e) => setSelectedAccountId(e.target.value)}
                style={{
                  padding: "0.5rem 0.75rem",
                  borderRadius: 6,
                  border: "1px solid rgba(148,163,184,0.5)",
                  background: "rgba(3,7,18,0.9)",
                  color: "#e5f4ff",
                  fontSize: "0.9rem",
                  minWidth: 240,
                }}
              >
                {accounts.map((acc) => (
                  <option key={acc.id} value={acc.id}>
                    {accountLabel(acc)}
                  </option>
                ))}
              </select>
            )}
            <Button
              type="button"
              variant="secondary"
              onClick={() => selectedAccountId && load(selectedAccountId)}
              disabled={state === "loading"}
            >
              {state === "loading" ? "Loading…" : "Refresh"}
            </Button>
          </div>
          </Card>
        )}

        <Card title={selected ? `Strategies · ${accountLabel(selected)}` : "Strategies"}>
          <div style={{ overflowX: "auto" }}>
          {state === "loading" && (
            <p style={{ padding: "1rem", opacity: 0.75 }}>Loading your strategy performance…</p>
          )}

          {state === "no_accounts" && (
            <p style={{ padding: "1rem", opacity: 0.85 }}>
              No connected trading account yet. Once your hosted workspace is set up, your strategies and
              performance appear here.
            </p>
          )}

          {state === "unavailable" && (
            <p style={{ padding: "1rem", opacity: 0.85 }}>
              Strategy metrics are temporarily unavailable. Please try again shortly.
            </p>
          )}

          {state === "no_strategies" && (
            <p style={{ padding: "1rem", opacity: 0.85 }}>
              No strategies are assigned to this account yet. Enable a strategy from the Marketplace to see its
              performance here.
            </p>
          )}

          {state === "ready" && (
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  {["Strategy", "Trades", "Net PnL", "Observed Hit Rate"].map((h) => (
                    <th key={h} style={{ textAlign: "left", padding: "0.5rem", borderBottom: "1px solid rgba(255,255,255,0.1)" }}>
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const emptyAssigned = r.assigned && !r.has_attributed_trades;
                  return (
                    <tr key={r.strategy_name}>
                      <td style={{ padding: "0.5rem" }}>
                        {r.strategy_name}
                        {r.assigned && (
                          <span style={{ marginLeft: 8, fontSize: "0.7rem", color: "#7dd3fc", border: "1px solid rgba(125,211,252,0.4)", borderRadius: 4, padding: "1px 6px" }}>
                            Enabled
                          </span>
                        )}
                      </td>
                      {emptyAssigned ? (
                        <td colSpan={3} style={{ padding: "0.5rem", opacity: 0.7 }}>No attributed trades yet</td>
                      ) : (
                        <>
                          <td style={{ padding: "0.5rem" }}>{r.trades}</td>
                          <td style={{ padding: "0.5rem", color: r.net_pnl >= 0 ? "#22c55e" : "#f87171" }}>
                            {r.net_pnl >= 0 ? "+" : ""}{r.net_pnl.toFixed(2)}
                          </td>
                          <td style={{ padding: "0.5rem" }}>{r.win_rate_pct}%</td>
                        </>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
          </div>
        </Card>
      </div>
    </LocalizedBetaSurface>
  );
}

"use client";

import { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { useLang } from "@/components/AppShell";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { apiFetch } from "@/lib/api";
import { t } from "@/lib/i18n";
import type { TradingAccount, StrategyAssignment } from "@/types/strategies";

// =============================================================================
// Types
// =============================================================================

type Strategy = {
  id: number;
  name: string;
  is_active: boolean;
};

type DemoTradeResponse = {
  ok: boolean;
  job_id?: number;
  status?: string;
  message?: string;
  error?: string;
  daily_trades?: {
    used: number;
    limit: number;
  };
};

// =============================================================================
// Component
// =============================================================================

export default function LiveTradingPage() {
  const lang = useLang();
  const router = useRouter();

  // Accounts
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [loadingAccounts, setLoadingAccounts] = useState(false);

  // Strategies and assignments (for read-only display)
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [assignments, setAssignments] = useState<StrategyAssignment[]>([]);
  const [loadingStrategies, setLoadingStrategies] = useState(false);

  // Fetch accounts
  useEffect(() => {
    const fetchAccounts = async () => {
      setLoadingAccounts(true);
      try {
        const data = await apiFetch<TradingAccount[]>("/api/trading/accounts/", {});
        setAccounts(data);
      } catch (err) {
        console.error("Failed to fetch accounts:", err);
        setAccounts([]);
      } finally {
        setLoadingAccounts(false);
      }
    };
    fetchAccounts();
  }, []);

  // Fetch strategies and assignments
  useEffect(() => {
    const fetchStrategiesAndAssignments = async () => {
      setLoadingStrategies(true);
      try {
        const [strats, assigns] = await Promise.all([
          apiFetch<Strategy[]>("/api/strategies/strategies/", {}),
          apiFetch<StrategyAssignment[]>("/api/strategies/assignments/", {}),
        ]);
        setStrategies(strats);
        setAssignments(assigns);
      } catch (err) {
        console.error("Failed to fetch strategies/assignments:", err);
        setStrategies([]);
        setAssignments([]);
      } finally {
        setLoadingStrategies(false);
      }
    };
    fetchStrategiesAndAssignments();
  }, []);

  // Build a lookup: strategyId -> strategy
  const strategyLookup = new Map<number, Strategy>();
  for (const s of strategies) {
    strategyLookup.set(s.id, s);
  }

  // Build a lookup: accountId -> assignments[]
  const assignmentsByAccount = new Map<number, StrategyAssignment[]>();
  for (const a of assignments) {
    const list = assignmentsByAccount.get(a.account) || [];
    list.push(a);
    assignmentsByAccount.set(a.account, list);
  }

  // Demo trade state
  const [demoTradeLoading, setDemoTradeLoading] = useState<string | null>(null); // "accountId-strategyId" or null
  const [demoTradeMessage, setDemoTradeMessage] = useState<{
    type: "success" | "error";
    text: string;
  } | null>(null);

  // Handle demo trade execution
  const handleDemoTrade = useCallback(
    async (accountId: number, strategyId: number) => {
      const key = `${accountId}-${strategyId}`;
      setDemoTradeLoading(key);
      setDemoTradeMessage(null);

      try {
        const response = await apiFetch<DemoTradeResponse>(
          "/api/execution/demo-trade/",
          {
            method: "POST",
            body: JSON.stringify({
              account_id: accountId,
              strategy_id: strategyId,
              symbol: "EURUSD",
              side: "BUY",
            }),
          }
        );

        if (response.ok) {
          setDemoTradeMessage({
            type: "success",
            text: `Demo trade created (Job #${response.job_id}). ${response.daily_trades ? `${response.daily_trades.used}/${response.daily_trades.limit} daily trades used.` : ""}`,
          });
        } else {
          setDemoTradeMessage({
            type: "error",
            text: response.message || response.error || "Failed to create demo trade",
          });
        }
      } catch (err) {
        const errorMessage = err instanceof Error ? err.message : "Failed to create demo trade";
        setDemoTradeMessage({
          type: "error",
          text: errorMessage,
        });
      } finally {
        setDemoTradeLoading(null);
      }
    },
    []
  );

  return (
    <div style={{ maxWidth: 1000, margin: "0 auto" }}>
      {/* Header */}
      <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem", color: "#f0f6ff" }}>
        {t(lang, "liveTrading.title")}
      </h1>
      <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
        {t(lang, "liveTrading.subtitle")}
      </p>
      <p style={{ fontSize: "0.75rem", color: "#64748b", marginBottom: "0.35rem" }}>
        {t(lang, "legal.microDisclaimer")}
      </p>
      <p
        style={{
          fontSize: "0.72rem",
          color: "#64748b",
          marginBottom: "1rem",
          lineHeight: 1.5,
        }}
      >
        {t(lang, "liveTrading.disclaimerLine1")}
      </p>

      {/* Demo Trade Message Toast */}
      {demoTradeMessage && (
        <div
          style={{
            marginBottom: "1rem",
            padding: "0.75rem 1rem",
            background:
              demoTradeMessage.type === "success"
                ? "rgba(74, 222, 128, 0.1)"
                : "rgba(239, 68, 68, 0.1)",
            border: `1px solid ${demoTradeMessage.type === "success" ? "rgba(74, 222, 128, 0.3)" : "rgba(239, 68, 68, 0.3)"}`,
            borderRadius: 8,
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
          }}
        >
          <span
            style={{
              fontSize: "0.85rem",
              color: demoTradeMessage.type === "success" ? "#4ade80" : "#f87171",
            }}
          >
            {demoTradeMessage.type === "success" ? "✓" : "✕"} {demoTradeMessage.text}
          </span>
          <button
            onClick={() => setDemoTradeMessage(null)}
            style={{
              background: "none",
              border: "none",
              color: "#9ca3af",
              cursor: "pointer",
              padding: "0.25rem",
            }}
          >
            ✕
          </button>
        </div>
      )}

      {/* Execution Info Banner */}
      <div
        style={{
          marginBottom: "1.5rem",
          padding: "1rem 1.25rem",
          background: "rgba(59, 130, 246, 0.08)",
          border: "1px solid rgba(59, 130, 246, 0.3)",
          borderRadius: 8,
          display: "flex",
          alignItems: "flex-start",
          gap: "0.75rem",
        }}
      >
        <span style={{ fontSize: "1.5rem", color: "#3b82f6", lineHeight: 1 }}>ℹ️</span>
        <div>
          <h3
            style={{
              margin: "0 0 0.35rem",
              fontSize: "1rem",
              fontWeight: 600,
              color: "#93c5fd",
            }}
          >
            Demo Trading Available
          </h3>
          <p
            style={{
              margin: 0,
              fontSize: "0.85rem",
              color: "#60a5fa",
              lineHeight: 1.5,
            }}
          >
            Demo accounts with active strategy assignments can run test trades.
            Limited to EURUSD, 0.01 lots, max 3 trades per day.
            This feature is for demonstration purposes only.
          </p>
        </div>
      </div>

      {/* Linked Accounts Section */}
      <Card
        title={t(lang, "liveTrading.accountsTitle")}
        subtitle={t(lang, "liveTrading.accountsSubtitle")}
      >
        {loadingAccounts && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>
            {t(lang, "dashboard.loadingAccounts")}
          </p>
        )}

        {!loadingAccounts && accounts.length === 0 && (
          <div style={{ textAlign: "center", padding: "1.5rem 0" }}>
            <p style={{ fontSize: "0.9rem", color: "#9ca3af", marginBottom: "0.5rem" }}>
              {t(lang, "liveTrading.accountsEmpty")}
            </p>
            <Button
              variant="secondary"
              onClick={() => router.push("/accounts")}
              style={{ marginTop: "0.5rem" }}
            >
              {t(lang, "liveTrading.ctaLinkAccount")}
            </Button>
          </div>
        )}

        {!loadingAccounts && accounts.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
            {accounts.map((acc) => {
              const accAssignments = assignmentsByAccount.get(acc.id) || [];
              return (
                <div
                  key={acc.id}
                  style={{
                    padding: "0.75rem 1rem",
                    borderRadius: 8,
                    backgroundColor: "rgba(15,23,42,0.5)",
                    border: "1px solid rgba(148,163,184,0.2)",
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      marginBottom: "0.4rem",
                    }}
                  >
                    <div>
                      <span
                        style={{
                          fontWeight: 500,
                          color: "#f0f6ff",
                          fontSize: "0.95rem",
                        }}
                      >
                        {acc.name}
                      </span>
                      {acc.is_demo && (
                        <span style={{ marginLeft: "0.5rem" }}>
                          <Badge color="blue">Demo</Badge>
                        </span>
                      )}
                    </div>
                    <Badge color={acc.is_active ? "green" : "gray"}>
                      {acc.is_active
                        ? t(lang, "dashboard.active")
                        : t(lang, "dashboard.inactive")}
                    </Badge>
                  </div>
                  <div
                    style={{
                      fontSize: "0.8rem",
                      color: "#9db0c9",
                      display: "flex",
                      gap: "1.25rem",
                      flexWrap: "wrap",
                    }}
                  >
                    <span>
                      <span style={{ color: "#7c8ca4" }}>
                        {t(lang, "accounts.brokerServerLabel")}
                      </span>{" "}
                      {acc.broker_name}
                    </span>
                    <span>
                      <span style={{ color: "#7c8ca4" }}>
                        {t(lang, "accounts.accountNumberLabel")}
                      </span>{" "}
                      {acc.account_number}
                    </span>
                  </div>
                  {/* Assigned strategies with demo trade buttons */}
                  {accAssignments.length > 0 && (
                    <div style={{ marginTop: "0.5rem" }}>
                      <span style={{ fontSize: "0.75rem", color: "#7c8ca4" }}>
                        {t(lang, "liveTrading.assignedStrategies")}:
                      </span>
                      <div
                        style={{
                          display: "flex",
                          flexDirection: "column",
                          gap: "0.4rem",
                          marginTop: "0.25rem",
                        }}
                      >
                        {accAssignments.map((asn) => {
                          const strat = strategyLookup.get(asn.strategy);
                          const canRunDemoTrade =
                            acc.is_demo && acc.is_active && asn.is_active;
                          const loadingKey = `${acc.id}-${asn.strategy}`;
                          const isLoading = demoTradeLoading === loadingKey;

                          return (
                            <div
                              key={asn.id}
                              style={{
                                display: "flex",
                                alignItems: "center",
                                justifyContent: "space-between",
                                gap: "0.5rem",
                              }}
                            >
                              <span
                                style={{
                                  fontSize: "0.75rem",
                                  padding: "0.15rem 0.5rem",
                                  borderRadius: 4,
                                  backgroundColor: asn.is_active
                                    ? "rgba(74, 222, 128, 0.15)"
                                    : "rgba(148,163,184,0.15)",
                                  color: asn.is_active ? "#4ade80" : "#9ca3af",
                                }}
                              >
                                {strat?.name || `Strategy #${asn.strategy}`}
                                {!asn.is_active && " (paused)"}
                              </span>

                              {/* Demo Trade Button - only for demo accounts with active assignments */}
                              {canRunDemoTrade && (
                                <button
                                  onClick={() =>
                                    handleDemoTrade(acc.id, asn.strategy)
                                  }
                                  disabled={isLoading}
                                  style={{
                                    fontSize: "0.7rem",
                                    padding: "0.2rem 0.5rem",
                                    borderRadius: 4,
                                    border: "1px solid rgba(59, 130, 246, 0.5)",
                                    background: isLoading
                                      ? "rgba(59, 130, 246, 0.2)"
                                      : "rgba(59, 130, 246, 0.1)",
                                    color: "#60a5fa",
                                    cursor: isLoading ? "wait" : "pointer",
                                    opacity: isLoading ? 0.7 : 1,
                                  }}
                                >
                                  {isLoading ? "Running..." : "Run Demo Trade"}
                                </button>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Card>

      {/* Strategies Section (read-only) */}
      <Card
        title={t(lang, "liveTrading.strategiesTitle")}
        subtitle={t(lang, "liveTrading.strategiesSubtitle")}
      >
        {loadingStrategies && (
          <p style={{ fontSize: "0.9rem", color: "#9ca3af" }}>Loading…</p>
        )}

        {!loadingStrategies && strategies.length === 0 && (
          <div style={{ textAlign: "center", padding: "1.5rem 0" }}>
            <p style={{ fontSize: "0.9rem", color: "#9ca3af", marginBottom: "0.5rem" }}>
              {t(lang, "liveTrading.strategiesEmpty")}
            </p>
            <Button
              variant="secondary"
              onClick={() => router.push("/strategies/create")}
              style={{ marginTop: "0.5rem" }}
            >
              {t(lang, "liveTrading.ctaCreateStrategy")}
            </Button>
          </div>
        )}

        {!loadingStrategies && strategies.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: "0.5rem" }}>
            {strategies.map((strat) => {
              // Check if strategy is assigned to any account
              const isAssigned = assignments.some((a) => a.strategy === strat.id);
              return (
                <div
                  key={strat.id}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    padding: "0.6rem 0.85rem",
                    borderRadius: 6,
                    backgroundColor: "rgba(15,23,42,0.4)",
                    border: "1px solid rgba(148,163,184,0.15)",
                  }}
                >
                  <span style={{ color: "#f0f6ff", fontSize: "0.9rem" }}>
                    {strat.name}
                  </span>
                  <span
                    style={{
                      fontSize: "0.75rem",
                      color: isAssigned ? "#4ade80" : "#9ca3af",
                    }}
                  >
                    {isAssigned
                      ? t(lang, "liveTrading.assigned")
                      : t(lang, "liveTrading.notAssigned")}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </Card>

      {/* Next Steps */}
      <Card title={t(lang, "liveTrading.nextStepsTitle")}>
        <p
          style={{
            fontSize: "0.82rem",
            color: "#9ca3af",
            marginBottom: "1rem",
          }}
        >
          {t(lang, "liveTrading.nextStepsBody")}
        </p>
        <div
          style={{
            display: "flex",
            gap: "0.75rem",
            flexWrap: "wrap",
          }}
        >
          <Button variant="secondary" onClick={() => router.push("/accounts")}>
            {t(lang, "liveTrading.ctaLinkAccount")}
          </Button>
          <Button variant="secondary" onClick={() => router.push("/strategies/create")}>
            {t(lang, "liveTrading.ctaCreateStrategy")}
          </Button>
          <Button
            variant="secondary"
            onClick={() => router.push("/backtests?create=true")}
          >
            {t(lang, "liveTrading.ctaCreateTest")}
          </Button>
          <Button variant="secondary" onClick={() => router.push("/backtests")}>
            {t(lang, "liveTrading.ctaViewBacktests")}
          </Button>
        </div>
      </Card>
    </div>
  );
}

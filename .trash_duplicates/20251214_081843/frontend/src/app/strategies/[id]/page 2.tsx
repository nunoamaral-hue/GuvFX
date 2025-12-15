"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Badge } from "@/components/ui/Badge";
import { Alert } from "@/components/ui/Alert";
import { AppShell } from "@/components/AppShell";

type Strategy = {
  id: number;
  name: string;
  description: string;
  style: string | null;
  symbol_universe: string;
  timeframe: string;
  risk_per_trade_pct: string | null;
  max_drawdown_pct: string | null;
  magic_number: number | null;
  is_active: boolean;
  entry_logic: string;
  exit_logic: string;
  notes: string;
  // NEW fields:
  ma_fast_period: number | null;
  ma_slow_period: number | null;
  ma_type: string | null;
  auto_optimize_by_ai: boolean;
  created_at: string;
  updated_at: string;
};

type AISuggestions = {
  strategy_id: number;
  strategy_name: string;
  has_backtest_data: boolean;
  latest_run_id: number | null;
  latest_run_status: string | null;
  latest_run_created_at: string | null;
  performance_summary: {
    total_return_pct: number;
    max_drawdown_pct: number;
    win_rate_pct: number;
    num_trades: number;
  } | null;
  parameter_suggestions: Record<string, any> | null;
  risk_suggestions: Record<string, any> | null;
  notes: string[];
};

type TradingAccount = {
  id: number;
  name: string;
  broker_name: string;
  account_number: string;
  is_demo: boolean;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

type StrategyAssignment = {
  id: number;
  strategy: number;
  strategy_name: string;
  account: number;
  account_name: string;
  broker_name: string;
  is_enabled: boolean;
  created_at: string;
};

type StrategyChangeLog = {
  id: number;
  source: string;
  changed_by_email: string | null;
  created_at: string;
  before_settings: Record<string, any> | null;
  after_settings: Record<string, any> | null;
};

export default function StrategyDetailPage() {
  const params = useParams();
  const strategyId = Number(params?.id);

  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [accessToken, setAccessToken] = useState<string>("");
  const [aiSuggestions, setAiSuggestions] = useState<AISuggestions | null>(null);

  const [loadingStrategy, setLoadingStrategy] = useState(false);
  const [loadingAI, setLoadingAI] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // accounts + assignments
  const [accounts, setAccounts] = useState<TradingAccount[]>([]);
  const [assignments, setAssignments] = useState<StrategyAssignment[]>([]);
  const [loadingLinks, setLoadingLinks] = useState(false);
  const [linkMessage, setLinkMessage] = useState<string | null>(null);
  const [selectedAccountId, setSelectedAccountId] = useState<number | "">("");
  const [linking, setLinking] = useState(false);
  const [removingId, setRemovingId] = useState<number | null>(null);

  const [changeLogs, setChangeLogs] = useState<StrategyChangeLog[]>([]);
  const [loadingLogs, setLoadingLogs] = useState(false);

  // editable settings
  // Fetch change logs
  useEffect(() => {
    if (!accessToken || !strategyId) return;

    const fetchLogs = async () => {
      setLoadingLogs(true);
      try {
        const logs = await apiFetch<StrategyChangeLog[]>(
          `/api/strategies/changes/?strategy=${strategyId}`,
          {},
          accessToken
        );
        setChangeLogs(logs);
      } catch (err) {
        console.error("Failed to fetch change logs:", err);
        // history is nice-to-have; we keep this silent
      } finally {
        setLoadingLogs(false);
      }
    };

    fetchLogs();
  }, [accessToken, strategyId]);
  const [tfEdit, setTfEdit] = useState("");
  const [symbolsEdit, setSymbolsEdit] = useState("");
  const [maFastEdit, setMaFastEdit] = useState<string>("");
  const [maSlowEdit, setMaSlowEdit] = useState<string>("");
  const [maTypeEdit, setMaTypeEdit] = useState<string>("");
  const [autoAiEdit, setAutoAiEdit] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [settingsMessage, setSettingsMessage] = useState<string | null>(null);
  const [autoTuneLoading, setAutoTuneLoading] = useState(false);

  const labelStyle: React.CSSProperties = {
    color: "#8fa0b7",
    fontSize: "0.88rem",
    marginRight: 4,
  };

  const valueStyle: React.CSSProperties = {
    color: "#e9f4ff",
    fontSize: "0.9rem",
  };

  // Load token from localStorage
  useEffect(() => {
    if (typeof window !== "undefined") {
      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
      }
    }
  }, []);

  // Fetch strategy details
  useEffect(() => {
    if (!strategyId || !accessToken) return;

    const fetchStrategy = async () => {
      setLoadingStrategy(true);
      setError(null);
      try {
        const data = await apiFetch<Strategy>(
          `/api/strategies/strategies/${strategyId}/`,
          {},
          accessToken
        );
        setStrategy(data);

        // initialise edit fields
        setTfEdit(data.timeframe || "");
        setSymbolsEdit(data.symbol_universe || "");
        setMaFastEdit(
          typeof data.ma_fast_period === "number"
            ? String(data.ma_fast_period)
            : ""
        );
        setMaSlowEdit(
          typeof data.ma_slow_period === "number"
            ? String(data.ma_slow_period)
            : ""
        );
        setMaTypeEdit(data.ma_type || "");
        setAutoAiEdit(data.auto_optimize_by_ai);
      } catch (err: any) {
        console.error(err);
        setError(err.message ?? "Failed to load strategy.");
      } finally {
        setLoadingStrategy(false);
      }
    };

    fetchStrategy();
  }, [strategyId, accessToken]);

  // Fetch accounts + assignments
  useEffect(() => {
    if (!accessToken || !strategyId) return;

    const fetchLinks = async () => {
      setLoadingLinks(true);
      setLinkMessage(null);
      try {
        const [accs, assigns] = await Promise.all([
          apiFetch<TradingAccount[]>("/api/trading/accounts/", {}, accessToken),
          apiFetch<StrategyAssignment[]>(
            "/api/strategies/assignments/",
            {},
            accessToken
          ),
        ]);

        setAccounts(accs);
        const filtered = assigns.filter(
          (a) => a.strategy === strategyId && a.is_enabled
        );
        setAssignments(filtered);
      } catch (err: any) {
        console.error(err);
        setError((prev) => prev ?? "Failed to load linked accounts.");
      } finally {
        setLoadingLinks(false);
      }
    };

    fetchLinks();
  }, [accessToken, strategyId]);

  const handleGetAISuggestions = async () => {
    if (!accessToken) {
      setError("No token found. Please log in again.");
      return;
    }

    setLoadingAI(true);
    setError(null);
    try {
      // 1) Get AI suggestions as before
      const data = await apiFetch<AISuggestions>(
        `/api/ai/strategies/${strategyId}/suggest/`,
        { method: "POST" },
        accessToken
      );
      setAiSuggestions(data);

      // 2) If auto optimization is enabled, refetch the strategy so
      //    the settings form reflects any AI changes made on the backend.
      if (autoAiEdit) {
        const updated = await apiFetch<Strategy>(
          `/api/strategies/strategies/${strategyId}/`,
          {},
          accessToken
        );
        setStrategy(updated);

        // Sync editable fields with updated strategy
        setTfEdit(updated.timeframe || "");
        setSymbolsEdit(updated.symbol_universe || "");
        setMaFastEdit(
          typeof updated.ma_fast_period === "number"
            ? String(updated.ma_fast_period)
            : ""
        );
        setMaSlowEdit(
          typeof updated.ma_slow_period === "number"
            ? String(updated.ma_slow_period)
            : ""
        );
        setMaTypeEdit(updated.ma_type || "");
        setAutoAiEdit(updated.auto_optimize_by_ai);
      }
    } catch (err: any) {
      console.error(err);
      setError(err.message ?? "Failed to fetch AI suggestions.");
    } finally {
      setLoadingAI(false);
    }
  };

  const handleLinkAccount = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!accessToken) {
      setError("No token found. Please log in again.");
      return;
    }
    if (!selectedAccountId) {
      setLinkMessage("Please select an account to link.");
      return;
    }

    setLinking(true);
    setLinkMessage(null);
    try {
      const body = {
        strategy: strategyId,
        account: selectedAccountId,
        is_enabled: true,
      };

      await apiFetch<StrategyAssignment>(
        "/api/strategies/assignments/",
        {
          method: "POST",
          body: JSON.stringify(body),
        },
        accessToken
      );

      setLinkMessage("Account linked to strategy.");
      setSelectedAccountId("");

      // refresh assignments
      const assigns = await apiFetch<StrategyAssignment[]>(
        "/api/strategies/assignments/",
        {},
        accessToken
      );
      setAssignments(
        assigns.filter((a) => a.strategy === strategyId && a.is_enabled)
      );
    } catch (err: any) {
      console.error(err);
      setError(err.message ?? "Failed to link account.");
    } finally {
      setLinking(false);
    }
  };

  const handleRemoveLink = async (assignmentId: number) => {
    if (!accessToken) return;
    setRemovingId(assignmentId);
    setLinkMessage(null);

    try {
      await apiFetch<void>(
        `/api/strategies/assignments/${assignmentId}/`,
        { method: "DELETE" },
        accessToken
      );

      setAssignments((prev) => prev.filter((a) => a.id !== assignmentId));
      setLinkMessage("Link removed.");
    } catch (err: any) {
      console.error(err);
      setError(err.message ?? "Failed to remove link.");
    } finally {
      setRemovingId(null);
    }
  };

  const handleSaveSettings = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!accessToken) {
      setError("No token found. Please log in again.");
      return;
    }

    setSavingSettings(true);
    setSettingsMessage(null);

    try {
      const body: any = {
        timeframe: tfEdit,
        symbol_universe: symbolsEdit,
        auto_optimize_by_ai: autoAiEdit,
      };

      if (maFastEdit) body.ma_fast_period = Number(maFastEdit);
      else body.ma_fast_period = null;

      if (maSlowEdit) body.ma_slow_period = Number(maSlowEdit);
      else body.ma_slow_period = null;

      if (maTypeEdit) body.ma_type = maTypeEdit;
      else body.ma_type = "";

      const updated = await apiFetch<Strategy>(
        `/api/strategies/strategies/${strategyId}/`,
        {
          method: "PATCH",
          body: JSON.stringify(body),
        },
        accessToken
      );

      setStrategy(updated);
      setSettingsMessage("Strategy settings updated.");
    } catch (err: any) {
      console.error(err);
      setError(err.message ?? "Failed to update strategy settings.");
    } finally {
      setSavingSettings(false);
    }
  };

  const handleApplyAiRecommendations = () => {
    const rec =
      (aiSuggestions?.parameter_suggestions as any)?.recommended_settings || null;
    if (!rec) {
      setSettingsMessage("No AI recommended settings available to apply.");
      return;
    }

    if (typeof rec.timeframe === "string") {
      setTfEdit(rec.timeframe);
    }
    if (typeof rec.symbol_universe === "string") {
      setSymbolsEdit(rec.symbol_universe);
    }
    if (rec.ma_fast_period !== undefined && rec.ma_fast_period !== null) {
      setMaFastEdit(String(rec.ma_fast_period));
    }
    if (rec.ma_slow_period !== undefined && rec.ma_slow_period !== null) {
      setMaSlowEdit(String(rec.ma_slow_period));
    }
    if (typeof rec.ma_type === "string") {
      setMaTypeEdit(rec.ma_type);
    }

    setSettingsMessage("AI recommended settings applied to the form. Review and save.");
  };

  const handleAutoTune = async () => {
    if (!accessToken) {
      setError("No token found. Please log in again.");
      return;
    }

    setAutoTuneLoading(true);
    setError(null);
    setSettingsMessage(null);

    try {
      const data = await apiFetch<{
        applied_settings: {
          timeframe?: string;
          symbol_universe?: string;
          ma_fast_period?: number;
          ma_slow_period?: number;
          ma_type?: string;
        } | null;
      }>(
        `/api/strategies/strategies/${strategyId}/auto-tune/`,
        { method: "POST" },
        accessToken
      );

      // Always refresh the strategy from backend to reflect any changes
      const updated = await apiFetch<Strategy>(
        `/api/strategies/strategies/${strategyId}/`,
        {},
        accessToken
      );
      setStrategy(updated);

      // Sync editable fields with updated strategy
      setTfEdit(updated.timeframe || "");
      setSymbolsEdit(updated.symbol_universe || "");
      setMaFastEdit(
        typeof updated.ma_fast_period === "number"
          ? String(updated.ma_fast_period)
          : ""
      );
      setMaSlowEdit(
        typeof updated.ma_slow_period === "number"
          ? String(updated.ma_slow_period)
          : ""
      );
      setMaTypeEdit(updated.ma_type || "");
      setAutoAiEdit(updated.auto_optimize_by_ai);

      if (data.applied_settings) {
        setSettingsMessage(
          "Auto-tune complete. AI recommendations have been applied to this strategy."
        );
      } else {
        setSettingsMessage(
          "Auto-tune completed. No changes were applied (auto optimization may be disabled)."
        );
      }
    } catch (err: any) {
      console.error(err);
      setError(err.message ?? "Failed to auto-tune strategy.");
    } finally {
      setAutoTuneLoading(false);
    }
  };

  return (
    <AppShell>
      <div style={{ maxWidth: 900, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          Strategy Detail
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          Inspect your strategy, edit its parameters, link it to accounts, and
          request AI-powered suggestions.
        </p>

        {error && <Alert type="error">{error}</Alert>}
        {linkMessage && <Alert type="info">{linkMessage}</Alert>}
        {settingsMessage && <Alert type="info">{settingsMessage}</Alert>}

        {/* Strategy overview */}
        <Card title={strategy ? strategy.name : `Strategy #${strategyId}`}>
          {!accessToken && (
            <p style={{ fontStyle: "italic", fontSize: "0.9rem" }}>
              No token found. Please log in again.
            </p>
          )}

          {loadingStrategy && <p>Loading strategy...</p>}

          {strategy && (
            <div style={{ fontSize: "0.95rem" }}>
              {/* Header row: name + status badge */}
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  marginBottom: "0.7rem",
                }}
              >
                <h2
                  style={{
                    fontSize: "1.4rem",
                    margin: 0,
                    color: "#f1f5ff",
                  }}
                >
                  {strategy.name}
                </h2>
                <Badge color={strategy.is_active ? "green" : "gray"}>
                  {strategy.is_active ? "Active" : "Inactive"}
                </Badge>
              </div>

              {/* Description */}
              <p style={{ marginBottom: "0.6rem" }}>
                <span style={labelStyle}>Description:</span>
                <span style={valueStyle}>
                  {strategy.description || (
                    <span style={{ color: "#7c8ca4" }}>No description</span>
                  )}
                </span>
              </p>

              {/* Key info grid */}
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                  gap: "0.4rem 1.5rem",
                }}
              >
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Style:</span>
                  <span style={valueStyle}>{strategy.style || "—"}</span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Symbols:</span>
                  <span style={valueStyle}>
                    {strategy.symbol_universe || "—"}
                  </span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Timeframe:</span>
                  <span style={valueStyle}>{strategy.timeframe || "—"}</span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Risk per trade (%):</span>
                  <span style={valueStyle}>
                    {strategy.risk_per_trade_pct ?? "—"}
                  </span>
                </p>
                <p style={{ margin: 0 }}>
                  <span style={labelStyle}>Magic number:</span>
                  <span style={valueStyle}>
                    {strategy.magic_number ?? "—"}
                  </span>
                </p>
              </div>

              {/* Timestamps */}
              <p
                style={{
                  fontSize: "0.78rem",
                  color: "#7c8ca4",
                  marginTop: "0.6rem",
                }}
              >
                Created:{" "}
                <span style={{ color: "#c9def7" }}>
                  {new Date(strategy.created_at).toLocaleString()}
                </span>
              </p>
            </div>
          )}
        </Card>

        {/* Editable settings */}
        <Card
          title="Strategy Settings"
          subtitle="Manually adjust core parameters or allow AI to optimize them for you."
        >
          <form onSubmit={handleSaveSettings}>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
                gap: "0.75rem 1.5rem",
              }}
            >
              <div>
                <label
                  htmlFor="tf-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Timeframe
                </label>
                <input
                  id="tf-edit"
                  type="text"
                  value={tfEdit}
                  onChange={(e) => setTfEdit(e.target.value)}
                  placeholder="e.g. H4"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                />
              </div>

              <div>
                <label
                  htmlFor="symbols-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Symbols (comma-separated)
                </label>
                <input
                  id="symbols-edit"
                  type="text"
                  value={symbolsEdit}
                  onChange={(e) => setSymbolsEdit(e.target.value)}
                  placeholder="e.g. EURUSD,GBPUSD"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                />
              </div>

              <div>
                <label
                  htmlFor="ma-fast-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Fast MA period
                </label>
                <input
                  id="ma-fast-edit"
                  type="number"
                  min={1}
                  value={maFastEdit}
                  onChange={(e) => setMaFastEdit(e.target.value)}
                  placeholder="e.g. 20"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                />
              </div>

              <div>
                <label
                  htmlFor="ma-slow-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Slow MA period
                </label>
                <input
                  id="ma-slow-edit"
                  type="number"
                  min={1}
                  value={maSlowEdit}
                  onChange={(e) => setMaSlowEdit(e.target.value)}
                  placeholder="e.g. 50"
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                />
              </div>

              <div>
                <label
                  htmlFor="ma-type-edit"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Moving average type
                </label>
                <select
                  id="ma-type-edit"
                  value={maTypeEdit}
                  onChange={(e) => setMaTypeEdit(e.target.value)}
                  style={{
                    width: "100%",
                    padding: "0.6rem 0.8rem",
                    borderRadius: 8,
                    border: "1px solid rgba(148,163,184,0.65)",
                    background: "rgba(3, 7, 18, 0.9)",
                    color: "#e5f4ff",
                    fontSize: "0.9rem",
                    outline: "none",
                    boxSizing: "border-box",
                  }}
                  disabled={autoAiEdit}
                >
                  <option value="">Not specified</option>
                  <option value="SMA">Simple MA (SMA)</option>
                  <option value="EMA">Exponential MA (EMA)</option>
                  <option value="WMA">Weighted MA (WMA)</option>
                </select>
              </div>

              <div>
                <label
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  AI optimization
                </label>
                <label
                  style={{
                    fontSize: "0.85rem",
                    color: "#e5f4ff",
                    display: "inline-flex",
                    alignItems: "center",
                    gap: 8,
                    cursor: "pointer",
                  }}
                >
                  <input
                    type="checkbox"
                    checked={autoAiEdit}
                    onChange={(e) => setAutoAiEdit(e.target.checked)}
                    style={{ cursor: "pointer" }}
                  />
                  Let AI manage parameters automatically
                </label>
                <p
                  style={{
                    fontSize: "0.78rem",
                    color: "#7c8ca4",
                    marginTop: "0.25rem",
                  }}
                >
                  When enabled, GuvFX will allow AI to tune timeframe, symbols, and
                  moving average settings based on backtests.
                </p>
                {strategy?.auto_optimize_by_ai && strategy?.updated_at && (
                  <p
                    style={{
                      fontSize: "0.78rem",
                      color: "#9ca3af",
                      marginTop: "0.25rem",
                    }}
                  >
                    Last AI tune:{" "}
                    <span style={{ color: "#e5f4ff" }}>
                      {new Date(strategy.updated_at).toLocaleString()}
                    </span>
                  </p>
                )}
              </div>
            </div>

            <div
              style={{
                marginTop: "1rem",
                display: "flex",
                justifyContent: "flex-end",
                gap: "0.5rem",
                flexWrap: "wrap",
              }}
            >
              <Button
                type="button"
                variant="secondary"
                onClick={handleAutoTune}
                disabled={autoTuneLoading || !accessToken || !autoAiEdit}
              >
                {autoTuneLoading ? "Auto-tuning…" : "Auto-tune now"}
              </Button>
              <Button type="submit" disabled={savingSettings || !accessToken}>
                {savingSettings ? "Saving…" : "Save settings"}
              </Button>
            </div>
          </form>
        </Card>

        {/* Linked Accounts */}
        <Card
          title="Linked Accounts"
          subtitle="Choose which trading accounts this strategy should run on."
        >
          {loadingLinks && <p>Loading linked accounts…</p>}

          {!loadingLinks && assignments.length === 0 && (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              This strategy is not linked to any trading accounts yet.
            </p>
          )}

          {!loadingLinks && assignments.length > 0 && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.45rem",
                marginBottom: "0.8rem",
              }}
            >
              {assignments.map((a) => (
                <div
                  key={a.id}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    alignItems: "center",
                    borderRadius: 8,
                    border: "1px solid #222838",
                    padding: "0.4rem 0.6rem",
                    background: "rgba(7, 12, 30, 0.9)",
                    fontSize: "0.86rem",
                  }}
                >
                  <div>
                    <div style={{ color: "#e9f4ff" }}>
                      {a.account_name}{" "}
                      <span
                        style={{
                          fontSize: "0.78rem",
                          color: "#94a3b8",
                        }}
                      >
                        ({a.broker_name})
                      </span>
                    </div>
                    <div
                      style={{
                        fontSize: "0.75rem",
                        color: "#7c8ca4",
                      }}
                    >
                      Linked at: {new Date(a.created_at).toLocaleString()}
                    </div>
                  </div>
                  <Button
                    variant="secondary"
                    onClick={() => handleRemoveLink(a.id)}
                    disabled={removingId === a.id}
                    style={{ fontSize: "0.8rem", padding: "0.3rem 0.8rem" }}
                  >
                    {removingId === a.id ? "Removing…" : "Remove"}
                  </Button>
                </div>
              ))}
            </div>
          )}

          {/* Link new account */}
          <form onSubmit={handleLinkAccount}>
            {accounts.length === 0 ? (
              <p style={{ fontSize: "0.85rem", color: "#cbd5f5" }}>
                You don’t have any trading accounts yet. Go to the{" "}
                <span style={{ fontWeight: 600 }}>Accounts</span> page to add one.
              </p>
            ) : (
              <>
                <label
                  htmlFor="account-select"
                  style={{
                    display: "block",
                    fontSize: "0.85rem",
                    color: "#cbd5f5",
                    marginBottom: "0.25rem",
                  }}
                >
                  Link to another account
                </label>
                <div
                  style={{
                    display: "flex",
                    gap: "0.6rem",
                    alignItems: "center",
                    flexWrap: "wrap",
                  }}
                >
                  <select
                    id="account-select"
                    value={selectedAccountId}
                    onChange={(e) =>
                      setSelectedAccountId(
                        e.target.value ? Number(e.target.value) : ""
                      )
                    }
                    style={{
                      minWidth: 220,
                      padding: "0.45rem 0.6rem",
                      borderRadius: 8,
                      border: "1px solid rgba(148,163,184,0.65)",
                      background: "rgba(3,7,18,0.9)",
                      color: "#e5f4ff",
                      fontSize: "0.85rem",
                      outline: "none",
                    }}
                  >
                    <option value="">Select an account…</option>
                    {accounts.map((acc) => (
                      <option key={acc.id} value={acc.id}>
                        {acc.name} ({acc.broker_name})
                      </option>
                    ))}
                  </select>

                  <Button
                    type="submit"
                    disabled={linking || !accessToken}
                    style={{ padding: "0.45rem 1.1rem" }}
                  >
                    {linking ? "Linking…" : "Link account"}
                  </Button>
                </div>
              </>
            )}
          </form>
        </Card>

        {/* Change history */}
        <Card
          title="Change history"
          subtitle="Recent manual edits and AI auto-tunes for this strategy."
        >
          {loadingLogs && <p>Loading history…</p>}

          {!loadingLogs && changeLogs.length === 0 && (
            <p style={{ fontSize: "0.9rem", color: "#cbd5f5" }}>
              No changes recorded yet.
            </p>
          )}

          {!loadingLogs && changeLogs.length > 0 && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: "0.6rem",
              }}
            >
              {changeLogs.map((log) => {
                const sourceLabel =
                  log.source === "AI_AUTO_TUNE" ? "AI auto-tune" : "Manual edit";

                const actorLabel = log.changed_by_email || "AI";

                const before = log.before_settings || {};
                const after = log.after_settings || {};
                const trackedKeys = [
                  "timeframe",
                  "symbol_universe",
                  "ma_fast_period",
                  "ma_slow_period",
                  "ma_type",
                ];

                const changes: string[] = [];
                trackedKeys.forEach((key) => {
                  const beforeVal = (before as any)[key];
                  const afterVal = (after as any)[key];
                  if (beforeVal !== afterVal) {
                    const from =
                      beforeVal === undefined || beforeVal === null
                        ? "—"
                        : String(beforeVal);
                    const to =
                      afterVal === undefined || afterVal === null
                        ? "—"
                        : String(afterVal);
                    changes.push(`${key}: ${from} → ${to}`);
                  }
                });

                return (
                  <div
                    key={log.id}
                    style={{
                      borderRadius: 8,
                      border: "1px solid #222838",
                      padding: "0.6rem 0.7rem",
                      background: "rgba(7,12,30,0.9)",
                      fontSize: "0.85rem",
                    }}
                  >
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        marginBottom: "0.25rem",
                      }}
                    >
                      <div>
                        <span style={{ color: "#e5f4ff" }}>{sourceLabel}</span>{" "}
                        <span style={{ color: "#9ca3af" }}>by {actorLabel}</span>
                      </div>
                      <div
                        style={{
                          fontSize: "0.78rem",
                          color: "#9ca3af",
                        }}
                      >
                        {new Date(log.created_at).toLocaleString()}
                      </div>
                    </div>
                    {changes.length > 0 ? (
                      <ul
                        style={{
                          margin: 0,
                          paddingLeft: "1.1rem",
                          color: "#cbd5f5",
                        }}
                      >
                        {changes.map((c, idx) => (
                          <li key={idx}>{c}</li>
                        ))}
                      </ul>
                    ) : (
                      <p
                        style={{
                          margin: 0,
                          color: "#7c8ca4",
                          fontStyle: "italic",
                        }}
                      >
                        No tracked field changes in this entry.
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </Card>

        {/* AI Suggestions */}
        <Card
          title="AI Suggestions"
          subtitle="Suggestions are based on your latest backtest metrics for this strategy."
        >
          <div
            style={{
              display: "flex",
              justifyContent: "flex-end",
              marginBottom: "0.75rem",
            }}
          >
            <Button
              onClick={handleGetAISuggestions}
              disabled={loadingAI || !accessToken}
            >
              {loadingAI ? "Getting suggestions..." : "Get AI Suggestions"}
            </Button>
          </div>

          {!aiSuggestions && (
            <p style={{ fontSize: "0.9rem", color: "#c9d7f2" }}>
              Click &ldquo;Get AI Suggestions&rdquo; to fetch insights for this
              strategy.
            </p>
          )}

          {aiSuggestions && (
            <div style={{ fontSize: "0.95rem" }}>
              <p style={{ margin: 0 }}>
                <span style={labelStyle}>Latest run status:</span>
                <span style={valueStyle}>
                  {aiSuggestions.latest_run_status ?? "—"}
                </span>
              </p>
              <p style={{ margin: "0.2rem 0 0.4rem 0" }}>
                <span style={labelStyle}>Has backtest data:</span>
                <span style={valueStyle}>
                  {aiSuggestions.has_backtest_data ? "Yes" : "No"}
                </span>
              </p>

              {aiSuggestions.performance_summary && (
                <div
                  style={{
                    marginTop: "0.75rem",
                    padding: "0.75rem",
                    background: "#0b1120",
                    borderRadius: 6,
                    border: "1px solid rgba(74,179,255,0.2)",
                  }}
                >
                  <h3
                    style={{
                      fontSize: "1rem",
                      marginBottom: "0.5rem",
                      color: "#e5f4ff",
                    }}
                  >
                    Performance Summary
                  </h3>
                  <ul
                    style={{
                      listStyle: "none",
                      padding: 0,
                      margin: 0,
                      fontSize: "0.9rem",
                    }}
                  >
                    <li>
                      <span style={labelStyle}>Total return:</span>
                      <span style={valueStyle}>
                        {aiSuggestions.performance_summary.total_return_pct.toFixed(2)}%
                      </span>
                    </li>
                    <li>
                      <span style={labelStyle}>Max drawdown:</span>
                      <span style={valueStyle}>
                        {aiSuggestions.performance_summary.max_drawdown_pct.toFixed(
                          2
                        )}
                        %
                      </span>
                    </li>
                    <li>
                      <span style={labelStyle}>Win rate:</span>
                      <span style={valueStyle}>
                        {aiSuggestions.performance_summary.win_rate_pct.toFixed(
                          2
                        )}
                        %
                      </span>
                    </li>
                    <li>
                      <span style={labelStyle}>Number of trades:</span>
                      <span style={valueStyle}>
                        {aiSuggestions.performance_summary.num_trades.toFixed(
                          0
                        )}
                      </span>
                    </li>
                  </ul>
                </div>
              )}

              {aiSuggestions.parameter_suggestions && (
                <div
                  style={{
                    marginTop: "0.75rem",
                    padding: "0.75rem",
                    background: "#071b12",
                    borderRadius: 6,
                    border: "1px solid rgba(56,189,149,0.25)",
                  }}
                >
                  <h3
                    style={{
                      fontSize: "1rem",
                      marginBottom: "0.5rem",
                      color: "#bbf7d0",
                    }}
                  >
                    Parameter Suggestions
                  </h3>
                  <pre
                    style={{
                      whiteSpace: "pre-wrap",
                      fontFamily: "monospace",
                      fontSize: "0.85rem",
                      color: "#d1fae5",
                    }}
                  >
                    {JSON.stringify(
                      aiSuggestions.parameter_suggestions,
                      null,
                      2
                    )}
                  </pre>

                  {(aiSuggestions.parameter_suggestions as any)?.recommended_settings && (
                    <div
                      style={{
                        marginTop: "0.75rem",
                        paddingTop: "0.75rem",
                        borderTop: "1px solid rgba(56,189,149,0.4)",
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                        gap: "0.75rem",
                        flexWrap: "wrap",
                      }}
                    >
                      <div style={{ fontSize: "0.85rem", color: "#cbd5f5" }}>
                        AI has proposed a concrete set of strategy settings. You can
                        apply them to the editable form above, review, and then save.
                      </div>
                      <Button
                        type="button"
                        onClick={handleApplyAiRecommendations}
                        style={{ padding: "0.45rem 1.1rem", fontSize: "0.85rem" }}
                      >
                        Apply AI recommendations to settings
                      </Button>
                    </div>
                  )}
                </div>
              )}

              {aiSuggestions.risk_suggestions && (
                <div
                  style={{
                    marginTop: "0.75rem",
                    padding: "0.75rem",
                    background: "#20140b",
                    borderRadius: 6,
                    border: "1px solid rgba(249,115,22,0.25)",
                  }}
                >
                  <h3
                    style={{
                      fontSize: "1rem",
                      marginBottom: "0.5rem",
                      color: "#fed7aa",
                    }}
                  >
                    Risk Suggestions
                  </h3>
                  <pre
                    style={{
                      whiteSpace: "pre-wrap",
                      fontFamily: "monospace",
                      fontSize: "0.85rem",
                      color: "#ffedd5",
                    }}
                  >
                    {JSON.stringify(aiSuggestions.risk_suggestions, null, 2)}
                  </pre>
                </div>
              )}

              {aiSuggestions.notes && aiSuggestions.notes.length > 0 && (
                <div
                  style={{
                    marginTop: "0.75rem",
                    padding: "0.75rem",
                    background: "#080c18",
                    borderRadius: 6,
                    border: "1px solid rgba(148,163,184,0.35)",
                  }}
                >
                  <h3
                    style={{
                      fontSize: "1rem",
                      marginBottom: "0.5rem",
                      color: "#e5f4ff",
                    }}
                  >
                    Notes
                  </h3>
                  <ul
                    style={{
                      paddingLeft: "1.1rem",
                      margin: 0,
                      fontSize: "0.9rem",
                      color: "#cbd5f5",
                    }}
                  >
                    {aiSuggestions.notes.map((note, idx) => (
                      <li key={idx}>{note}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
        </Card>
      </div>
    </AppShell>
  );
}
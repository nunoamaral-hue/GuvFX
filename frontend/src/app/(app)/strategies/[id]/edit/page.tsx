"use client";

import type { CSSProperties } from "react";
import { useEffect, useState, useCallback } from "react";
import { useRouter, useParams } from "next/navigation";
import { apiFetch } from "@/lib/api";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import { useLang } from "@/components/AppShell";
import { t } from "@/lib/i18n";

// =============================================================================
// Types
// =============================================================================

type TrendlineBreakPocketZone = {
  zone_name: string;
  zone_type: "supply" | "demand" | "pivot";
  low: number;
  high: number;
  source: "seeded" | "user";
};

type TrendlineBreakPocketFilters = {
  template_slug?: string;
  enabled: boolean;
  direction_mode: "both" | "long" | "short";
  pairs_enabled: string[];
  htf_timeframe: string;
  execution_timeframe: string;
  rr_target: number;
  trendline_lookback_bars: number;
  trendline_pivot_strength: number;
  break_confirm_bars: number;
  swing_break_mode: string;
  swing_lookback: number;
  pocket_retest_required: boolean;
  entry_buffer_pips: Record<string, number>;
  overshoot_max_pips: Record<string, number>;
  clean_air_min_pips: Record<string, number>;
  max_trades_per_day: number;
  news_filter_mode: string;
  zones: Record<string, TrendlineBreakPocketZone[]>;
};

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
  ma_fast_period: number | null;
  ma_slow_period: number | null;
  ma_type: string | null;
  auto_optimize_by_ai: boolean;
  filters: TrendlineBreakPocketFilters | Record<string, unknown>;
  created_at: string;
  updated_at: string;
};

// =============================================================================
// Helper: Check if strategy is Trendline Break Pocket template
// =============================================================================
const isTrendlineBreakPocket = (strategy: Strategy): boolean => {
  const filters = strategy.filters as TrendlineBreakPocketFilters;
  return filters?.template_slug === "trendline-break-pocket-ali";
};

// =============================================================================
// Styles
// =============================================================================

const labelStyle: CSSProperties = {
  color: "#9db0c9",
  fontSize: "0.85rem",
  marginBottom: "0.25rem",
  display: "block",
};

const inputStyle: CSSProperties = {
  width: "100%",
  padding: "0.5rem 0.75rem",
  borderRadius: 8,
  border: "1px solid rgba(148,163,184,0.25)",
  backgroundColor: "rgba(15,23,42,0.6)",
  color: "#f0f6ff",
  fontSize: "0.9rem",
};

const selectStyle: CSSProperties = {
  ...inputStyle,
  cursor: "pointer",
};

const fieldGroupStyle: CSSProperties = {
  marginBottom: "1rem",
};

const gridStyle: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))",
  gap: "1rem",
};

const hintStyle: CSSProperties = {
  fontSize: "0.75rem",
  color: "#7c8ca4",
  marginTop: "0.25rem",
};

const zoneCardStyle: CSSProperties = {
  padding: "0.75rem",
  borderRadius: 8,
  backgroundColor: "rgba(15,23,42,0.5)",
  border: "1px solid rgba(148,163,184,0.15)",
  marginBottom: "0.5rem",
};

const seededBadgeStyle: CSSProperties = {
  display: "inline-block",
  padding: "0.15rem 0.4rem",
  borderRadius: 4,
  backgroundColor: "rgba(34,211,238,0.15)",
  border: "1px solid rgba(34,211,238,0.3)",
  color: "#67e8f9",
  fontSize: "0.7rem",
  fontWeight: 600,
  marginLeft: "0.5rem",
};

// =============================================================================
// Component
// =============================================================================

export default function EditStrategyPage() {
  const params = useParams();
  const strategyId = Number(params?.id);
  const router = useRouter();
  const lang = useLang();

  const [strategy, setStrategy] = useState<Strategy | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  // Form state
  const [formData, setFormData] = useState<Partial<Strategy>>({});

  // Fetch strategy
  useEffect(() => {
    if (!strategyId || Number.isNaN(strategyId)) return;

    const fetchStrategy = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await apiFetch<Strategy>(
          `/api/strategies/strategies/${strategyId}/`,
          {}
        );
        setStrategy(data);
        setFormData(data);
      } catch (err: unknown) {
        console.error(err);
        setError(err instanceof Error ? err.message : "Failed to load strategy.");
      } finally {
        setLoading(false);
      }
    };

    fetchStrategy();
  }, [strategyId]);

  // Handle form field changes
  const handleChange = useCallback(
    (field: keyof Strategy, value: unknown) => {
      setFormData((prev) => ({ ...prev, [field]: value }));
    },
    []
  );

  // Handle nested filter changes (for Trendline Break Pocket)
  const handleFilterChange = useCallback(
    (field: string, value: unknown) => {
      setFormData((prev) => ({
        ...prev,
        filters: {
          ...(prev.filters || {}),
          [field]: value,
        },
      }));
    },
    []
  );

  // Handle zone changes
  const handleZoneChange = useCallback(
    (symbol: string, zoneIndex: number, field: keyof TrendlineBreakPocketZone, value: unknown) => {
      setFormData((prev) => {
        const filters = (prev.filters || {}) as TrendlineBreakPocketFilters;
        const zones = { ...(filters.zones || {}) };
        const symbolZones = [...(zones[symbol] || [])];
        if (symbolZones[zoneIndex]) {
          symbolZones[zoneIndex] = {
            ...symbolZones[zoneIndex],
            [field]: value,
            source: "user", // Mark as user-edited
          };
        }
        return {
          ...prev,
          filters: {
            ...filters,
            zones: { ...zones, [symbol]: symbolZones },
          },
        };
      });
    },
    []
  );

  // Add new zone
  const handleAddZone = useCallback(
    (symbol: string) => {
      setFormData((prev) => {
        const filters = (prev.filters || {}) as TrendlineBreakPocketFilters;
        const zones = { ...(filters.zones || {}) };
        const symbolZones = [...(zones[symbol] || [])];
        symbolZones.push({
          zone_name: `Zone ${symbolZones.length + 1}`,
          zone_type: "demand",
          low: 1.0,
          high: 1.01,
          source: "user",
        });
        return {
          ...prev,
          filters: {
            ...filters,
            zones: { ...zones, [symbol]: symbolZones },
          },
        };
      });
    },
    []
  );

  // Remove zone
  const handleRemoveZone = useCallback(
    (symbol: string, zoneIndex: number) => {
      setFormData((prev) => {
        const filters = (prev.filters || {}) as TrendlineBreakPocketFilters;
        const zones = { ...(filters.zones || {}) };
        const symbolZones = [...(zones[symbol] || [])];
        symbolZones.splice(zoneIndex, 1);
        return {
          ...prev,
          filters: {
            ...filters,
            zones: { ...zones, [symbol]: symbolZones },
          },
        };
      });
    },
    []
  );

  // Save strategy
  const handleSave = async () => {
    if (!strategyId) return;

    setSaving(true);
    setError(null);
    setSuccess(null);

    try {
      await apiFetch(`/api/strategies/strategies/${strategyId}/`, {
        method: "PATCH",
        body: JSON.stringify(formData),
      });
      setSuccess("Strategy saved successfully.");
      // Refresh strategy data
      const updated = await apiFetch<Strategy>(
        `/api/strategies/strategies/${strategyId}/`,
        {}
      );
      setStrategy(updated);
      setFormData(updated);
    } catch (err: unknown) {
      console.error(err);
      setError(err instanceof Error ? err.message : "Failed to save strategy.");
    } finally {
      setSaving(false);
    }
  };

  // Guard for invalid strategyId
  if (Number.isNaN(strategyId)) {
    return (
      <div style={{ maxWidth: 900, margin: "0 auto" }}>
        <Alert type="error">Invalid strategy ID.</Alert>
      </div>
    );
  }

  const filters = (formData.filters || {}) as TrendlineBreakPocketFilters;
  const isTBP = strategy && isTrendlineBreakPocket(strategy);

  return (
    <div style={{ maxWidth: 900, margin: "0 auto" }}>
      {/* Header */}
      <div style={{ marginBottom: "1.5rem" }}>
        <button
          onClick={() => router.push(`/strategies/${strategyId}`)}
          style={{
            marginBottom: "0.5rem",
            color: "#9db0c9",
            background: "transparent",
            border: "none",
            cursor: "pointer",
            fontSize: "0.85rem",
            padding: "0.25rem 0",
          }}
        >
          ← {t(lang, "strategy.actions.backToList")}
        </button>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem", color: "#f0f6ff" }}>
          {t(lang, "strategyEdit.title")}
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "0.5rem" }}>
          {strategy?.name || "Loading..."}
        </p>
      </div>

      {/* Alerts */}
      {error && <Alert type="error">{error}</Alert>}
      {success && <Alert type="success">{success}</Alert>}

      {loading && <p style={{ color: "#9ca3af" }}>Loading...</p>}

      {!loading && strategy && (
        <>
          {/* Basic Info Card */}
          <Card title={t(lang, "strategyEdit.basicInfo")} subtitle={t(lang, "strategyEdit.basicInfoSubtitle")}>
            <div style={gridStyle}>
              <div style={fieldGroupStyle}>
                <label style={labelStyle}>{t(lang, "strategy.definition.nameLabel")}</label>
                <input
                  type="text"
                  style={inputStyle}
                  value={formData.name || ""}
                  onChange={(e) => handleChange("name", e.target.value)}
                />
              </div>

              <div style={fieldGroupStyle}>
                <label style={labelStyle}>{t(lang, "strategy.definition.symbolsLabel")}</label>
                <input
                  type="text"
                  style={inputStyle}
                  value={formData.symbol_universe || ""}
                  onChange={(e) => handleChange("symbol_universe", e.target.value)}
                  placeholder="EURUSD,GBPUSD"
                />
              </div>

              <div style={fieldGroupStyle}>
                <label style={labelStyle}>{t(lang, "strategy.definition.timeframeLabel")}</label>
                <select
                  style={selectStyle}
                  value={formData.timeframe || ""}
                  onChange={(e) => handleChange("timeframe", e.target.value)}
                >
                  <option value="">Select...</option>
                  <option value="M1">M1</option>
                  <option value="M5">M5</option>
                  <option value="M15">M15</option>
                  <option value="M30">M30</option>
                  <option value="H1">H1</option>
                  <option value="H4">H4</option>
                  <option value="D1">D1</option>
                  <option value="W1">W1</option>
                </select>
              </div>

              <div style={fieldGroupStyle}>
                <label style={labelStyle}>{t(lang, "strategy.definition.riskLabel")}</label>
                <input
                  type="number"
                  step="0.01"
                  min="0"
                  max="10"
                  style={inputStyle}
                  value={formData.risk_per_trade_pct || ""}
                  onChange={(e) => handleChange("risk_per_trade_pct", e.target.value)}
                />
                <p style={hintStyle}>Percentage of account risked per trade (0.01 - 10%)</p>
              </div>
            </div>

            <div style={fieldGroupStyle}>
              <label style={labelStyle}>{t(lang, "strategy.definition.descriptionLabel")}</label>
              <textarea
                style={{ ...inputStyle, minHeight: 80, resize: "vertical" }}
                value={formData.description || ""}
                onChange={(e) => handleChange("description", e.target.value)}
              />
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
              <label style={{ ...labelStyle, marginBottom: 0 }}>
                {t(lang, "strategyEdit.enabled")}
              </label>
              <input
                type="checkbox"
                checked={formData.is_active ?? true}
                onChange={(e) => handleChange("is_active", e.target.checked)}
                style={{ width: 18, height: 18, cursor: "pointer" }}
              />
            </div>
          </Card>

          {/* Trendline Break Pocket Specific Parameters */}
          {isTBP && (
            <>
              <Card
                title={t(lang, "strategyEdit.tbpParameters")}
                subtitle={t(lang, "strategyEdit.tbpParametersSubtitle")}
              >
                <div style={gridStyle}>
                  <div style={fieldGroupStyle}>
                    <label style={labelStyle}>{t(lang, "strategyEdit.directionMode")}</label>
                    <select
                      style={selectStyle}
                      value={filters.direction_mode || "both"}
                      onChange={(e) => handleFilterChange("direction_mode", e.target.value)}
                    >
                      <option value="both">Both (Long + Short)</option>
                      <option value="long">Long Only</option>
                      <option value="short">Short Only</option>
                    </select>
                  </div>

                  <div style={fieldGroupStyle}>
                    <label style={labelStyle}>{t(lang, "strategyEdit.htfTimeframe")}</label>
                    <select
                      style={selectStyle}
                      value={filters.htf_timeframe || "D1"}
                      onChange={(e) => handleFilterChange("htf_timeframe", e.target.value)}
                    >
                      <option value="H4">H4</option>
                      <option value="D1">D1</option>
                      <option value="W1">W1</option>
                    </select>
                  </div>

                  <div style={fieldGroupStyle}>
                    <label style={labelStyle}>{t(lang, "strategyEdit.rrTarget")}</label>
                    <input
                      type="number"
                      step="0.1"
                      min="0.5"
                      style={inputStyle}
                      value={filters.rr_target ?? 2.0}
                      onChange={(e) => handleFilterChange("rr_target", parseFloat(e.target.value) || 2.0)}
                    />
                    <p style={hintStyle}>Risk-Reward target (e.g., 2.0 = 2R)</p>
                  </div>

                  <div style={fieldGroupStyle}>
                    <label style={labelStyle}>{t(lang, "strategyEdit.trendlineLookback")}</label>
                    <input
                      type="number"
                      min="50"
                      style={inputStyle}
                      value={filters.trendline_lookback_bars ?? 101}
                      onChange={(e) => handleFilterChange("trendline_lookback_bars", parseInt(e.target.value) || 101)}
                    />
                    <p style={hintStyle}>Minimum 50 bars for trendline detection</p>
                  </div>

                  <div style={fieldGroupStyle}>
                    <label style={labelStyle}>{t(lang, "strategyEdit.pivotStrength")}</label>
                    <input
                      type="number"
                      min="1"
                      max="10"
                      style={inputStyle}
                      value={filters.trendline_pivot_strength ?? 2}
                      onChange={(e) => handleFilterChange("trendline_pivot_strength", parseInt(e.target.value) || 2)}
                    />
                  </div>

                  <div style={fieldGroupStyle}>
                    <label style={labelStyle}>{t(lang, "strategyEdit.swingLookback")}</label>
                    <input
                      type="number"
                      min="3"
                      style={inputStyle}
                      value={filters.swing_lookback ?? 7}
                      onChange={(e) => handleFilterChange("swing_lookback", parseInt(e.target.value) || 7)}
                    />
                  </div>

                  <div style={fieldGroupStyle}>
                    <label style={labelStyle}>{t(lang, "strategyEdit.maxTradesPerDay")}</label>
                    <input
                      type="number"
                      min="1"
                      max="10"
                      style={inputStyle}
                      value={filters.max_trades_per_day ?? 1}
                      onChange={(e) => handleFilterChange("max_trades_per_day", parseInt(e.target.value) || 1)}
                    />
                  </div>

                  <div style={fieldGroupStyle}>
                    <label style={labelStyle}>{t(lang, "strategyEdit.newsFilterMode")}</label>
                    <select
                      style={selectStyle}
                      value={filters.news_filter_mode || "major_only"}
                      onChange={(e) => handleFilterChange("news_filter_mode", e.target.value)}
                    >
                      <option value="none">No Filter</option>
                      <option value="major_only">Major News Only</option>
                      <option value="all">All News</option>
                    </select>
                  </div>
                </div>

                <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", marginTop: "0.5rem" }}>
                  <label style={{ ...labelStyle, marginBottom: 0 }}>
                    {t(lang, "strategyEdit.pocketRetestRequired")}
                  </label>
                  <input
                    type="checkbox"
                    checked={filters.pocket_retest_required ?? true}
                    onChange={(e) => handleFilterChange("pocket_retest_required", e.target.checked)}
                    style={{ width: 18, height: 18, cursor: "pointer" }}
                  />
                </div>
              </Card>

              {/* HTF Zones Card */}
              <Card
                title={t(lang, "strategyEdit.htfZones")}
                subtitle={t(lang, "strategyEdit.htfZonesSubtitle")}
              >
                <div
                  style={{
                    padding: "0.5rem 0.75rem",
                    borderRadius: 6,
                    backgroundColor: "rgba(34,211,238,0.08)",
                    border: "1px solid rgba(34,211,238,0.2)",
                    marginBottom: "1rem",
                    fontSize: "0.8rem",
                    color: "#67e8f9",
                  }}
                >
                  {t(lang, "strategyEdit.zonesSeededHint")}
                </div>

                {Object.entries(filters.zones || {}).map(([symbol, zones]) => (
                  <div key={symbol} style={{ marginBottom: "1.5rem" }}>
                    <h4 style={{ color: "#93c5fd", fontSize: "1rem", marginBottom: "0.75rem" }}>
                      {symbol} Zones
                    </h4>

                    {zones.map((zone, idx) => (
                      <div key={idx} style={zoneCardStyle}>
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
                          <div style={{ display: "flex", alignItems: "center" }}>
                            <input
                              type="text"
                              style={{ ...inputStyle, width: 140, padding: "0.35rem 0.5rem" }}
                              value={zone.zone_name}
                              onChange={(e) => handleZoneChange(symbol, idx, "zone_name", e.target.value)}
                            />
                            {zone.source === "seeded" && (
                              <span style={seededBadgeStyle}>Seeded</span>
                            )}
                          </div>
                          <button
                            type="button"
                            onClick={() => handleRemoveZone(symbol, idx)}
                            style={{
                              background: "rgba(239,68,68,0.15)",
                              border: "1px solid rgba(239,68,68,0.3)",
                              color: "#fca5a5",
                              padding: "0.25rem 0.5rem",
                              borderRadius: 4,
                              cursor: "pointer",
                              fontSize: "0.75rem",
                            }}
                          >
                            Remove
                          </button>
                        </div>

                        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: "0.75rem" }}>
                          <div>
                            <label style={{ ...labelStyle, fontSize: "0.75rem" }}>Type</label>
                            <select
                              style={{ ...selectStyle, padding: "0.35rem 0.5rem", fontSize: "0.85rem" }}
                              value={zone.zone_type}
                              onChange={(e) => handleZoneChange(symbol, idx, "zone_type", e.target.value)}
                            >
                              <option value="supply">Supply</option>
                              <option value="demand">Demand</option>
                              <option value="pivot">Pivot</option>
                            </select>
                          </div>
                          <div>
                            <label style={{ ...labelStyle, fontSize: "0.75rem" }}>Low</label>
                            <input
                              type="number"
                              step="0.0001"
                              style={{ ...inputStyle, padding: "0.35rem 0.5rem", fontSize: "0.85rem" }}
                              value={zone.low}
                              onChange={(e) => handleZoneChange(symbol, idx, "low", parseFloat(e.target.value))}
                            />
                          </div>
                          <div>
                            <label style={{ ...labelStyle, fontSize: "0.75rem" }}>High</label>
                            <input
                              type="number"
                              step="0.0001"
                              style={{ ...inputStyle, padding: "0.35rem 0.5rem", fontSize: "0.85rem" }}
                              value={zone.high}
                              onChange={(e) => handleZoneChange(symbol, idx, "high", parseFloat(e.target.value))}
                            />
                          </div>
                        </div>
                      </div>
                    ))}

                    <button
                      type="button"
                      onClick={() => handleAddZone(symbol)}
                      style={{
                        background: "rgba(59,130,246,0.15)",
                        border: "1px solid rgba(59,130,246,0.3)",
                        color: "#93c5fd",
                        padding: "0.4rem 0.75rem",
                        borderRadius: 6,
                        cursor: "pointer",
                        fontSize: "0.8rem",
                        fontWeight: 500,
                      }}
                    >
                      + Add Zone
                    </button>
                  </div>
                ))}
              </Card>
            </>
          )}

          {/* Entry/Exit Logic Card */}
          <Card
            title={t(lang, "strategyEdit.logicRules")}
            subtitle={t(lang, "strategyEdit.logicRulesSubtitle")}
          >
            <div style={fieldGroupStyle}>
              <label style={labelStyle}>{t(lang, "strategy.definition.entryLogicLabel")}</label>
              <textarea
                style={{ ...inputStyle, minHeight: 100, resize: "vertical", fontFamily: "monospace", fontSize: "0.85rem" }}
                value={formData.entry_logic || ""}
                onChange={(e) => handleChange("entry_logic", e.target.value)}
              />
            </div>

            <div style={fieldGroupStyle}>
              <label style={labelStyle}>{t(lang, "strategy.definition.exitLogicLabel")}</label>
              <textarea
                style={{ ...inputStyle, minHeight: 100, resize: "vertical", fontFamily: "monospace", fontSize: "0.85rem" }}
                value={formData.exit_logic || ""}
                onChange={(e) => handleChange("exit_logic", e.target.value)}
              />
            </div>

            <div style={fieldGroupStyle}>
              <label style={labelStyle}>{t(lang, "strategy.definition.notesLabel")}</label>
              <textarea
                style={{ ...inputStyle, minHeight: 80, resize: "vertical" }}
                value={formData.notes || ""}
                onChange={(e) => handleChange("notes", e.target.value)}
              />
            </div>
          </Card>

          {/* Actions */}
          <Card>
            <div style={{ display: "flex", gap: "1rem", alignItems: "center" }}>
              <Button variant="primary" onClick={handleSave} disabled={saving}>
                {saving ? t(lang, "strategyEdit.saving") : t(lang, "strategyEdit.save")}
              </Button>
              <Button variant="secondary" onClick={() => router.push(`/strategies/${strategyId}`)}>
                {t(lang, "strategyEdit.cancel")}
              </Button>
              <span style={{ marginLeft: "auto", fontSize: "0.8rem", color: "#7c8ca4" }}>
                {t(lang, "strategyEdit.lastUpdated")}: {strategy.updated_at ? new Date(strategy.updated_at).toLocaleString() : "—"}
              </span>
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { apiFetch } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Alert } from "@/components/ui/Alert";
import {
  fetchSignalCopyStatus,
  deriveOwnedState,
  mpDisplayName,
  type SignalCopyStatus,
} from "@/lib/strategy-journey";
import { fetchJourney, type HostedJourney } from "@/lib/hosted-journey";

// The marketplace ids that have an automated (signal-copy) execution path. My Strategies surfaces these with
// a normalized customer lifecycle (Added / Ready to enable / Enabled / Needs attention) + a Manage action, so
// the customer never has to guess where to go to enable, pause or manage automated trading.
const AUTOMATED_MARKETPLACE_IDS = ["mp-010"] as const;

const ownedChipStyle = (tone: "ready" | "action" | "attention" | "neutral"): React.CSSProperties => {
  const m = {
    ready: { bg: "rgba(34,197,94,0.14)", border: "rgba(34,197,94,0.35)", text: "#86efac" },
    action: { bg: "rgba(59,130,246,0.14)", border: "rgba(59,130,246,0.35)", text: "#93c5fd" },
    attention: { bg: "rgba(245,158,11,0.14)", border: "rgba(245,158,11,0.40)", text: "#fcd34d" },
    neutral: { bg: "rgba(100,116,139,0.14)", border: "rgba(100,116,139,0.35)", text: "#94a3b8" },
  }[tone];
  return {
    display: "inline-flex", alignItems: "center", padding: "0.15rem 0.55rem", borderRadius: 999,
    border: `1px solid ${m.border}`, background: m.bg, color: m.text, fontSize: "0.72rem", fontWeight: 700,
  };
};

type OwnedRow = { mp: string; name: string; status: SignalCopyStatus; journey: HostedJourney | null };

/** Managed section: the customer's owned automated strategies with a normalized lifecycle + Manage action.
 *  AJ#7.2 — PRESENTATIONAL: the parent owns the fetch so it can DEDUPE the generic list (hide the backing
 *  Strategy row so a signal-copy product renders ONCE). Renders nothing when the customer owns none. */
function OwnedAutomatedStrategies({ rows, justEnabled }: { rows: OwnedRow[]; justEnabled: boolean }) {
  if (rows.length === 0) return null;

  return (
    <div style={{ marginBottom: "1.25rem" }}>
      {justEnabled && rows.some((r) => r.status.enabled) && (
        <Alert type="success">Your strategy is enabled. GuvFX will trade it automatically on your account.</Alert>
      )}
      <div
        style={{
          border: "1px solid rgba(255,255,255,0.10)", borderRadius: 14,
          background: "linear-gradient(180deg, rgba(10,16,35,0.72), rgba(6,10,25,0.85))",
          boxShadow: "0 10px 30px rgba(0,0,0,0.45)", padding: "1rem 1rem 1.1rem",
        }}
      >
        <div style={{ fontWeight: 700, color: "#e5f4ff", fontSize: "1.05rem", marginBottom: "0.6rem" }}>
          Automated strategies
        </div>
        <div style={{ display: "grid", gap: "0.6rem" }}>
          {rows.map((r) => {
            const canEnable = !!r.journey && (r.journey.execution_authorized === true || r.journey.can_enable_automated_trading === true);
            const view = deriveOwnedState({
              owned: !!r.status.armed, enabled: !!r.status.enabled, ambiguous: !!r.status.ambiguous,
              canArm: canEnable, journeyReady: canEnable,
            });
            return (
              <div
                key={r.mp}
                style={{
                  display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap",
                  border: "1px solid rgba(255,255,255,0.10)", borderRadius: 12, padding: "0.8rem 1rem",
                  background: "rgba(7, 12, 30, 0.9)",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                  <span style={{ color: "#f1f5ff", fontWeight: 600, fontSize: "1rem" }}>{r.name}</span>
                  <span style={ownedChipStyle(view.tone)}>{view.label}</span>
                </div>
                <Link href={`/strategies/configure?mp=${encodeURIComponent(r.mp)}`} style={{ textDecoration: "none" }}>
                  <Button variant={view.state === "ready_to_enable" ? "primary" : "secondary"}>
                    {view.state === "enabled" ? "Manage" : view.state === "ready_to_enable" ? "Enable" : "Configure"}
                  </Button>
                </Link>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

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
  filters: Record<string, unknown> | null;
  created_at: string;
  /** AJ#7.2 — server-computed dedup flag: this row backs a signal-copy product the customer owns and is shown
   *  in the managed "Automated strategies" section, so the generic list hides it (no client-side race). */
  is_signal_copy_backed?: boolean;
};

export default function StrategiesListPage() {
  const glassCardStyle: React.CSSProperties = {
    border: "1px solid rgba(255,255,255,0.10)",
    borderRadius: 14,
    background:
      "linear-gradient(180deg, rgba(10,16,35,0.72), rgba(6,10,25,0.85))",
    boxShadow: "0 10px 30px rgba(0,0,0,0.45)",
  };

  const rowStyle: React.CSSProperties = {
    border: "1px solid rgba(255,255,255,0.10)",
    borderRadius: 12,
    padding: "0.9rem 1rem",
    background: "rgba(7, 12, 30, 0.9)",
  };
  const [accessToken, setAccessToken] = useState("");
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [actionBusyId, setActionBusyId] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // AJ#7.2 — the customer's owned automated strategies (Wayond today), rendered in the managed section below.
  // This fetch feeds ONLY the managed card; the generic-list DEDUP is server-computed (Strategy
  // .is_signal_copy_backed), so the list never races this secondary fetch.
  const [automatedRows, setAutomatedRows] = useState<OwnedRow[]>([]);
  const [justEnabled, setJustEnabled] = useState(false);

  useEffect(() => {
    const checkAuth = async () => {
      if (typeof window === "undefined") return;

      const stored = window.localStorage.getItem("guvfx_access_token");
      if (stored) {
        setAccessToken(stored);
        return;
      }

      // Fallback: cookie-based auth (apiFetch includes credentials)
      try {
        await apiFetch("/api/auth/me/", { method: "GET" });
        // Any non-empty value enables UI controls guarded by accessToken
        setAccessToken("cookie");
      } catch {
        // Not authenticated; keep empty
      }
    };

    checkAuth();
  }, []);

  useEffect(() => {
    

    const fetchStrategies = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await apiFetch<Strategy[]>(
          "/api/strategies/strategies/",
          {});
        setStrategies(data);
      } catch (err: unknown) {
        console.error(err);
        const message =
          err instanceof Error ? err.message : "Failed to load strategies.";
        setError(message);
      } finally {
        setLoading(false);
      }
    };

    fetchStrategies();
  }, [accessToken]);

  useEffect(() => {
    if (!accessToken) return;
    // Read the just-enabled hint synchronously; applied together with the rows once the load resolves so we
    // never trigger a cascading render mid-fetch.
    const enabledHint = typeof window !== "undefined"
      && new URLSearchParams(window.location.search).get("enabled") === "1";
    let cancelled = false;
    (async () => {
      const journey = await fetchJourney().then((r) => (r.ok ? r.journey : null)).catch(() => null);
      const found: OwnedRow[] = [];
      for (const mp of AUTOMATED_MARKETPLACE_IDS) {
        const st = await fetchSignalCopyStatus(mp).catch(() => null);
        if (st?.armed) found.push({ mp, name: mpDisplayName(mp), status: st, journey });
      }
      if (!cancelled) { setAutomatedRows(found); setJustEnabled(enabledHint); }
    })();
    return () => { cancelled = true; };
  }, [accessToken]);

  // DEDUP (client, in LOCKSTEP with the managed card): hide a backing row from the generic list ONLY once the
  // managed section actually renders it — both derive from the SAME status fetch (`automatedRows`). This keeps
  // the product visible EXACTLY ONCE and, crucially, NEVER zero times: if the status fetch fails, automatedRows
  // is empty → nothing is deduped → the backing row still shows in the generic list (once), so an owned/enabled
  // product can never silently vanish.
  const automatedStrategyIds = new Set(
    automatedRows
      .map((r) => r.status.strategy_id)
      .filter((id): id is number => typeof id === "number"),
  );
  const visibleStrategies = strategies.filter((s) => !automatedStrategyIds.has(s.id));
  // A backing row that IS still in the generic list (managed card not yet loaded, or its fetch failed) must not
  // wear the misleading green "Active" badge (invariant 3). The server flag lets us render it honestly.
  const ownsSignalCopyProduct = strategies.some((s) => s.is_signal_copy_backed);

  return (
      <div style={{ maxWidth: 1100, margin: "0 auto" }}>
        <h1 style={{ fontSize: "2rem", marginBottom: "0.25rem" }}>
          My Strategies
        </h1>
        <p style={{ fontSize: "0.9rem", color: "#b7c5dd", marginBottom: "1rem" }}>
          View and analyze your strategies, then dive into AI-assisted insights.
        </p>

        {error && <Alert type="error">{error}</Alert>}
        {actionError && <Alert type="error">{actionError}</Alert>}

        <OwnedAutomatedStrategies rows={automatedRows} justEnabled={justEnabled} />

        <div style={{ ...glassCardStyle, padding: "1rem 1rem 1.1rem", marginBottom: "1rem" }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
            <div style={{ fontWeight: 700, color: "#e5f4ff", fontSize: "1.05rem" }}>Strategies</div>
            <Link href="/strategies/create" style={{ textDecoration: "none" }}>
              <Button variant="primary">Create strategy</Button>
            </Link>
          </div>
          <div style={{ marginTop: 6, fontSize: "0.85rem", color: "#9ca3af" }}>
            Manage your strategies and toggle them on/off.
          </div>
        </div>

        {!accessToken && (
          <p style={{ fontStyle: "italic", fontSize: "0.9rem", color: "#9ca3af" }}>
            Please log in to view your strategies.
          </p>
        )}

        {loading && <p>Loading strategies...</p>}

        {!loading && visibleStrategies.length === 0 && automatedRows.length === 0 && !ownsSignalCopyProduct && accessToken && !error && (
          <p style={{ fontSize: "0.9rem" }}>
            No strategies found yet. Create one from the Builder then come back
            here.
          </p>
        )}

        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(340px, 1fr))",
            gap: "1rem",
            marginTop: "0.75rem",
          }}
        >
          {visibleStrategies.map((strategy) => (
            <div
              key={strategy.id}
              style={{
                ...rowStyle,
                position: "relative",
                overflow: "hidden",
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
                <h3
                  style={{
                    fontSize: "1.05rem",
                    margin: 0,
                    color: "#f1f5ff",
                  }}
                >
                  {strategy.name}{" "}
                  <span
                    style={{
                      fontSize: "0.8rem",
                      fontWeight: 400,
                      color: "#8897b2",
                      marginLeft: 8,
                    }}
                  >
                    #{strategy.id}
                  </span>
                </h3>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  {strategy.is_signal_copy_backed ? (
                    // A signal-copy backing row that hasn't yet moved to the managed section (or whose status
                    // fetch failed): render it HONESTLY as an automated product — never the misleading green
                    // "Active" badge, which reflects Strategy.is_active, not whether it is trading.
                    <Badge color="blue">Automated</Badge>
                  ) : (
                    <Badge color={strategy.is_active ? "green" : "gray"}>
                      {strategy.is_active ? "Active" : "Inactive"}
                    </Badge>
                  )}

                  <select
                    aria-label="Strategy actions"
                    defaultValue=""
                    disabled={!accessToken || actionBusyId === strategy.id}
                    onChange={async (e) => {
                      const action = e.target.value;
                      e.target.value = "";
                      if (!action) return;

                      setActionError(null);
                      setActionBusyId(strategy.id);

                      try {
                        if (action === "toggle") {
                          const updated = await apiFetch<Strategy>(
                            `/api/strategies/strategies/${strategy.id}/`,
                            {
                              method: "PATCH",
                              body: JSON.stringify({ is_active: !strategy.is_active }),
                            }
                          );
                          setStrategies((prev) =>
                            prev.map((s) => (s.id === strategy.id
                              // Preserve the signal-copy backing flag across the update so the honest
                              // "Automated" badge can never revert to a misleading "Active" (invariant 3).
                              ? { ...updated, is_signal_copy_backed: updated.is_signal_copy_backed ?? s.is_signal_copy_backed }
                              : s))
                          );
                          return;
                        }

                        if (action === "delete") {
                          const ok = window.confirm(
                            `Delete strategy "${strategy.name}"? This cannot be undone.`
                          );
                          if (!ok) return;

                          await apiFetch(`/api/strategies/strategies/${strategy.id}/`, {
                            method: "DELETE",
                          });
                          setStrategies((prev) => prev.filter((s) => s.id !== strategy.id));
                          return;
                        }
                      } catch (err: unknown) {
                        console.error(err);
                        const message =
                          err instanceof Error ? err.message : "Action failed.";
                        setActionError(message);
                      } finally {
                        setActionBusyId(null);
                      }
                    }}
                    style={{
                      padding: "0.35rem 0.5rem",
                      borderRadius: 10,
                      border: "1px solid rgba(255,255,255,0.12)",
                      background: "rgba(10,16,35,0.55)",
                      color: "#cbd5f5",
                      fontSize: "0.8rem",
                      outline: "none",
                    }}
                  >
                    <option value="">Actions</option>
                    <option value="toggle">{strategy.is_active ? "Deactivate" : "Activate"}</option>
                    <option value="delete">Delete…</option>
                  </select>
                </div>
              </div>
              <p
                style={{
                  fontSize: "0.9rem",
                  margin: "0.2rem 0 0.3rem 0",
                  color: "#d0e1ff",
                }}
              >
                {strategy.description || (
                  <span style={{ color: "#7c8ca4" }}>No description</span>
                )}
              </p>
              <p
                style={{
                  fontSize: "0.8rem",
                  color: "#8fa0b7",
                  margin: 0,
                }}
              >
                <strong>Symbols:</strong> {strategy.symbol_universe || "—"}{" "}
                &nbsp;|&nbsp;
                <strong>Timeframe:</strong> {strategy.timeframe || "—"}
                {typeof strategy.filters?.template_slug === "string" && (
                  <>
                    &nbsp;|&nbsp;
                    <strong>Engine:</strong>{" "}
                    {strategy.filters.template_slug === "trendline-break-pocket-ali"
                      ? "TBP"
                      : strategy.filters.template_slug ===
                          "adaptive-liquidity-trap-scalper"
                        ? "ALTS"
                        : strategy.filters.template_slug ===
                            "structural-continuation-engine"
                          ? "SCE"
                          : String(strategy.filters.template_slug)}
                  </>
                )}
              </p>
              <p
                style={{
                  fontSize: "0.75rem",
                  color: "#6d7a92",
                  marginTop: "0.2rem",
                }}
              >
                Created: {new Date(strategy.created_at).toLocaleString()}
              </p>
              <div style={{ marginTop: "0.75rem" }}>
                <Link
                  href={`/strategies/${strategy.id}`}
                  style={{
                    fontSize: "0.85rem",
                    color: "#4ab3ff",
                    textDecoration: "none",
                  }}
                >
                  View details & AI suggestions →
                </Link>
              </div>
            </div>
          ))}
        </div>
      </div>
  );
}

"use client";

// =============================================================================
// GFX-PKT-STAKEHOLDER-BRANDING-AND-OPERATIONAL-OBSERVABILITY — Workstream B2.
//
// Internal /operations status page. Read-only: it renders the backend
// operations-summary (health + source-aware strategy metrics + broker metrics +
// open positions/plans/candidates + dispatch + open alerts). It NEVER places an
// order or mutates any state — it only polls a read endpoint. Staff-only (the
// endpoint is IsAdminUser; non-staff get 403 and see the notice below).
// =============================================================================

import { useEffect, useState, useCallback } from "react";
import { apiFetch } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";

type StratRow = {
  key: string; source_label: string; strategy: string; armed: boolean;
  signals_today: number; accepted: number; rejected: number;
  wins: number; losses: number; breakevens: number; realised_pnl: string;
  cards_sent: number; last_signal_at: string | null; per_leg_lot: string | null;
};
type Heartbeat = { source: string; age_s: number | null; interval_s: number; state: string };
type Component = { component: string; status: string; since: string | null; consecutive_failures: number };
type AlertRow = { severity: string; component: string; detail: string; first_seen: string | null; acknowledged: boolean };
type Summary = {
  generated_at: string;
  overall: "HEALTHY" | "WARNING" | "CRITICAL" | "DISABLED" | "UNKNOWN";
  control: { auto_execution: boolean; mode: string; kill_switch: boolean };
  components: Component[];
  heartbeats: Heartbeat[];
  strategies: StratRow[];
  positions: { open: number; promoted_plans: number; pending_candidates: number; failed_candidates: number };
  dispatch: { enabled: boolean; transport: string; last_delivery_at: string | null };
  broker: { account: string | null; reachable: boolean; balance?: number; equity?: number; free_margin?: number; margin_level?: number; reason?: string };
  alerts: AlertRow[];
};

const STATE_COLOR: Record<string, "green" | "yellow" | "red" | "gray"> = {
  HEALTHY: "green", OK: "green", WARNING: "yellow", STALE: "yellow", DEGRADED: "yellow",
  CRITICAL: "red", FAILED: "red", DISABLED: "gray", UNKNOWN: "gray",
};

function ago(iso: string | null): string {
  if (!iso) return "—";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 90) return `${s}s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

export default function OperationsPage() {
  const [s, setS] = useState<Summary | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const data = await apiFetch<Summary>("/api/reliability/operations-summary/");
      setS(data); setErr(null);
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : "Failed to load operations status");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const id = setInterval(load, 30000);
    return () => clearInterval(id);
  }, [load]);

  if (loading) return <div style={{ padding: 24 }}>Loading operational status…</div>;
  if (err) return <div style={{ padding: 24, color: "#b91c1c" }}>Operational status unavailable: {err} <br /><span style={{ color: "#6b7280", fontSize: 13 }}>(This page is restricted to authorised internal staff.)</span></div>;
  if (!s) return null;

  const overallColor = STATE_COLOR[s.overall] || "gray";
  return (
    <div style={{ padding: 24, maxWidth: 1100, margin: "0 auto" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <h1 style={{ fontSize: 26, fontWeight: 700 }}>Operational status</h1>
        <Badge color={overallColor}>{s.overall}</Badge>
      </div>
      <div style={{ color: "#6b7280", fontSize: 13, marginBottom: 20 }}>Updated {ago(s.generated_at)} · read-only monitoring</div>

      <Section title="System control">
        <Row label="Auto execution" value={s.control.auto_execution ? "On" : "Off"} color={s.control.auto_execution ? "green" : "gray"} />
        <Row label="Mode" value={s.control.mode} />
        <Row label="Kill switch" value={s.control.kill_switch ? "ENGAGED" : "Off"} color={s.control.kill_switch ? "red" : "green"} />
      </Section>

      <Section title="Strategies">
        {s.strategies.map((st) => (
          <div key={st.key} style={{ border: "1px solid #edeff2", borderRadius: 10, padding: 14, marginBottom: 12 }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <strong>{st.strategy}</strong>
              <Badge color={st.armed ? "green" : "gray"}>{st.armed ? "Live" : "Paused"}</Badge>
            </div>
            <div style={{ color: "#6b7280", fontSize: 13, margin: "4px 0 8px" }}>
              Source: {st.source_label}{st.per_leg_lot ? ` · ${st.per_leg_lot} lots/TP` : ""} · last signal {ago(st.last_signal_at)}
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 18, fontSize: 14 }}>
              <Stat k="Signals today" v={st.signals_today} />
              <Stat k="Accepted" v={st.accepted} />
              <Stat k="Rejected" v={st.rejected} />
              <Stat k="Wins" v={st.wins} />
              <Stat k="Losses" v={st.losses} />
              <Stat k="Breakeven" v={st.breakevens} />
              <Stat k="Realised PnL" v={`$${st.realised_pnl}`} />
              <Stat k="Cards sent" v={st.cards_sent} />
            </div>
          </div>
        ))}
      </Section>

      <Section title="Broker / account">
        <Row label="Account" value={s.broker.account || "—"} />
        {s.broker.reachable ? (
          <>
            <Row label="Balance" value={s.broker.balance != null ? `$${s.broker.balance}` : "—"} />
            <Row label="Equity" value={s.broker.equity != null ? `$${s.broker.equity}` : "—"} />
            <Row label="Free margin" value={s.broker.free_margin != null ? `$${s.broker.free_margin}` : "—"} />
            <Row label="Margin level" value={s.broker.margin_level != null ? `${Math.round(s.broker.margin_level)}%` : "—"} />
          </>
        ) : (
          <Row label="Broker link" value={`Unreachable (${s.broker.reason || "n/a"})`} color="yellow" />
        )}
      </Section>

      <Section title="Positions & pipeline">
        <Row label="Open positions" value={String(s.positions.open)} />
        <Row label="Active (promoted) plans" value={String(s.positions.promoted_plans)} />
        <Row label="Pending notification cards" value={String(s.positions.pending_candidates)} />
        <Row label="Failed notification cards" value={String(s.positions.failed_candidates)} color={s.positions.failed_candidates ? "yellow" : "gray"} />
        <Row label="Dispatch" value={`${s.dispatch.enabled ? "on" : "off"} (${s.dispatch.transport}) · last sent ${ago(s.dispatch.last_delivery_at)}`} />
      </Section>

      <Section title="Component health">
        {s.heartbeats.map((h) => (
          <Row key={h.source} label={h.source} value={h.age_s == null ? "no beat" : `${h.age_s}s ago (limit ${h.interval_s}s)`} color={STATE_COLOR[h.state] || "gray"} />
        ))}
        {s.components.map((c) => (
          <Row key={c.component} label={c.component} value={c.status} color={STATE_COLOR[c.status] || "gray"} />
        ))}
      </Section>

      <Section title={`Open alerts (${s.alerts.length})`}>
        {s.alerts.length === 0 ? <div style={{ color: "#16a34a" }}>No open alerts.</div> : s.alerts.map((a, i) => (
          <div key={i} style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid #f4f6f8" }}>
            <span><Badge color={STATE_COLOR[a.severity] || "gray"}>{a.severity}</Badge> <strong style={{ marginLeft: 6 }}>{a.component}</strong> — {a.detail}</span>
            <span style={{ color: "#6b7280", fontSize: 12 }}>{ago(a.first_seen)}{a.acknowledged ? " · ack" : ""}</span>
          </div>
        ))}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div style={{ marginBottom: 24 }}>
      <h2 style={{ fontSize: 15, fontWeight: 700, textTransform: "uppercase", color: "#6b7280", marginBottom: 10 }}>{title}</h2>
      {children}
    </div>
  );
}
function Row({ label, value, color }: { label: string; value: string; color?: "green" | "yellow" | "red" | "gray" }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "6px 0", borderBottom: "1px solid #f4f6f8" }}>
      <span style={{ color: "#374151" }}>{label}</span>
      {color ? <Badge color={color}>{value}</Badge> : <span style={{ fontWeight: 600 }}>{value}</span>}
    </div>
  );
}
function Stat({ k, v }: { k: string; v: string | number }) {
  return (
    <div><div style={{ color: "#6b7280", fontSize: 12 }}>{k}</div><div style={{ fontWeight: 700 }}>{v}</div></div>
  );
}
